"""SURGE local paper-trading API and dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from bot.risk_gates import RiskGates
from bot.onboard_ai import OnboardTradingAgent
from bot.live_coinbase_adapter import (LiveCoinbaseAccount, LiveConfig, LiveTradingDisabled,
                                       LiveTradingRejected)
from data.collectors.coinbase_collector import CoinbaseCollector
from data.collectors.collector_base import Candle
from data.history import CandleHistoryStore, ExchangeHistory
from data.backtesting import BacktestEngine
from strategies.strategy_suite import STRATEGY_CATALOG, generate_signal


ROOT = Path(__file__).resolve().parents[2]
DB_FILE = Path(os.environ.get("SURGE_DB_PATH", str(ROOT / "data" / "surge.sqlite3")))
ACCOUNT_ID = "paper_default"
STARTING_CASH = 10_000.0
MAX_POSITION_PCT = 5.0
MAX_DAILY_LOSS_PCT = 3.0
MAX_OPEN_POSITIONS = 3
FEE_RATE = 0.001
FRONTEND = ROOT / "dashboard" / "frontend" / "index.html"

app = FastAPI(title="SURGE Crypto Trading Platform", version="1.1.0")
logger = logging.getLogger("surge.collector")
live_armed_until = 0.0


def db() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db() -> None:
    with closing(db()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS candles (
          symbol TEXT NOT NULL, timeframe TEXT NOT NULL, ts_open INTEGER NOT NULL,
          open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
          volume REAL NOT NULL, source TEXT NOT NULL, fetched_at REAL NOT NULL,
          PRIMARY KEY(symbol, timeframe, ts_open)
        );
        CREATE TABLE IF NOT EXISTS signals (
          id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, strategy TEXT NOT NULL,
          direction TEXT NOT NULL, strength REAL NOT NULL, reference_price REAL NOT NULL,
          created_at REAL NOT NULL, details_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
          id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
          side TEXT NOT NULL CHECK(side IN ('buy','sell')), qty REAL NOT NULL,
          price REAL NOT NULL, fee REAL NOT NULL, realized_pnl REAL NOT NULL DEFAULT 0,
          strategy TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS positions (
          account_id TEXT NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL,
          avg_price REAL NOT NULL, updated_at REAL NOT NULL,
          PRIMARY KEY(account_id, symbol)
        );
        CREATE TABLE IF NOT EXISTS account_state (
          account_id TEXT PRIMARY KEY, cash REAL NOT NULL, starting_equity REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runtime_state (
          id INTEGER PRIMARY KEY CHECK(id=1), kill_switch INTEGER NOT NULL DEFAULT 1,
          armed_until REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS ai_decisions (
          id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, action TEXT NOT NULL,
          score REAL NOT NULL, created_at REAL NOT NULL, details_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS automation_state (
          id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
          last_candle_ts INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS live_orders (
          id INTEGER PRIMARY KEY, exchange TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
          quote_amount REAL NOT NULL, status TEXT NOT NULL, exchange_order_id TEXT,
          created_at REAL NOT NULL, response_json TEXT NOT NULL
        );
        INSERT OR IGNORE INTO account_state(account_id,cash,starting_equity)
          VALUES('paper_default',10000,10000);
        INSERT OR IGNORE INTO runtime_state(id,kill_switch,armed_until) VALUES(1,1,0);
        INSERT OR IGNORE INTO automation_state(id,enabled,last_candle_ts) VALUES(1,0,0);
        CREATE INDEX IF NOT EXISTS idx_candles_recent ON candles(symbol,timeframe,ts_open DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_recent ON orders(created_at DESC);
        """)
        con.commit()


init_db()


def store_candles(symbol: str, timeframe: str, candles: list, source: str = "coinbase") -> None:
    fetched = time.time()
    with closing(db()) as con:
        con.executemany("INSERT INTO candles(symbol,timeframe,ts_open,open,high,low,close,volume,source,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,timeframe,ts_open) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,source=excluded.source,fetched_at=excluded.fetched_at",
                        [(symbol, timeframe, c.ts_open, c.open, c.high, c.low, c.close, c.volume, source, fetched) for c in candles])
        con.commit()
    # Keep the exchange-aware history store as the canonical backtest source.
    CandleHistoryStore(DB_FILE).save(source, symbol, timeframe, candles)


async def collector_loop() -> None:
    interval = max(60, int(os.environ.get("SURGE_COLLECTION_SECONDS", "300")))
    while True:
        try:
            end = int(time.time())
            start = end - 100 * CoinbaseCollector.TIMEFRAME_MAP["1h"]
            candles = await asyncio.to_thread(CoinbaseCollector().fetch_candles, "BTC-USD", "1h", start, end)
            if candles:
                await asyncio.to_thread(store_candles, "BTC-USD", "1h", candles)
                app.state.market_data = {"last_success": time.time(), "candles": len(candles), "error": None}
                await asyncio.to_thread(run_automation_for_new_candle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Periodic Coinbase refresh failed: %s", exc)
            app.state.market_data = {"last_success": getattr(app.state, "market_data", {}).get("last_success"),
                                     "candles": 0, "error": str(exc)}
        await asyncio.sleep(interval)


@app.on_event("startup")
async def start_collector() -> None:
    app.state.market_data = {"last_success": None, "candles": 0, "error": None}
    app.state.collector_task = asyncio.create_task(collector_loop())


@app.on_event("shutdown")
async def stop_collector() -> None:
    task = getattr(app.state, "collector_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class OrderRequest(BaseModel):
    symbol: str = Field(default="BTC-USD", pattern=r"^[A-Z0-9]+-[A-Z0-9]+$")
    side: Literal["buy", "sell"]
    usd_amount: float = Field(default=100, gt=0, le=500)


class ArmRequest(BaseModel):
    duration_minutes: int = Field(default=30, ge=1, le=60)


class AutomationRequest(BaseModel):
    enabled: bool


class BacktestRequest(BaseModel):
    exchange: str = "coinbase"
    symbol: str = "BTC-USD"
    timeframe: str = "1h"
    strategy: str = "ema_12_26_crossover"
    start_ms: int | None = None
    end_ms: int | None = None
    initial_cash: float = Field(default=10_000, gt=0)
    fee_rate: float = Field(default=FEE_RATE, ge=0, lt=1)


class LiveArmRequest(BaseModel):
    acknowledgement: str


class LiveOrderRequest(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]+/[A-Z0-9]+$")
    side: Literal["buy", "sell"]
    quote_amount: float = Field(gt=0, le=500)


def live_config() -> LiveConfig:
    return LiveConfig.from_env()


def latest_price(con: sqlite3.Connection, symbol: str) -> float | None:
    row = con.execute("SELECT close FROM candles WHERE symbol=? AND timeframe='1h' ORDER BY ts_open DESC LIMIT 1", (symbol,)).fetchone()
    return float(row[0]) if row else None


def gate_state(con: sqlite3.Connection) -> dict:
    state = con.execute("SELECT kill_switch,armed_until FROM runtime_state WHERE id=1").fetchone()
    remaining = max(0, int(state["armed_until"] - time.time()))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    realized = con.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM orders WHERE account_id=? AND side='sell' AND date(created_at,'unixepoch')=?", (ACCOUNT_ID, today)).fetchone()[0]
    risk = RiskGates({"max_position_pct": MAX_POSITION_PCT, "max_daily_loss_pct": MAX_DAILY_LOSS_PCT,
                      "max_open_positions": MAX_OPEN_POSITIONS})
    allowed, reason, details = risk.evaluate({"kill_switch": bool(state["kill_switch"]), "armed_until": state["armed_until"],
        "now": time.time(), "starting_equity": STARTING_CASH, "daily_realized_pnl": realized})
    return {"allowed": allowed, "reasons": [] if allowed else reason.split("; "), "kill_switch": bool(state["kill_switch"]),
            "armed_seconds_remaining": remaining, "max_position_pct": MAX_POSITION_PCT,
            "max_daily_loss_pct": MAX_DAILY_LOSS_PCT, "max_open_positions": MAX_OPEN_POSITIONS, "details": details}


def execute_order(con: sqlite3.Connection, symbol: str, side: str, usd_amount: float, strategy: str) -> dict:
    gate = gate_state(con)
    if side == "buy" and not gate["allowed"]:
        raise HTTPException(status_code=409, detail={"message": "Order blocked by risk gates", "gates": gate})
    price = latest_price(con, symbol)
    if not price or price <= 0:
        raise HTTPException(status_code=409, detail="No stored Coinbase hourly price; refresh market data first")
    position = con.execute("SELECT qty,avg_price FROM positions WHERE account_id=? AND symbol=?", (ACCOUNT_ID, symbol)).fetchone()
    current_qty = float(position["qty"]) if position else 0.0
    state = con.execute("SELECT cash,starting_equity FROM account_state WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
    if side == "buy":
        max_usd = float(state["starting_equity"]) * MAX_POSITION_PCT / 100
        room = max(0.0, max_usd - current_qty * price)
        spend = min(usd_amount, room, float(state["cash"]) / (1 + FEE_RATE))
        if spend < 1.0:
            raise HTTPException(status_code=409, detail="Position limit or available cash prevents this buy")
        open_count = con.execute("SELECT COUNT(*) FROM positions WHERE account_id=? AND qty>0", (ACCOUNT_ID,)).fetchone()[0]
        control = con.execute("SELECT kill_switch,armed_until FROM runtime_state WHERE id=1").fetchone()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        realized_today = con.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM orders WHERE account_id=? AND side='sell' AND date(created_at,'unixepoch')=?", (ACCOUNT_ID,today)).fetchone()[0]
        allowed, reason, details = RiskGates({"max_position_pct":MAX_POSITION_PCT,"max_daily_loss_pct":MAX_DAILY_LOSS_PCT,"max_open_positions":MAX_OPEN_POSITIONS}).evaluate({
            "kill_switch":bool(control["kill_switch"]),"armed_until":control["armed_until"],"now":time.time(),
            "starting_equity":state["starting_equity"],"daily_realized_pnl":realized_today,"side":"buy",
            "is_new_position":current_qty<=0,"open_positions":open_count,"current_position_value":current_qty*price,
            "order_value":spend,"available_cash":state["cash"]})
        if not allowed:
            raise HTTPException(status_code=409,detail={"message":reason,"gates":details})
        qty = spend / price
        fee = spend * FEE_RATE
        new_qty = current_qty + qty
        avg = ((current_qty * float(position["avg_price"])) + spend + spend * FEE_RATE) / new_qty if current_qty > 0 else price * (1 + FEE_RATE)
        con.execute("UPDATE account_state SET cash=cash-? WHERE account_id=?", (spend + fee, ACCOUNT_ID))
        realized = 0.0
    else:
        if current_qty <= 0:
            raise HTTPException(status_code=409, detail="No long position to sell")
        qty = min(current_qty, usd_amount / price)
        proceeds = qty * price
        fee = proceeds * FEE_RATE
        realized = qty * (price - float(position["avg_price"])) - fee
        new_qty = current_qty - qty
        avg = float(position["avg_price"])
        con.execute("UPDATE account_state SET cash=cash+? WHERE account_id=?", (proceeds - fee, ACCOUNT_ID))
    con.execute("INSERT INTO positions(account_id,symbol,qty,avg_price,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(account_id,symbol) DO UPDATE SET qty=excluded.qty,avg_price=excluded.avg_price,updated_at=excluded.updated_at",
                (ACCOUNT_ID, symbol, new_qty, avg, time.time()))
    cur = con.execute("INSERT INTO orders(account_id,symbol,side,qty,price,fee,realized_pnl,strategy,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (ACCOUNT_ID, symbol, side, qty, price, fee, realized, strategy, time.time()))
    return {"id": cur.lastrowid, "account": ACCOUNT_ID, "symbol": symbol, "side": side, "qty": qty, "price": price, "fee": fee, "realized_pnl": realized, "status": "filled", "mode": "paper"}


def portfolio(con: sqlite3.Connection) -> dict:
    state = con.execute("SELECT cash,starting_equity FROM account_state WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
    positions = []
    market_value = 0.0
    unrealized = 0.0
    for row in con.execute("SELECT symbol,qty,avg_price FROM positions WHERE account_id=? AND qty>0", (ACCOUNT_ID,)):
        price = latest_price(con, row["symbol"]) or float(row["avg_price"])
        value = float(row["qty"]) * price
        pnl = float(row["qty"]) * (price - float(row["avg_price"]))
        positions.append({"symbol": row["symbol"], "qty": row["qty"], "avg_price": row["avg_price"], "last_price": price, "market_value": value, "unrealized_pnl": pnl})
        market_value += value
        unrealized += pnl
    realized = con.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM orders WHERE account_id=? AND side='sell'", (ACCOUNT_ID,)).fetchone()[0]
    return {"account": ACCOUNT_ID, "mode": "paper", "cash": state["cash"], "market_value": market_value,
            "equity": state["cash"] + market_value, "starting_equity": state["starting_equity"],
            "realized_pnl": realized, "unrealized_pnl": unrealized, "positions": positions}


@app.get("/")
def index():
    return FileResponse(FRONTEND)


@app.get("/health")
def health():
    try:
        with closing(db()) as con:
            con.execute("SELECT 1").fetchone()
        return {"status": "ok", "service": "SURGE", "mode": "paper", "database": "ok"}
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")


@app.get("/api/status")
def status():
    with closing(db()) as con:
        return {"mode": "paper", "gates": gate_state(con), "portfolio": portfolio(con),
                "data_source": "Coinbase, Binance, and Kraken public candle history", "market_collector": getattr(app.state, "market_data", {"last_success":None,"candles":0,"error":None})}


@app.get("/api/strategies")
def strategy_catalog():
    return STRATEGY_CATALOG


@app.post("/api/ai/analyze")
def ai_analyze(symbol: str = "BTC-USD", exchange: str = "coinbase"):
    """Run the onboard explainable ensemble on stored closed candles; never submits orders."""
    if exchange not in {"coinbase", "binance", "kraken"}:
        raise HTTPException(status_code=422, detail="Unsupported exchange")
    candles = CandleHistoryStore(DB_FILE).load(exchange, symbol, "1h")
    if not candles and exchange == "coinbase":
        with closing(db()) as con:
            rows = con.execute("SELECT ts_open,open,high,low,close,volume FROM candles WHERE symbol=? AND timeframe='1h' ORDER BY ts_open", (symbol,)).fetchall()
        candles = [Candle(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                          float(r["volume"]), int(r["ts_open"]), int(r["ts_open"]) + 3_600_000) for r in rows]
    candles = [c for c in candles if c.ts_close <= int(time.time() * 1000)]
    if len(candles) < 27:
        raise HTTPException(status_code=409, detail="Refresh at least 27 hourly candles first")
    with closing(db()) as con:
        try:
            decision = OnboardTradingAgent().analyze(
                symbol, [float(row.close) for row in candles],
                highs=[float(row.high) for row in candles],
                lows=[float(row.low) for row in candles],
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"AI analysis failed: {exc}")
        decision["exchange"] = exchange
        cur = con.execute("INSERT INTO ai_decisions(symbol,action,score,created_at,details_json) VALUES(?,?,?,?,?)",
                          (symbol, decision["action"], decision["score"], time.time(), json.dumps(decision)))
        con.commit()
        return {"id": cur.lastrowid, "exchange": exchange, "symbol": symbol, "price": candles[-1].close, **decision}


@app.get("/api/ai/decisions")
def ai_decisions(limit: int = 20):
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM ai_decisions ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 100),)).fetchall()
    return [{**dict(row), "details": json.loads(row["details_json"])} for row in rows]


def automation_state() -> dict:
    with closing(db()) as con:
        row = con.execute("SELECT enabled,last_candle_ts FROM automation_state WHERE id=1").fetchone()
        return {"enabled": bool(row["enabled"]), "mode": "paper", "last_candle_ts": row["last_candle_ts"]}


def run_automation_for_new_candle() -> dict | None:
    """Evaluate once per newly stored BTC hourly candle; paper orders remain gated."""
    with closing(db()) as con:
        state = con.execute("SELECT enabled,last_candle_ts FROM automation_state WHERE id=1").fetchone()
        if not state["enabled"]:
            return None
        latest = con.execute("SELECT MAX(ts_open) FROM candles WHERE symbol='BTC-USD' AND timeframe='1h' AND ts_open+3600000<=?", (int(time.time() * 1000),)).fetchone()[0]
        if latest is None or int(latest) <= int(state["last_candle_ts"]):
            return None
        con.execute("UPDATE automation_state SET last_candle_ts=? WHERE id=1", (latest,))
        rows = con.execute("SELECT high,low,close FROM candles WHERE symbol='BTC-USD' AND timeframe='1h' AND ts_open<=? ORDER BY ts_open", (latest,)).fetchall()
        if len(rows) < 27:
            con.commit()
            return None
        decision = OnboardTradingAgent().analyze("BTC-USD", [float(r["close"]) for r in rows],
            highs=[float(r["high"]) for r in rows], lows=[float(r["low"]) for r in rows])
        created = time.time()
        con.execute("INSERT INTO ai_decisions(symbol,action,score,created_at,details_json) VALUES(?,?,?,?,?)",
                    ("BTC-USD", decision["action"], decision["score"], created, json.dumps(decision)))
        order = None
        price = float(rows[-1]["close"])
        position = con.execute("SELECT qty FROM positions WHERE account_id=? AND symbol='BTC-USD'", (ACCOUNT_ID,)).fetchone()
        if decision["action"] == "buy" and (not position or float(position["qty"]) <= 0):
            gate = gate_state(con)
            if gate["allowed"]:
                order = execute_order(con, "BTC-USD", "buy", STARTING_CASH * MAX_POSITION_PCT / 100, "onboard_ai_ensemble")
            else:
                order = {"status": "blocked", "reason": gate["reasons"]}
        elif decision["action"] == "sell_or_reduce" and position and float(position["qty"]) > 0:
            order = execute_order(con, "BTC-USD", "sell", float(position["qty"]) * price, "onboard_ai_ensemble")
        con.commit()
        return {"symbol": "BTC-USD", "candle_ts": int(latest), "decision": decision, "order": order}


@app.get("/api/automation")
def get_automation():
    current = automation_state()
    with closing(db()) as con:
        current["gates"] = gate_state(con)
    return current


@app.post("/api/automation")
def set_automation(request: AutomationRequest):
    with closing(db()) as con:
        if request.enabled:
            gates = gate_state(con)
            if not gates["allowed"]:
                raise HTTPException(status_code=409, detail={"message": "Arm paper trading before enabling automated execution", "gates": gates})
            latest = con.execute("SELECT COALESCE(MAX(ts_open),0) FROM candles WHERE symbol='BTC-USD' AND timeframe='1h'").fetchone()[0]
            con.execute("UPDATE automation_state SET enabled=1,last_candle_ts=? WHERE id=1", (latest,))
        else:
            con.execute("UPDATE automation_state SET enabled=0 WHERE id=1")
        con.commit()
    return automation_state()


@app.get("/api/live/status")
def live_status():
    try:
        config = live_config()
        configured = bool(config.enabled and config.armed and config.api_key and config.api_secret and config.max_order_quote > 0)
        user_armed = configured and live_armed_until > time.time()
        return {"configured": configured, "enabled": config.enabled, "exchange": config.exchange_id,
                "user_armed": user_armed, "armed_seconds_remaining": max(0, int(live_armed_until-time.time())) if user_armed else 0,
                "max_order_quote": config.max_order_quote if configured else 0,
                "mode": "live" if user_armed else "paper"}
    except (LiveTradingDisabled, ValueError):
        return {"configured": False, "enabled": False, "exchange": None, "user_armed": False,
                "armed_seconds_remaining": 0, "max_order_quote": 0, "mode": "paper"}


@app.post("/api/live/arm")
def arm_live(request: LiveArmRequest):
    global live_armed_until
    if request.acknowledgement != "I UNDERSTAND THIS SENDS REAL ORDERS":
        raise HTTPException(status_code=400, detail="Explicit live-order acknowledgement is required")
    try:
        config = live_config()
        # The server-side enable flag and exact environment latch are separate from this UI arm.
        LiveCoinbaseAccount(config, user_armed=True, client=object())
    except (LiveTradingDisabled, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    live_armed_until = time.time() + 300
    return {"status": "live_armed", "expires_at": live_armed_until,
            "warning": "Real exchange orders can now be submitted for five minutes."}


@app.post("/api/live/disarm")
def disarm_live():
    global live_armed_until
    live_armed_until = 0
    return {"status": "disarmed", "mode": "paper"}


@app.post("/api/live/orders")
def place_live_order(request: LiveOrderRequest):
    if live_armed_until <= time.time():
        raise HTTPException(status_code=409, detail="Live trading is not user-armed")
    try:
        config = live_config()
        account = LiveCoinbaseAccount(config, user_armed=True)
        expected_quote = "USDT" if config.exchange_id == "binance" else "USD"
        if request.symbol.split("/")[1] != expected_quote:
            raise LiveTradingRejected(f"{config.exchange_id} live symbols must use {expected_quote} quote currency")
        account.client.load_markets()
        reference_price = float(account.client.fetch_ticker(request.symbol).get("last") or 0)
        if reference_price <= 0:
            raise LiveTradingRejected("Exchange returned no usable market price")
        balance = account.get_balances()
        base, quote = request.symbol.split("/")
        free = balance.get("free", {})
        available = float(free.get(quote if request.side == "buy" else base, 0) or 0)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with closing(db()) as con:
            daily_total = float(con.execute("SELECT COALESCE(SUM(quote_amount),0) FROM live_orders WHERE date(created_at,'unixepoch')=?", (day,)).fetchone()[0])
        daily_cap = config.max_order_quote * 5
        if daily_total + request.quote_amount > daily_cap:
            raise LiveTradingRejected("Order exceeds the daily live quote-volume cap")
        if request.quote_amount > config.max_order_quote:
            raise LiveTradingRejected("Order exceeds the configured per-order cap")
        if not (request.quote_amount <= available if request.side == "buy"
                else request.quote_amount / reference_price <= available):
            raise LiveTradingRejected("Order exceeds the configured cap or available exchange balance")
        def risk_check(context):
            if context["order_value"] > config.max_order_quote or daily_total + context["order_value"] > daily_cap:
                return False
            if request.side == "buy":
                # Leave a small quote balance reserve for exchange fees/rounding.
                return context["order_value"] <= available * 0.995
            return context["amount"] <= available
        order = account.place_market_order(request.symbol, request.side, request.quote_amount,
            reference_price, risk_check=risk_check)
        with closing(db()) as con:
            cur = con.execute("INSERT INTO live_orders(exchange,symbol,side,quote_amount,status,exchange_order_id,created_at,response_json) VALUES(?,?,?,?,?,?,?,?)",
                (config.exchange_id, request.symbol, request.side, request.quote_amount,
                 str(order.get("status", "submitted")), str(order.get("id", "")), time.time(), json.dumps(order, default=str)))
            con.commit()
        return {"id": cur.lastrowid, "exchange": config.exchange_id, "mode": "live", "order": order}
    except (LiveTradingDisabled, LiveTradingRejected, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.exception("Live order request failed")
        raise HTTPException(status_code=502, detail=f"Exchange rejected or failed the live order request: {exc}")


@app.get("/api/live/orders")
def live_order_history(limit: int = 20):
    with closing(db()) as con:
        rows = con.execute("SELECT id,exchange,symbol,side,quote_amount,status,exchange_order_id,created_at,response_json FROM live_orders ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 100),)).fetchall()
    return [{**dict(row), "response": json.loads(row["response_json"])} for row in rows]


@app.post("/api/backtest")
def run_backtest(request: BacktestRequest):
    if request.exchange not in {"coinbase", "binance", "kraken"}:
        raise HTTPException(status_code=422, detail="Unsupported exchange")
    try:
        candles = CandleHistoryStore(DB_FILE).load(request.exchange, request.symbol, request.timeframe,
                                                  request.start_ms, request.end_ms)
        if not candles and request.exchange == "coinbase":
            with closing(db()) as con:
                query = "SELECT ts_open,open,high,low,close,volume FROM candles WHERE symbol=? AND timeframe=?"
                params: list = [request.symbol, request.timeframe]
                if request.start_ms is not None:
                    query += " AND ts_open>=?"; params.append(request.start_ms)
                if request.end_ms is not None:
                    query += " AND ts_open<?"; params.append(request.end_ms)
                query += " ORDER BY ts_open"
                step_ms = {"1m":60_000,"5m":300_000,"15m":900_000,"1h":3_600_000,"4h":14_400_000,"1d":86_400_000}.get(request.timeframe, 3_600_000)
                candles = [Candle(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                                  float(r["volume"]), int(r["ts_open"]), int(r["ts_open"]) + step_ms)
                           for r in con.execute(query, params).fetchall()]
        candles = [c for c in candles if c.ts_close <= int(time.time() * 1000)]
        if not candles:
            raise HTTPException(status_code=409, detail="No stored candle history matches this backtest")
        if request.strategy not in {entry["name"] for entry in STRATEGY_CATALOG}:
            raise HTTPException(status_code=422, detail="Unknown strategy")

        def target_long(prefix):
            try:
                signal = generate_signal(request.strategy, request.symbol,
                    [float(c.close) for c in prefix], highs=[float(c.high) for c in prefix],
                    lows=[float(c.low) for c in prefix])
                return signal["direction"] == "long"
            except (ValueError, TypeError):
                return False

        result = BacktestEngine(request.initial_cash, request.fee_rate).run(candles, target_long, request.symbol)
        return {"exchange": request.exchange, "symbol": request.symbol, "timeframe": request.timeframe,
                "strategy": request.strategy, "result": result.to_dict(),
                "caveat": "Historical simulation only; fees and next-bar fills are modeled, liquidity, spread and slippage are not."}
    except HTTPException:
        raise
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Backtest failed: {exc}")


@app.post("/api/market/refresh")
def refresh_market(symbol: str = "BTC-USD", timeframe: str = "1h", limit: int = 100,
                   exchange: str = "coinbase"):
    if not symbol or not symbol.replace("-", "").isalnum() or exchange not in {"coinbase", "binance", "kraken"}:
        raise HTTPException(status_code=422, detail="Unsupported symbol or timeframe")
    if timeframe not in {"1m", "5m", "15m", "1h", "4h", "1d"}:
        raise HTTPException(status_code=422, detail="Unsupported symbol or timeframe")
    limit = max(26, min(limit, 1000))
    end = int(time.time())
    seconds = {"1m":60,"5m":300,"15m":900,"1h":3600,"4h":14400,"1d":86400}[timeframe]
    start_ms, end_ms = (end - limit * seconds) * 1000, end * 1000
    try:
        candles = ExchangeHistory().fetch(exchange, symbol, timeframe, start_ms, end_ms,
                                          CandleHistoryStore(DB_FILE))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{exchange} request failed: {exc}")
    if not candles:
        raise HTTPException(status_code=502, detail=f"{exchange} returned no candles")
    if exchange == "coinbase":
        store_candles(symbol, timeframe, candles)
    candles.sort(key=lambda c: c.ts_open)
    return {"exchange": exchange, "symbol": symbol, "timeframe": timeframe, "count": len(candles), "source": exchange, "candles": [c.__dict__ for c in candles]}


@app.get("/api/candles")
def get_candles(symbol: str = "BTC-USD", timeframe: str = "1h", limit: int = 100,
                exchange: str = "coinbase"):
    if exchange not in {"coinbase", "binance", "kraken"}:
        raise HTTPException(status_code=422, detail="Unsupported exchange")
    candles = CandleHistoryStore(DB_FILE).load(exchange, symbol, timeframe)
    if not candles and exchange == "coinbase":
        with closing(db()) as con:
            rows = con.execute("SELECT ts_open,open,high,low,close,volume FROM candles WHERE symbol=? AND timeframe=? ORDER BY ts_open DESC LIMIT ?", (symbol, timeframe, min(max(limit, 1), 1000))).fetchall()
        return list(reversed([dict(row) for row in rows]))
    candles = candles[-min(max(limit, 1), 1000):]
    return [{"ts_open": c.ts_open, "open": c.open, "high": c.high, "low": c.low,
             "close": c.close, "volume": c.volume, "ts_close": c.ts_close} for c in candles]


@app.get("/api/signals")
def signals(limit: int = 20):
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 100),)).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/strategy/run")
def run_strategy(symbol: str = "BTC-USD", strategy: str = "ema_12_26_crossover", execute: bool = False,
                 exchange: str = "coinbase"):
    if exchange not in {"coinbase", "binance", "kraken"}:
        raise HTTPException(status_code=422, detail="Unsupported exchange")
    candle_rows = CandleHistoryStore(DB_FILE).load(exchange, symbol, "1h")
    if not candle_rows and exchange == "coinbase":
        with closing(db()) as con:
            raw = con.execute("SELECT ts_open,open,high,low,close,volume FROM candles WHERE symbol=? AND timeframe='1h' ORDER BY ts_open", (symbol,)).fetchall()
        candle_rows = [Candle(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                              float(r["volume"]), int(r["ts_open"]), int(r["ts_open"]) + 3_600_000) for r in raw]
    candle_rows = [c for c in candle_rows if c.ts_close <= int(time.time() * 1000)]
    if len(candle_rows) < 27:
        raise HTTPException(status_code=409, detail="Refresh at least 27 hourly candles first")
    with closing(db()) as con:
        closes = [float(row.close) for row in candle_rows]
        highs = [float(row.high) for row in candle_rows]
        lows = [float(row.low) for row in candle_rows]
        try:
            decision = generate_signal(strategy, symbol, closes, highs=highs, lows=lows)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        price = closes[-1]
        now = time.time()
        cur = con.execute("INSERT INTO signals(symbol,strategy,direction,strength,reference_price,created_at,details_json) VALUES(?,?,?,?,?,?,?)",
                          (symbol, strategy, decision["direction"], decision["strength"], price, now, json.dumps(decision)))
        result = {"signal_id": cur.lastrowid, "symbol": symbol, "strategy": strategy, "price": price, **decision, "order": None}
        if execute and exchange == "coinbase" and decision["direction"] == "long":
            gate = gate_state(con)
            if gate["allowed"]:
                result["order"] = execute_order(con, symbol, "buy", STARTING_CASH * MAX_POSITION_PCT / 100, "ema_12_26_crossover")
            else:
                result["order"] = {"status": "blocked", "reason": gate["reasons"]}
        elif execute and exchange == "coinbase" and decision["direction"] == "exit":
            position = con.execute("SELECT qty FROM positions WHERE account_id=? AND symbol=?", (ACCOUNT_ID, symbol)).fetchone()
            if position and position["qty"] > 0:
                result["order"] = execute_order(con, symbol, "sell", float(position["qty"]) * price, "ema_12_26_crossover")
        con.commit()
        return result


@app.post("/api/orders")
def place_order(request: OrderRequest):
    with closing(db()) as con:
        result = execute_order(con, request.symbol, request.side, request.usd_amount, "manual_paper")
        con.commit()
        return result


@app.post("/api/control/arm")
def arm(request: ArmRequest):
    with closing(db()) as con:
        con.execute("UPDATE runtime_state SET kill_switch=0,armed_until=? WHERE id=1", (time.time() + request.duration_minutes * 60,))
        con.commit()
        return {"status": "armed", "mode": "paper", "expires_at": time.time() + request.duration_minutes * 60}


@app.post("/api/control/kill")
def kill():
    with closing(db()) as con:
        con.execute("UPDATE runtime_state SET kill_switch=1,armed_until=0 WHERE id=1")
        con.commit()
        return {"status": "killed", "mode": "paper"}


@app.post("/api/control/flatten")
def flatten():
    with closing(db()) as con:
        # Flatten remains available while the new-order kill switch is engaged.
        rows = con.execute("SELECT symbol,qty FROM positions WHERE account_id=? AND qty>0", (ACCOUNT_ID,)).fetchall()
        results = []
        for row in rows:
            price = latest_price(con, row["symbol"])
            if not price:
                continue
            state = con.execute("SELECT cash FROM account_state WHERE account_id=?", (ACCOUNT_ID,)).fetchone()
            qty = float(row["qty"])
            avg = con.execute("SELECT avg_price FROM positions WHERE account_id=? AND symbol=?", (ACCOUNT_ID, row["symbol"])).fetchone()[0]
            proceeds = qty * price
            fee = proceeds * FEE_RATE
            pnl = qty * (price - avg) - fee
            con.execute("UPDATE account_state SET cash=cash+? WHERE account_id=?", (proceeds-fee, ACCOUNT_ID))
            con.execute("UPDATE positions SET qty=0,updated_at=? WHERE account_id=? AND symbol=?", (time.time(), ACCOUNT_ID, row["symbol"]))
            cur = con.execute("INSERT INTO orders(account_id,symbol,side,qty,price,fee,realized_pnl,strategy,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (ACCOUNT_ID,row["symbol"],"sell",qty,price,fee,pnl,"flatten",time.time()))
            results.append({"id":cur.lastrowid,"symbol":row["symbol"],"qty":qty,"price":price,"realized_pnl":pnl,"status":"filled"})
        con.commit()
        return {"flattened": results}


@app.get("/api/orders")
def order_history(limit: int = 50):
    with closing(db()) as con:
        return [dict(row) for row in con.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 200),))]


@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            with closing(db()) as con:
                price = latest_price(con, "BTC-USD")
            await websocket.send_json({"type":"market","symbol":"BTC-USD","price":price,"timestamp":time.time()})
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        return


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard.backend.app:app", host="127.0.0.1", port=int(os.environ.get("PORT", "8000")), log_level="info")
