"""SURGE local paper-trading API and dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import stat
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from dashboard.backend.authority_client import (allowed_host, check_local_post, gateway_for,
    local_csrf_token, set_session_cookie, unavailable)

from bot.risk_gates import RiskGates
from bot.onboard_ai import OnboardTradingAgent
from data.call_log import DataCallLog
from data.public_metrics import fetch_coinbase_book, fetch_global, fetch_listed_markets
from data.collectors.coinbase_collector import CoinbaseCollector
from data.collectors.collector_base import Candle
from data.history import CandleHistoryStore, ExchangeHistory
from research.autonomous.status_cards import candidate_cards
from data.backtesting import BacktestEngine
from strategies.strategy_suite import STRATEGY_CATALOG, STRATEGIES, generate_signal


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

_MANAGED_MUTATIONS = frozenset({"/api/orders", "/api/control/arm", "/api/control/disarm", "/api/control/kill",
    "/api/control/flatten", "/api/live/arm", "/api/live/disarm", "/api/live/orders", "/api/automation"})
# Session bootstrap performs its own Origin and one-time-token checks.
_SELF_CHECKED_POSTS = frozenset({"/api/session/bootstrap"})
LIVE_ACKNOWLEDGEMENT = "I UNDERSTAND THIS SENDS REAL ORDERS"


def _post_paths() -> frozenset:
    return frozenset(route.path for route in app.routes if "POST" in (getattr(route, "methods", None) or ()))


@app.middleware("http")
async def authenticate_managed_mutations(request: Request, call_next):
    execute = request.query_params.get('execute', '').lower() in {'true', '1', 'yes', 'on'}
    managed = request.method == 'POST' and (request.url.path in _MANAGED_MUTATIONS
        or request.url.path == '/api/strategy/run' and execute)
    try:
        if managed:
            gateway_for(request, mutation=True)
        elif (request.method == 'POST' and request.url.path not in _SELF_CHECKED_POSTS
                and request.url.path in _post_paths()):
            # Every other POST mutation needs loopback Host, same Origin and CSRF.
            check_local_post(request)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})
    return await call_next(request)


@app.get('/api/csrf')
def csrf_token(request: Request):
    """Per-process token for non-managed POSTs; unreadable cross-origin."""
    if not allowed_host(request):
        raise HTTPException(403, 'Host rejected')
    return {'csrf_token': local_csrf_token(request.app)}


class BootstrapRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)


@app.post('/api/session/bootstrap')
def bootstrap_session(request: Request, body: BootstrapRequest):
    """Exchange a one-time launcher token for the HttpOnly SameSite=Strict session."""
    if not allowed_host(request):
        raise HTTPException(403, 'Host rejected')
    gateway = getattr(request.app.state, 'authority_gateway', None)
    if gateway is None:
        raise HTTPException(503, 'Authority gateway unavailable')
    session = gateway.bootstrap(request, body.token)
    result = JSONResponse({'actor': session.actor, 'role': session.role,
        'csrf_token': session.csrf_token, 'control_epoch': gateway.control_epoch})
    set_session_cookie(result, session)
    return result


@app.get('/api/session')
def browser_session(request: Request):
    gateway = gateway_for(request)
    session = gateway.authenticate(request)
    result = JSONResponse({'actor': session.actor, 'role': session.role,
        'csrf_token': session.csrf_token, 'control_epoch': gateway.control_epoch})
    set_session_cookie(result, session)
    return result


@app.get('/api/control/status')
def authority_status(request: Request):
    return gateway_for(request).forward(request, 'status')



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
        CREATE TABLE IF NOT EXISTS strategy_queue (
          id INTEGER PRIMARY KEY, strategy TEXT NOT NULL, symbol TEXT NOT NULL,
          timeframe TEXT NOT NULL, created_at REAL NOT NULL
        );
        INSERT OR IGNORE INTO account_state(account_id,cash,starting_equity)
          VALUES('paper_default',10000,10000);
        INSERT OR IGNORE INTO runtime_state(id,kill_switch,armed_until) VALUES(1,1,0);
        INSERT OR IGNORE INTO automation_state(id,enabled,last_candle_ts) VALUES(1,0,0);
        CREATE INDEX IF NOT EXISTS idx_candles_recent ON candles(symbol,timeframe,ts_open DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_recent ON orders(created_at DESC);
        """)
        con.commit()




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
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Periodic Coinbase refresh failed: %s", exc)
            app.state.market_data = {"last_success": getattr(app.state, "market_data", {}).get("last_success"),
                                     "candles": 0, "error": str(exc)}
        await asyncio.sleep(interval)


@app.on_event("startup")
async def start_collector() -> None:
    provider_names = {"SURGE_COINBASE_API_KEY", "SURGE_COINBASE_API_SECRET",
        "SURGE_BINANCE_API_KEY", "SURGE_BINANCE_API_SECRET", "TYPESAFE_API_KEY", "SURGE_BRAIN_API_KEY"}
    if any(os.environ.get(name) for name in provider_names):
        raise RuntimeError("Dashboard must start without provider credentials")
    init_db()
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
    release_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="operator arm", min_length=1, max_length=256)


class DisarmRequest(BaseModel):
    reason: str = Field(default="operator disarm", min_length=1, max_length=256)


class FlattenRequest(BaseModel):
    reason: str = Field(default="operator flatten", min_length=1, max_length=256)
    incident_id: str | None = Field(default=None, min_length=1, max_length=128)


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
    release_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class LiveOrderRequest(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]+/[A-Z0-9]+$")
    side: Literal["buy", "sell"]
    quote_amount: float = Field(gt=0, le=500)


def live_config():
    raise HTTPException(409, "Venue configuration belongs to the authority")


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


def _refuse_dashboard_fill(route: str) -> None:
    raise HTTPException(status_code=409, detail="Paper fills are accepted only by the autonomous paper authority")


def execute_order(con: sqlite3.Connection, symbol: str, side: str, usd_amount: float, strategy: str) -> dict:
    _refuse_dashboard_fill("execute_order")


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


_OBS_STARTED = time.time()
_OBS_EVENTS: deque = deque(maxlen=50)
_OBS_ROUTES: dict = {}


@app.middleware("http")
async def observe_requests(request: Request, call_next):
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        path = request.url.path
        if path != "/api/observability":
            ms = (time.perf_counter() - started) * 1000
            stat_row = _OBS_ROUTES.setdefault(path, {"count": 0, "errors": 0, "total_ms": 0.0, "max_ms": 0.0})
            stat_row["count"] += 1
            stat_row["errors"] += status_code >= 400
            stat_row["total_ms"] += ms
            stat_row["max_ms"] = max(stat_row["max_ms"], ms)
            if status_code >= 400 or request.method != "GET":
                _OBS_EVENTS.appendleft({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "method": request.method, "path": path, "status": status_code, "ms": round(ms, 1)})


@app.get("/api/observability")
def observability():
    """Plain-language health, traffic and recent-event snapshot for the dashboard."""
    db_ok, candles = True, 0
    try:
        with closing(db()) as con:
            con.execute("SELECT 1").fetchone()
            try:
                candles = con.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
            except sqlite3.OperationalError:
                candles = 0
    except sqlite3.Error:
        db_ok = False
    routes = [{"path": path, "count": r["count"], "errors": r["errors"],
               "avg_ms": round(r["total_ms"] / r["count"], 1), "max_ms": round(r["max_ms"], 1)}
              for path, r in _OBS_ROUTES.items()]
    routes.sort(key=lambda r: r["count"], reverse=True)
    total = sum(r["count"] for r in routes)
    errors = sum(r["errors"] for r in routes)
    collector = getattr(app.state, "market_data", {"last_success": None, "candles": 0, "error": None})
    return {"uptime_seconds": int(time.time() - _OBS_STARTED), "database": "ok" if db_ok else "down",
            "stored_candles": candles, "collector": collector, "requests": total, "errors": errors,
            "routes": routes[:10], "events": list(_OBS_EVENTS)[:20], "mode": "paper"}


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
        return {"mode": "paper", "accounting_source": "legacy_display_only",
                "authority_status_url": "/api/control/status", "gates": gate_state(con), "portfolio": portfolio(con),
                "data_source": "Coinbase, Binance, and Kraken public candle history", "market_collector": getattr(app.state, "market_data", {"last_success":None,"candles":0,"error":None})}


@app.get("/api/autonomous/runtime")
def autonomous_runtime():
    """Read-only projection of the paper runtime's status.json. Never arms, kills or submits."""
    # HANDOFF(claude-opus-5.5 2026-10-17): operator latch clear/kill must go through bot.autonomous.control, not here.
    path = _runtime_dir() / "status.json"
    if not path.exists():
        return {"available": False, "detail": "Paper runtime has not written status yet. Start: python -m bot.autonomous.runtime --paper"}
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"Runtime status unreadable: {type(exc).__name__}")
    age_s = None
    if isinstance(payload.get("at_us"), int):
        age_s = round(time.time() - payload["at_us"] / 1_000_000, 1)
    return {"available": True, "age_seconds": age_s, "stale": age_s is None or age_s > 180, "status": payload,
            "ledger": _ledger_projection(payload.get("ledger"))}


MAX_PROPOSAL_FILE_BYTES = 2_097_152
_PROPOSAL_FILES = 32  # newest day files (rollover parts included) that are read
_PROPOSAL_SCAN_LIMIT = 2_000  # directory entries examined before sorting
_PROPOSAL_LIMIT = 50


def _runtime_dir() -> Path:
    return Path(os.environ.get("AUTONOMOUS_RUNTIME_DIR", ROOT / "data" / "autonomous" / "runtime"))


def _utc(us) -> str | None:
    if type(us) is not int or us < 0:
        return None
    return datetime.fromtimestamp(us / 1_000_000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger_projection(ledger) -> dict | None:
    """Ledger readiness from status.json (e.g. LEDGER_AWAITING_MIDNIGHT_MARK with next_eligible_us)."""
    if not isinstance(ledger, dict):
        return None
    state = ledger.get("state") if isinstance(ledger.get("state"), str) else None
    nxt = ledger.get("next_eligible_us") if type(ledger.get("next_eligible_us")) is int else None
    return {"state": state, "ready": ledger.get("ready") is True, "next_eligible_us": nxt,
            "next_eligible_utc": _utc(nxt)}


@app.get("/api/autonomous/proposals")
def autonomous_proposals():
    """Pending BRAIN proposals from <runtime-dir>/proposals/*.json. Read-only; nothing here approves or applies.

    One bad file (oversized, symlinked, unparsable, wrong shape) is listed as unreadable and never fails the
    endpoint; the directory scan is bounded before sorting."""
    import re
    directory = Path(os.environ["AUTONOMOUS_PROPOSALS_DIR"]) if os.environ.get("AUTONOMOUS_PROPOSALS_DIR") \
        else _runtime_dir() / "proposals"
    pending, unreadable, counts = [], [], {"pending": 0, "provider_unavailable": 0}
    names, truncated = [], False
    pattern = re.compile(r"(\d{4}-\d{2}-\d{2})(?:-(\d{1,6}))?\.json")
    try:
        if directory.is_dir() and not directory.is_symlink():
            with os.scandir(directory) as entries:
                for count, entry in enumerate(entries):
                    if count >= _PROPOSAL_SCAN_LIMIT:
                        truncated = True
                        break
                    match = pattern.fullmatch(entry.name)
                    if match:
                        names.append(((match.group(1), int(match.group(2) or 0)), entry.name))
    except OSError:
        unreadable.append(directory.name)
    for _, name in sorted(names, reverse=True)[:_PROPOSAL_FILES]:
        path = directory / name
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PROPOSAL_FILE_BYTES:
                    raise ValueError("oversized or not a regular file")
                stored = json.loads(handle.read(MAX_PROPOSAL_FILE_BYTES + 1))
            entries = stored["proposals"]
            if not isinstance(entries, list):
                raise ValueError("invalid")
        except Exception:  # includes RecursionError, ELOOP for symlinks and wrong shapes
            unreadable.append(name)
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("status") == "PROVIDER_UNAVAILABLE":
                counts["provider_unavailable"] += 1
                continue
            if entry.get("status") != "PENDING_OPERATOR_REVIEW" or not isinstance(entry.get("proposal"), dict):
                continue
            counts["pending"] += 1
            proposal = entry["proposal"]
            usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else None
            pending.append({"day": pattern.fullmatch(name).group(1), "kind": entry.get("kind"),
                            "created_us": entry.get("created_us"),
                            "created_utc": _utc(entry.get("created_us")), "entry_hash": entry.get("entry_hash"),
                            "model_id": proposal.get("model_id"), "action": proposal.get("action"),
                            "finding": proposal.get("finding"), "hypothesis": proposal.get("hypothesis"),
                            "evidence_refs": proposal.get("evidence_refs"), "schema_diff": proposal.get("schema_diff"),
                            "usage": usage and {k: usage.get(k) for k in ("input_tokens", "output_tokens")},
                            "approved": False})
    pending.sort(key=lambda p: p["created_us"] if type(p["created_us"]) is int else -1, reverse=True)
    return {"pending": pending[:_PROPOSAL_LIMIT], "counts": counts, "unreadable": sorted(unreadable),
            "scan_truncated": truncated,
            "governance": "Proposals only: operator review required; nothing here approves or applies."}


_TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
_RUNTIME_FILE_COMMANDS = {
    "stop": ("STOP", "create"),
    "clear_stop": ("STOP", "remove"),
    "kill": ("KILL", "create"),
    "clear_kill": ("KILL", "remove"),
}


class StrategyQueueItem(BaseModel):
    strategy: str
    symbol: str = "BTC-USD"
    timeframe: str = "1h"


class RuntimeFileCommand(BaseModel):
    action: Literal["stop", "clear_stop", "kill", "clear_kill"]


def _product_symbol(symbol: str) -> str:
    parts = symbol.split("-")
    if len(parts) != 2 or not all(part.isalnum() and part for part in parts):
        raise HTTPException(status_code=422, detail="Product must look like BTC-USD")
    return symbol


def _load_closed_candles(exchange: str, symbol: str, timeframe: str) -> list:
    if exchange not in {"coinbase", "binance", "kraken", "okx"} or timeframe not in _TIMEFRAMES:
        raise HTTPException(status_code=422, detail="Unsupported exchange or timeframe")
    candle_rows = CandleHistoryStore(DB_FILE).load(exchange, symbol, timeframe)
    if not candle_rows and exchange == "coinbase":
        with closing(db()) as con:
            raw = con.execute(
                "SELECT ts_open,open,high,low,close,volume FROM candles WHERE symbol=? AND timeframe=? ORDER BY ts_open",
                (symbol, timeframe)).fetchall()
        width = _TIMEFRAMES[timeframe] * 1000
        candle_rows = [Candle(float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                              float(r["volume"]), int(r["ts_open"]), int(r["ts_open"]) + width) for r in raw]
    now_ms = int(time.time() * 1000)
    return [c for c in candle_rows if c.ts_close <= now_ms]


@app.get("/api/strategies")
def strategy_catalog():
    return STRATEGY_CATALOG


@app.get("/api/market/ticker")
def market_ticker(symbol: str = "BTC-USD"):
    """Public Coinbase ticker. The exchange trade time is the price timestamp."""
    import urllib.request
    symbol = _product_symbol(symbol)
    url = f"https://api.exchange.coinbase.com/products/{symbol}/ticker"
    request = urllib.request.Request(url, headers={"User-Agent": "SURGE-paper-platform/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Coinbase ticker failed: {type(exc).__name__}")
    try:
        price = float(payload["price"])
        bid = float(payload["bid"])
        ask = float(payload["ask"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=502, detail="Coinbase ticker missing price, bid, or ask")
    if not price > 0 or not bid > 0 or not ask > 0:
        raise HTTPException(status_code=502, detail="Coinbase ticker prices are not positive")
    return {"symbol": symbol, "price": price, "bid": bid, "ask": ask,
            "trade_time": payload.get("time"), "trade_size": payload.get("size"),
            "volume": payload.get("volume"), "source_url": url,
            "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "note": "Last trade and top of book from Coinbase. This is not a fill and not a regime."}


@app.get("/api/strategy/queue")
def strategy_queue():
    with closing(db()) as con:
        rows = con.execute("SELECT id,strategy,symbol,timeframe,created_at FROM strategy_queue ORDER BY id").fetchall()
    return {"items": [dict(row) for row in rows], "executes_orders": False,
            "catalog": [item["name"] for item in STRATEGY_CATALOG]}


@app.post("/api/strategy/queue")
def enqueue_strategy(item: StrategyQueueItem):
    if item.strategy not in STRATEGIES or item.timeframe not in _TIMEFRAMES:
        raise HTTPException(status_code=422, detail="Unknown strategy or timeframe")
    symbol = _product_symbol(item.symbol)
    with closing(db()) as con:
        cur = con.execute(
            "INSERT INTO strategy_queue(strategy,symbol,timeframe,created_at) VALUES(?,?,?,?)",
            (item.strategy, symbol, item.timeframe, time.time()))
        con.commit()
        row_id = cur.lastrowid
    return {"id": row_id, "strategy": item.strategy, "symbol": symbol, "timeframe": item.timeframe,
            "executes_orders": False}


@app.post("/api/strategy/queue/remove")
def dequeue_strategy(item_id: int):
    with closing(db()) as con:
        cur = con.execute("DELETE FROM strategy_queue WHERE id=?", (item_id,))
        con.commit()
    if cur.rowcount != 1:
        raise HTTPException(status_code=404, detail="Queue item not found")
    return {"removed": item_id, "executes_orders": False}


@app.post("/api/strategy/queue/run")
def run_strategy_queue():
    """Score every queued strategy on stored closed candles. Never submits an order."""
    with closing(db()) as con:
        queued = [dict(row) for row in con.execute(
            "SELECT id,strategy,symbol,timeframe FROM strategy_queue ORDER BY id")]
    triggers = []
    for item in queued:
        candles = _load_closed_candles("coinbase", item["symbol"], item["timeframe"])
        if len(candles) < 2:
            triggers.append({**item, "status": "insufficient_candles", "direction": None, "order": None})
            continue
        try:
            decision = generate_signal(item["strategy"], item["symbol"],
                                       [float(c.close) for c in candles],
                                       highs=[float(c.high) for c in candles],
                                       lows=[float(c.low) for c in candles])
        except (ValueError, TypeError) as exc:
            triggers.append({**item, "status": "refused", "detail": str(exc), "order": None})
            continue
        price = float(candles[-1].close)
        now = time.time()
        with closing(db()) as con:
            cur = con.execute(
                "INSERT INTO signals(symbol,strategy,direction,strength,reference_price,created_at,details_json) VALUES(?,?,?,?,?,?,?)",
                (item["symbol"], item["strategy"], decision["direction"], decision["strength"], price, now, json.dumps(decision)))
            con.commit()
            signal_id = cur.lastrowid
        active = decision["direction"] in {"long", "exit"}
        triggers.append({**item, "status": "trigger" if active else "watch", "signal_id": signal_id,
                         "price": price, "direction": decision["direction"], "strength": decision["strength"],
                         "reason": decision.get("reason"), "caveat": decision.get("caveat"),
                         "candle_close_ms": candles[-1].ts_close, "order": None})
    return {"triggers": triggers, "executes_orders": False,
            "note": "Signals and triggers only. The paper authority still owns every order."}


@app.get("/api/autonomous/commands")
def autonomous_commands():
    directory = _runtime_dir()
    return {"directory": str(directory), "executes_orders": False, "arms": False, "commands": [
        {"action": "stop", "file": "STOP", "effect": "Ask the paper runtime in this directory to exit. Does not start it and does not flatten."},
        {"action": "clear_stop", "file": "STOP", "effect": "Remove STOP. Does not start the process."},
        {"action": "kill", "file": "KILL", "effect": "Block every new paper order, including exits, until the file is removed."},
        {"action": "clear_kill", "file": "KILL", "effect": "Remove KILL. Does not arm paper and does not place an order."},
    ], "present": {"STOP": (directory / "STOP").exists(), "KILL": (directory / "KILL").exists()}}


@app.post("/api/autonomous/runtime/command")
def autonomous_runtime_command(command: RuntimeFileCommand):
    """File switch for the configured paper runtime directory. Cannot arm or submit."""
    name, effect = _RUNTIME_FILE_COMMANDS[command.action]
    directory = _runtime_dir().resolve()
    target = (directory / name).resolve()
    if target.parent != directory:
        raise HTTPException(status_code=400, detail="Runtime command path rejected")
    directory.mkdir(parents=True, exist_ok=True)
    if effect == "create":
        target.touch()
    elif target.exists():
        target.unlink()
    return {"action": command.action, "file": name, "present": target.exists(),
            "directory": str(directory), "executes_orders": False, "armed": False}


@app.post("/api/ai/analyze")
def ai_analyze(symbol: str = "BTC-USD", exchange: str = "coinbase"):
    """Run the onboard explainable ensemble on stored closed candles; never submits orders."""
    if exchange not in {"coinbase", "binance", "kraken", "okx"}:
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


def run_automation_for_new_candle() -> None:
    """Compatibility hook: only the isolated authority worker schedules decisions."""
    return None


@app.get("/api/automation")
def get_automation():
    return {"enabled": False, "mode": "paper", "last_candle_ts": None,
            "status": "authority_worker_required"}


@app.post("/api/automation")
def set_automation(request: AutomationRequest):
    return unavailable("Authority automation handler unavailable")


@app.get("/api/live/status")
def live_status():
    """Public availability only; provider credentials are never read by web routes."""
    return {"configured": False, "enabled": False, "exchange": None,
            "user_armed": False, "armed_seconds_remaining": 0, "max_order_quote": 0,
            "mode": "paper", "missing_requirements": ["Authenticated authority status required"]}


@app.post("/api/live/arm")
def arm_live(request: Request, body: LiveArmRequest):
    if body.acknowledgement != LIVE_ACKNOWLEDGEMENT:
        raise HTTPException(422, "Exact live acknowledgement required")
    return gateway_for(request, mutation=True).forward(request, 'arm',
        {'reason': 'operator live arm', 'release_hash': body.release_hash}, require_mode='live')


@app.post("/api/live/disarm")
def disarm_live(request: Request, body: DisarmRequest | None = None):
    reason = (body or DisarmRequest()).reason
    return gateway_for(request, mutation=True).forward(request, 'disarm', {'reason': reason},
        require_mode='live')


@app.post("/api/live/orders")
def place_live_order(request: LiveOrderRequest):
    return unavailable("Authority live order handler unavailable")


@app.get("/api/live/orders")
def live_order_history(limit: int = 20):
    with closing(db()) as con:
        rows = con.execute("SELECT id,exchange,symbol,side,quote_amount,status,exchange_order_id,created_at,response_json FROM live_orders ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 100),)).fetchall()
    return [{**dict(row), "response": json.loads(row["response_json"])} for row in rows]


@app.post("/api/backtest")
def run_backtest(request: BacktestRequest):
    if request.exchange not in {"coinbase", "binance", "kraken", "okx"}:
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
    if not symbol or not symbol.replace("-", "").isalnum() or exchange not in {"coinbase", "binance", "kraken", "okx"}:
        raise HTTPException(status_code=422, detail="Unsupported symbol or timeframe")
    if timeframe not in {"1m", "5m", "15m", "1h", "4h", "1d"}:
        raise HTTPException(status_code=422, detail="Unsupported symbol or timeframe")
    limit = max(26, min(limit, 1000))
    end = int(time.time())
    seconds = {"1m":60,"5m":300,"15m":900,"1h":3600,"4h":14400,"1d":86400}[timeframe]
    start_ms, end_ms = (end - limit * seconds) * 1000, end * 1000
    started = time.time()
    try:
        candles = ExchangeHistory().fetch(exchange, symbol, timeframe, start_ms, end_ms,
                                          CandleHistoryStore(DB_FILE))
        DataCallLog(DB_FILE).record(source=exchange, symbol=symbol, timeframe=timeframe,
                                    started_at=started, elapsed_ms=int((time.time() - started) * 1000),
                                    ok=bool(candles), row_count=len(candles), status_code=200 if candles else 204)
    except Exception as exc:
        DataCallLog(DB_FILE).record(source=exchange, symbol=symbol, timeframe=timeframe,
                                    started_at=started, elapsed_ms=int((time.time() - started) * 1000),
                                    ok=False, row_count=0, error=type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"{exchange} request failed: {exc}")
    if not candles:
        raise HTTPException(status_code=502, detail=f"{exchange} returned no candles")
    if exchange == "coinbase":
        store_candles(symbol, timeframe, candles)
    candles.sort(key=lambda c: c.ts_open)
    return {"exchange": exchange, "symbol": symbol, "timeframe": timeframe, "count": len(candles), "source": exchange, "candles": [c.__dict__ for c in candles]}


def _stored_close_change(con: sqlite3.Connection, symbol: str) -> dict:
    rows = con.execute(
        "SELECT ts_open, open, high, low, close FROM candles WHERE symbol=? AND timeframe='1h' ORDER BY ts_open",
        (symbol,)).fetchall()
    body = {"symbol": symbol, "present": bool(rows), "candles": len(rows), "closed_candles": 0,
            "open_dropped": 0, "consistent": False, "close_change": None, "first_open": None, "last_open": None}
    if len(rows) < 2:
        return body
    times = [row[0] for row in rows]
    if times != sorted(times) or len(set(times)) != len(times):
        return body
    for ts_open, open_, high, low, close in rows:
        if not (close > 0 and high >= max(open_, close) and low <= min(open_, close)):
            return body
    now_ms = int(time.time() * 1000)
    closed = [row for row in rows if row[0] + 3_600_000 <= now_ms]
    body["closed_candles"] = len(closed)
    body["open_dropped"] = len(rows) - len(closed)
    if len(closed) < 2:
        return body
    from decimal import Decimal
    first = Decimal(str(closed[0][4]))
    last = Decimal(str(closed[-1][4]))
    body.update({"consistent": True, "close_change": str((last - first) / first),
                 "first_open": closed[0][0], "last_open": closed[-1][0]})
    return body


@app.get("/api/market/trends")
def stored_trends():
    """Close-to-close change of stored hourly candles. This does not set a regime."""
    with closing(db()) as con:
        series = [_stored_close_change(con, symbol) for symbol in ("BTC-USD", "ETH-USD")]
    body = {"series": series, "regime_valid": False, "eligible": False, "dispatched": False}
    return body


@app.post("/api/market/global")
def refresh_global():
    try:
        return fetch_global(DataCallLog(DB_FILE))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"coingecko global request failed: {type(exc).__name__}")


@app.post("/api/market/dominance-history")
def refresh_dominance_history():
    """Seven-day BTC and ETH dominance changes. The shared slot stays invalid."""
    from data.public_metrics import fetch_dominance_history
    try:
        return fetch_dominance_history(DataCallLog(DB_FILE))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"dominance history request failed: {type(exc).__name__}")


@app.post("/api/market/stablecoins")
def refresh_stablecoins():
    from data.public_metrics import fetch_stablecoins
    try:
        return fetch_stablecoins(DataCallLog(DB_FILE))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"defillama stablecoin request failed: {type(exc).__name__}")


@app.post("/api/market/stablecoin-chart")
def refresh_stablecoin_chart():
    """Seven-day stablecoin chart change. It is not written into the state slot."""
    from data.public_metrics import fetch_stablecoin_chart
    try:
        return fetch_stablecoin_chart(DataCallLog(DB_FILE))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"defillama stablecoin chart request failed: {type(exc).__name__}")


@app.post("/api/market/circle")
def refresh_circle():
    """Issuer USDC disclosure. It does not set a regime or admit a research name."""
    from data.public_metrics import fetch_circle_transparency
    try:
        return fetch_circle_transparency(DataCallLog(DB_FILE))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"circle transparency request failed: {type(exc).__name__}")


@app.post("/api/market/fed")
def refresh_fed():
    """Cited FOMC statement and calendar. A rate decision does not set the crypto regime."""
    from data.public_metrics import fetch_fed_schedule
    try:
        return fetch_fed_schedule(DataCallLog(DB_FILE))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"federal reserve request failed: {type(exc).__name__}")


@app.post("/api/market/deribit")
def refresh_deribit():
    """Deribit public perpetual summaries. They do not set a regime or send an order."""
    from data.public_metrics import fetch_deribit_summaries
    try:
        return fetch_deribit_summaries(DataCallLog(DB_FILE))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"deribit summary request failed: {type(exc).__name__}")


@app.post("/api/market/funding")
def refresh_funding():
    from data.public_metrics import fetch_perp_quotes
    try:
        return fetch_perp_quotes(DataCallLog(DB_FILE))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"public funding request failed: {type(exc).__name__}")


@app.post("/api/market/swap-history")
def refresh_swap_history():
    """OKX public funding and open-interest history. The 1-hour funding slot stays invalid."""
    from data.public_metrics import fetch_swap_history
    try:
        return fetch_swap_history(DataCallLog(DB_FILE))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"public swap history request failed: {type(exc).__name__}")


@app.get("/api/data-calls")
def data_calls(limit: int = 50):
    """Recent market-data calls from the dashboard log and the autonomous book log."""
    limit = min(max(int(limit), 1), 200)
    surge = DataCallLog(DB_FILE).recent(limit)
    for row in surge:
        row["log"] = "surge"
    autonomous_path = ROOT / "data" / "autonomous" / "public_calls.sqlite3"
    autonomous = DataCallLog(autonomous_path).recent(limit) if autonomous_path.exists() else []
    for row in autonomous:
        row["log"] = "autonomous"
    merged = sorted([*surge, *autonomous], key=lambda row: row.get("started_at") or 0, reverse=True)[:limit]
    return merged


@app.post("/api/market/listings")
def refresh_listings():
    """Public market rows for the six researched names. None of them become eligible."""
    observed = fetch_listed_markets(DataCallLog(DB_FILE), ("AAVE", "HYPE", "PENDLE", "AERO", "LDO", "ONDO"))
    rows = []
    for row in observed.get("rows", []):
        shown = dict(row)
        shown["eligible"] = False
        rows.append(shown)
    body = {"source_url": observed.get("source_url"), "rows": rows,
            "unresolved": list(observed.get("unresolved", [])), "eligible": False}
    return body


@app.post("/api/market/book-hold")
def book_hold():
    """Hold one public BTC-USD top of book. This does not submit or mark it eligible."""
    from datetime import datetime
    from bot.autonomous.state import hold_public_top
    book = fetch_coinbase_book(DataCallLog(DB_FILE), "BTC-USD", level=2)
    stamp = book.get("exchange_time")
    if not isinstance(stamp, str) or "." not in stamp or not stamp.endswith("Z"):
        raise HTTPException(status_code=502, detail="Coinbase book time was not parsed")
    head, frac = stamp[:-1].split(".")
    event_us = int(datetime.fromisoformat(head + "." + frac[:6] + "+00:00").timestamp() * 1_000_000)
    held = hold_public_top(book["bids"], book["asks"], event_us, int(book["sequence"]))
    body = {"sequence": book.get("sequence"), "exchange_time": stamp, "bids": len(book.get("bids") or []),
            "asks": len(book.get("asks") or []), "reason": held["reason"], "mid_valid": held["mid_valid"],
            "eligible": False, "tokens": held["tokens"], "dispatched": False}
    return body


@app.get("/api/market/book-observations")
def book_observations(limit: int = 8):
    """Stored public-book holds. The response forces eligibility and dispatch off."""
    path = ROOT / "data" / "autonomous" / "book_observations.sqlite3"
    if not path.exists():
        body = {"rows": [], "eligible": False, "dispatched": False}
    else:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            found = connection.execute(
                "SELECT sequence, exchange_time, bids, asks, reason FROM book_observations ORDER BY sequence DESC LIMIT ?",
                (min(max(limit, 1), 20),)).fetchall()
        body = {"rows": [{"sequence": row[0], "exchange_time": row[1], "bids": row[2], "asks": row[3],
                          "reason": row[4], "eligible": False, "dispatched": False} for row in found],
                "eligible": False, "dispatched": False}
    return body


@app.get("/api/market/book-tops")
def book_tops():
    """Replay stored tops. Eligibility and dispatch stay false."""
    from bot.autonomous.worker import replay_stored_tops
    body = replay_stored_tops()
    body["eligible"] = False
    body["dispatched"] = False
    from bot.autonomous.worker import load_book_tops
    mids = []
    for bids, asks, event_us, _sequence in load_book_tops(70):
        try:
            bid = float(bids[0][0])
            ask = float(asks[0][0])
        except (TypeError, ValueError, IndexError):
            continue
        if bid > 0 and ask > 0 and ask >= bid:
            mids.append({"event_us": event_us, "mid": (bid + ask) / 2})
    body["mids"] = mids
    from bot.autonomous.worker import load_eth_tops
    eth_mids = []
    for bids, asks, event_us, _sequence in load_eth_tops(70):
        try:
            bid = float(bids[0][0])
            ask = float(asks[0][0])
        except (TypeError, ValueError, IndexError):
            continue
        if bid > 0 and ask > 0 and ask >= bid:
            eth_mids.append({"event_us": event_us, "mid": (bid + ask) / 2})
    body["eth_mids"] = eth_mids
    ordered = [row["event_us"] for row in mids]
    gaps = [current - previous for previous, current in zip(ordered, ordered[1:])
            if current - previous > 70_000_000 or current - previous < 50_000_000]
    body["cadence_gaps"] = len(gaps)
    body["opening_gap_only"] = len(gaps) == 1 and len(ordered) > 1 and ordered[1] - ordered[0] == gaps[0]
    qualified = []
    unqualified = 0
    for event_us in ordered:
        early = 60_000_000 - (event_us % 60_000_000)
        if early == 60_000_000 or early <= 5_000_000:
            qualified.append(event_us)
        else:
            unqualified += 1
    close_gaps = [current - previous for previous, current in zip(qualified, qualified[1:])
                  if current - previous > 70_000_000 or current - previous < 50_000_000]
    body["close_cadence_gaps"] = len(close_gaps)
    body["unqualified_tops"] = unqualified
    from bot.autonomous.worker import load_eth_tops
    eth_times = [event_us for _bids, _asks, event_us, _sequence in load_eth_tops(70)]
    eth_qualified = []
    eth_unqualified = 0
    for event_us in eth_times:
        early = 60_000_000 - (event_us % 60_000_000)
        if early == 60_000_000 or early <= 5_000_000:
            eth_qualified.append(event_us)
        else:
            eth_unqualified += 1
    eth_gaps = [current - previous for previous, current in zip(eth_qualified, eth_qualified[1:])
                if current - previous > 70_000_000 or current - previous < 50_000_000]
    body["eth_qualified_tops"] = len(eth_qualified)
    body["eth_close_cadence_gaps"] = len(eth_gaps)
    body["eth_unqualified_tops"] = eth_unqualified
    from types import SimpleNamespace
    from bot.autonomous.worker import PaperWorker
    gate = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper"))).gate_jev_decision(None)
    body["gate_status"] = gate["status"]
    body["gate_reason"] = gate["reason"]
    body["gate_dispatched"] = gate["dispatched"]
    return body


@app.get("/api/risk/jev-closed")
def jev_closed():
    """An empty credential must not construct a Jev client or place a call."""
    from bot.autonomous.jev_client import JevClient
    refused = False
    try:
        JevClient(transport=None, schema_resolver=None, api_key="")
    except ValueError:
        refused = True
    body = {"called": False, "refused": refused, "dispatched": False}
    return body


@app.get("/api/risk/limits")
def risk_limits():
    """Read the immutable autonomous limits. This does not arm or submit."""
    from bot.autonomous.contracts import RiskLimits
    limits = RiskLimits()
    body = {"max_drawdown": format(limits.max_drawdown, "f"), "max_daily_loss": format(limits.max_daily_loss, "f"),
            "max_position_fraction": format(limits.max_position_fraction, "f"),
            "kelly_fraction": format(limits.kelly_fraction, "f"), "max_state_tokens": limits.max_state_tokens,
            "armed": limits.armed, "candidate_eligible": limits.candidate_eligible, "dispatched": False}
    return body


@app.get("/api/risk/kelly-check")
def kelly_check():
    """Run the quarter-Kelly cap on fixed inputs. This does not size or submit an order."""
    from decimal import Decimal
    from bot.autonomous.sizing import KELLY_FRACTION_CEILING, kelly_loss_budget
    equity, probability, win, loss = Decimal("10000"), Decimal("0.80"), Decimal("1"), Decimal("1")
    budget = kelly_loss_budget(equity, probability, win, loss)
    refused = False
    try:
        kelly_loss_budget(equity, probability, win, loss, kelly_fraction=Decimal("0.26"))
    except ValueError:
        refused = True
    body = {"equity": format(equity, "f"), "probability": format(probability, "f"), "payoff": "1:1",
            "kelly_ceiling": format(KELLY_FRACTION_CEILING, "f"), "loss_budget": format(budget, "f"),
            "above_ceiling_refused": refused, "dispatched": False}
    return body


@app.get("/api/risk/entry-gate")
def entry_gate_check():
    """Run the code gate on fixed judgments. This does not call Jev or submit."""
    from types import SimpleNamespace
    from bot.autonomous.contracts import AdvisoryRisk, CandidateDecision, Direction, DirectionDistribution, Regime
    from bot.autonomous.worker import PaperWorker

    def run(regime, direction, quality, distribution, direction_confidence, noul):
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        decision = CandidateDecision("check", regime, direction, quality, AdvisoryRisk.SAFE, distribution,
                                     direction_confidence, 0.95, 0.95, 0.95, noul)
        result = worker.gate_jev_decision(decision)
        return {"status": result["status"], "reason": result.get("reason"), "dispatched": result["dispatched"]}

    body = {
        "boundary": run(Regime.TRENDING, Direction.LONG, 2, DirectionDistribution(0.80, 0.10, 0.10), 0.80, 0.10),
        "pass": run(Regime.TRENDING, Direction.LONG, 2, DirectionDistribution(0.81, 0.10, 0.09), 0.81, 0.10),
        "low_confidence": run(Regime.TRENDING, Direction.LONG, 2, DirectionDistribution(0.59, 0.21, 0.20), 0.59, 0.10),
        "crisis": run(Regime.CRISIS, Direction.NEUTRAL, 0, DirectionDistribution(0.20, 0.20, 0.60), 0.95, 0.10),
        "dispatched": False,
    }
    body["dispatched"] = any(item["dispatched"] for item in (body["boundary"], body["pass"], body["low_confidence"], body["crisis"]))
    return body


@app.get("/api/review/schema-proposal")
def schema_proposal_check():
    """A fixed schema proposal stays unapproved. This does not change a schema or submit."""
    from bot.autonomous.brain import BrainProposal
    from bot.autonomous.nightly import SessionView, review_session

    class _Propose:
        model_id = "fixed-check"

        def review(self, *, job_id, archive_hash, metrics):
            return BrainProposal(action="propose_schema", finding="fixed check", hypothesis="not applied",
                                 evidence_refs=(archive_hash,), schema_diff="fixed-check-diff", model_id=self.model_id)

    report = review_session(SessionView("paper-test", 0, "e" * 64, ()), _Propose(), job_id="fixed-schema-check")
    body = {"status": report.status, "reason_codes": list(report.reason_codes), "brain_action": report.brain_action,
            "approved": report.approved, "dispatched": False}
    return body


@app.get("/api/review/paper")
def paper_review():
    """Score the paper serve ledger. The report cannot approve itself."""
    from bot.autonomous.nightly import review_account_activity
    path = Path("/tmp/paper-serve-ledger.sqlite3")
    if not path.exists():
        body = {"present": False, "binary_brier": None, "resolved": 0, "pending": 0,
                "approved": False, "dispatched": False}
    else:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            report = review_account_activity(connection, "paper-test", time.time_ns() // 1000, "e" * 64)
            control = connection.execute(
                """SELECT permit, hard_killed, drawdown_latched, daily_halted, crisis_halted
                   FROM account_control WHERE account_id=?""", ("paper-test",)).fetchone()
            price_row = connection.execute(
                "SELECT payload_json FROM label_events WHERE kind='resolved' ORDER BY recorded_at_us DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        entry_price = exit_price = None
        if price_row and price_row[0]:
            payload = json.loads(price_row[0])
            execution = payload.get("verified_execution") if isinstance(payload, dict) else None
            if isinstance(execution, dict):
                entry_price = execution.get("entry_price_usd")
                exit_price = execution.get("exit_price_usd")
        body = {"present": True, "account": report.account_id, "binary_brier": report.binary_brier,
                "resolved": report.resolved, "pending": report.pending, "approved": report.approved,
                "status": report.status, "brain_action": report.brain_action,
                "fills": dict(report.counts).get("fill", 0), "misses": dict(report.counts).get("miss", 0),
                "entry_price_usd": entry_price, "exit_price_usd": exit_price,
                "permit": None if control is None else control[0],
                "hard_killed": bool(control[1]) if control else None,
                "drawdown_latched": bool(control[2]) if control else None,
                "daily_halted": bool(control[3]) if control else None,
                "crisis_halted": bool(control[4]) if control else None,
                "dispatched": False}
    return body


@app.get("/api/review/slot")
def review_slot():
    """Show the claimed 00:15 UTC slot and the next one. This does not run the review."""
    from bot.autonomous.nightly import next_review_us
    from bot.autonomous.worker import due_review_slot
    now_us = time.time_ns() // 1000
    path = Path("/tmp/serve-slots-minute.sqlite")
    claimed = []
    if path.exists():
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            claimed = [row[0] for row in connection.execute("SELECT slot FROM review_slots ORDER BY slot")]
        finally:
            connection.close()
    nxt = next_review_us(now_us)
    body = {"due_slot": due_review_slot(now_us), "next_slot": nxt, "claimed_slots": claimed,
            "next_claimed": nxt in claimed, "dispatched": False}
    return body


@app.get("/api/research/schemas")
def research_schemas():
    """Read the six typed research schemas. This does not call Jev or admit a name."""
    expected = {
        "regime": ["trending", "mean_reverting", "high_vol", "crisis"],
        "direction": ["long", "short", "neutral"],
        "setup_quality": [0, 1, 2, 3],
        "risk_state": ["safe", "near_limit", "reduce"],
    }
    rows = []
    for path in sorted((ROOT / "config" / "autonomous" / "schemas").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        rubric = payload["properties"]["decision_rubric"]["properties"]
        matches = all(rubric[name].get("enum") == values for name, values in expected.items())
        matches = matches and rubric["toxic_flow"].get("type") == "boolean"
        admission = payload["properties"]["admission_status"]
        allowed = admission.get("enum")
        if allowed is None and "const" in admission:
            allowed = [admission["const"]]
        rows.append({"candidate_id": payload["properties"]["candidate_id"]["const"],
                     "instrument": payload["properties"]["instrument"]["const"],
                     "admission_status": allowed, "rubric_matches": matches, "eligible": False})
    body = {"schemas": rows, "eligible": False, "dispatched": False}
    return body


@app.get("/api/research/candidates")
def research_candidates():
    return candidate_cards()


@app.get("/api/research/observations")
def research_observations():
    """Fetch public protocol rows for the six names. A row does not make a name eligible."""
    from data.public_metrics import fetch_holder_revenue, fetch_listed_markets, fetch_listed_tvl, fetch_protocol_fees, fetch_protocol_holder_rows, fetch_unlock_status
    symbols = ("AAVE", "HYPE", "PENDLE", "AERO", "LDO", "ONDO")
    log = DataCallLog(DB_FILE)
    markets = fetch_listed_markets(log, symbols)
    ids = {row["symbol"]: row["id"] for row in markets.get("rows", []) if row.get("symbol") and row.get("id")}
    tvl = fetch_listed_tvl(log, ids) if ids else {"rows": [], "unresolved": list(symbols), "eligible": False}
    fees = []
    fee_gaps = []
    tvl_by_symbol = {row.get("symbol"): row for row in tvl.get("rows", [])}
    for symbol, gecko_id in ids.items():
        candidates = []
        row = tvl_by_symbol.get(symbol)
        if row and row.get("slug"):
            candidates.append(row["slug"])
        for slug in tvl.get("parent_slugs", {}).get(symbol, []):
            if slug not in candidates:
                candidates.append(slug)
        matched = []
        for slug in candidates:
            try:
                matched.append(fetch_protocol_fees(log, slug, gecko_id))
            except Exception:
                continue
        if not matched:
            fee_gaps.append(symbol)
            continue
        ambiguous = len(matched) > 1
        for item in matched:
            item["symbol"] = symbol
            item["ambiguous"] = ambiguous
        fees.extend(matched)
    fee_slugs = tuple(dict.fromkeys(
        [row["slug"] for row in tvl.get("rows", []) if row.get("slug")]
        + [row["slug"] for row in fees if row.get("slug")]))
    holders = fetch_holder_revenue(log, fee_slugs) if fee_slugs else {"rows": [], "unresolved": [], "eligible": False}
    unlocks = fetch_unlock_status(log)
    identities = []
    for symbol, gecko_id in ids.items():
        parents = tvl.get("parent_slugs", {}).get(symbol) or []
        joined = tvl_by_symbol.get(symbol) or {}
        slug = parents[0] if parents else joined.get("slug")
        identities.append((symbol, slug if isinstance(slug, str) else "", gecko_id))
    raw_holders = fetch_protocol_holder_rows(log, tuple(identities))
    for row in markets.get("rows", []):
        row["eligible"] = False
    body = {"markets": markets.get("rows", []), "unresolved_markets": markets.get("unresolved", []),
            "tvl": tvl.get("rows", []), "unresolved_tvl": tvl.get("unresolved", []),
            "fees": fees, "fee_gaps": fee_gaps, "holder_revenue": holders.get("rows", []),
            "unresolved_holder_revenue": holders.get("unresolved", []),
            "unjoined_same_symbol": tvl.get("unjoined_same_symbol", []),
            "unlocks_status": unlocks["status_code"], "unlocks_present": False,
            "raw_holder_rows": raw_holders.get("rows", []),
            "unresolved_raw_holders": raw_holders.get("unresolved", []),
            "entity_adjusted": False,
            "eligible": False, "dispatched": False,
            "note": "Protocol TVL and fees are not token-holder accrual. None of these names are eligible."}
    return body


@app.get("/api/candles")
def get_candles(symbol: str = "BTC-USD", timeframe: str = "1h", limit: int = 100,
                exchange: str = "coinbase"):
    if exchange not in {"coinbase", "binance", "kraken", "okx"}:
        raise HTTPException(status_code=422, detail="Unsupported exchange")
    limit = min(max(limit, 1), 1000)
    candles = CandleHistoryStore(DB_FILE).load(exchange, symbol, timeframe, limit=limit)
    if not candles and exchange == "coinbase":
        with closing(db()) as con:
            rows = con.execute("SELECT ts_open,open,high,low,close,volume FROM candles WHERE symbol=? AND timeframe=? ORDER BY ts_open DESC LIMIT ?", (symbol, timeframe, limit)).fetchall()
        return list(reversed([dict(row) for row in rows]))
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
    if execute:
        return unavailable("Authority strategy execution handler unavailable")
    if exchange not in {"coinbase", "binance", "kraken", "okx"}:
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
        if execute and exchange == "coinbase":
            result["order"] = {"status": "blocked", "reason": ["paper authority required"]}
        con.commit()
        return result


@app.post("/api/orders")
def place_order(request: OrderRequest):
    return unavailable("Authority manual order handler unavailable")


@app.post("/api/control/arm")
def arm(request: Request, body: ArmRequest):
    return gateway_for(request, mutation=True).forward(request, 'arm',
        {'reason': body.reason, 'release_hash': body.release_hash})


@app.post("/api/control/disarm")
def disarm(request: Request, body: DisarmRequest | None = None):
    return gateway_for(request, mutation=True).forward(request, 'disarm',
        {'reason': (body or DisarmRequest()).reason})


def _paper_control_flags() -> dict:
    return {"ledger_present": False, "hard_killed": None, "source": "authority_status_required"}


@app.get("/api/risk/kill-split")
def kill_split():
    """Show the dashboard flag and the ledger flag orders actually read."""
    with closing(db()) as con:
        state = con.execute("SELECT kill_switch FROM runtime_state WHERE id=1").fetchone()
    ledger = _paper_control_flags()
    dashboard_killed = bool(state[0]) if state else None
    body = {"dashboard_killed": dashboard_killed, "ledger_hard_killed": ledger["hard_killed"],
            "same": dashboard_killed is ledger["hard_killed"], "dispatched": False}
    return body


@app.get("/api/risk/inventory")
def paper_inventory():
    """Read the paper balances. They have no event time, so they stay out of the causal snapshot."""
    path = Path("/tmp/paper-serve-ledger.sqlite3")
    rows = []
    if path.exists():
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = [{"asset": row[0], "native_qty": row[1]} for row in connection.execute(
                "SELECT asset, native_qty FROM balance_positions WHERE account_id=? ORDER BY asset",
                ("paper-test",))]
        finally:
            connection.close()
    body = {"rows": rows, "as_of_available": False, "in_snapshot": False, "eligible": False, "dispatched": False}
    return body


@app.get("/api/risk/equity")
def paper_equity():
    """Apply the code loss check to the stored paper mark. This does not reprice it."""
    path = Path("/tmp/paper-serve-ledger.sqlite3")
    body = {"present": False, "repriced": False, "dispatched": False}
    if path.exists():
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                """SELECT nav, high_water_nav, day_open_nav, drawdown, daily_loss, marked_at_us
                   FROM equity_marks WHERE account_id=?
                   ORDER BY marked_at_us DESC, mark_id DESC LIMIT 1""",
                ("paper-test",)).fetchone()
        finally:
            connection.close()
        if row is not None:
            from decimal import Decimal
            from fractions import Fraction
            from bot.autonomous.risk import loss_triggers
            hit_drawdown, hit_daily = loss_triggers(Fraction(Decimal(row[0])), Fraction(Decimal(row[1])), Fraction(Decimal(row[2])))
            body = {"present": True, "nav": row[0], "high_water_nav": row[1], "day_open_nav": row[2],
                    "stored_drawdown": row[3], "stored_daily_loss": row[4], "marked_at_us": row[5],
                    "drawdown_hit": hit_drawdown, "daily_hit": hit_daily, "repriced": False, "dispatched": False}
    return body


@app.get("/api/risk/unpriced-inventory")
def unpriced_inventory():
    """Place the journal balances next to the public AAVE book. The stored mark is not repriced."""
    from decimal import Decimal
    from data.public_metrics import fetch_coinbase_book
    path = Path("/tmp/paper-serve-ledger.sqlite3")
    balances = []
    mark = None
    if path.exists():
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            balances = [{"asset": row[0], "native_qty": row[1]} for row in connection.execute(
                "SELECT asset, native_qty FROM balance_positions WHERE account_id=? ORDER BY asset",
                ("paper-test",))]
            row = connection.execute(
                """SELECT nav, high_water_nav, day_open_nav, marked_at_us FROM equity_marks
                   WHERE account_id=? ORDER BY marked_at_us DESC, mark_id DESC LIMIT 1""",
                ("paper-test",)).fetchone()
        finally:
            connection.close()
        if row is not None:
            mark = {"nav": row[0], "high_water_nav": row[1], "day_open_nav": row[2], "marked_at_us": row[3]}
    book = fetch_coinbase_book(DataCallLog(DB_FILE), "AAVE-USD", level=1)
    bids, asks = book.get("bids") or [], book.get("asks") or []
    bid = max(bids)[0] if bids else None
    ask = min(asks)[0] if asks else None
    mid = (bid + ask) / 2 if bid is not None and ask is not None and bid > 0 and ask >= bid else None
    quantity = next((Decimal(item["native_qty"]) for item in balances if item["asset"] == "AAVE"), None)
    notional = None
    if quantity is not None and mid is not None:
        notional = str((quantity * Decimal(str(mid))).quantize(Decimal("0.01")))
    assets = {item["asset"]: Decimal(item["native_qty"]) for item in balances}
    extra = sorted(set(assets) - {"USD", "AAVE"})
    usd = assets.get("USD")
    cash_fraction = None
    marked_journal_usd = None
    if not extra and usd is not None and notional is not None and usd >= 0:
        marked = usd + Decimal(notional)
        if marked > 0 and usd <= marked:
            cash_fraction = format(usd / marked, "f")
            marked_journal_usd = format(marked, "f")
    from bot.autonomous.contracts import RiskLimits
    limits = RiskLimits()
    cap = limits.max_position_fraction
    gross_cap = limits.max_gross_fraction
    position_fraction_marked = None
    position_fraction_stored = None
    if notional is not None and marked_journal_usd is not None:
        position_fraction_marked = format(Decimal(notional) / Decimal(marked_journal_usd), "f")
    stored_nav = Decimal(mark["nav"]) if mark is not None else None
    if notional is not None and stored_nav is not None and stored_nav > 0:
        position_fraction_stored = format(Decimal(notional) / stored_nav, "f")
    gross_fraction_marked = None if extra else position_fraction_marked
    gross_fraction_stored = None if extra else position_fraction_stored
    body = {"balances": balances, "mark": mark, "aave_bid": bid, "aave_ask": ask, "aave_mid": mid,
            "aave_notional_usd": notional, "included_in_nav": False, "book_time": book.get("exchange_time"),
            "position_headroom_valid": False, "repriced": False, "eligible": False, "dispatched": False,
            "cash_fraction_of_marked_journal": cash_fraction, "cash_fraction_valid": cash_fraction is not None,
            "marked_journal_usd": marked_journal_usd, "copied_to_state": False,
            "state_slot_cash_headroom": "left_invalid",
            "max_position_fraction": format(cap, "f"),
            "position_fraction_of_marked_journal": position_fraction_marked,
            "inside_max_position_of_marked_journal": position_fraction_marked is not None and Decimal(position_fraction_marked) <= cap,
            "position_fraction_of_stored_nav": position_fraction_stored,
            "inside_max_position_of_stored_nav": position_fraction_stored is not None and Decimal(position_fraction_stored) <= cap,
            "state_slot_position_headroom": "left_invalid",
            "max_gross_fraction": format(gross_cap, "f"),
            "gross_fraction_of_marked_journal": gross_fraction_marked,
            "inside_max_gross_of_marked_journal": gross_fraction_marked is not None and Decimal(gross_fraction_marked) <= gross_cap,
            "gross_fraction_of_stored_nav": gross_fraction_stored,
            "inside_max_gross_of_stored_nav": gross_fraction_stored is not None and Decimal(gross_fraction_stored) <= gross_cap,
            "state_slot_gross_fraction": "left_invalid",
            "reason": "Stored mark is not a reprice of the journal balances, so headroom is not computed."}
    return body


@app.get("/api/risk/snapshot-drawdown")
def snapshot_drawdown():
    """Put the stored mark's drawdown on the public snapshot context. This does not tokenize or submit."""
    from bot.autonomous.state import hold_public_top
    from bot.autonomous.worker import load_book_tops
    tops = load_book_tops(70)
    from bot.autonomous.worker import load_mark_context
    shared = load_mark_context()
    if not tops:
        body = {"tops": 0, "drawdown_valid": False, "eligible_valid": False, "tokens": None,
                "repriced": False, "dispatched": False}
    else:
        current = tops[-1]
        event_us = current[2]
        close_us = (event_us // 60_000_000 + 1) * 60_000_000
        if close_us - event_us > 5_000_000:
            close_us = event_us
        from bot.autonomous.worker import load_causal_inventory
        inventory = load_causal_inventory(close_us)
        held = hold_public_top(current[0], current[1], event_us, current[3], prior=tuple(tops[:-1]),
                               candle_close_us=close_us, shared=shared, causal_inventory=inventory)
        body = {"tops": len(tops), "minute_books": held.get("minute_books"), "required": 61,
                "rv_valid": held["rv_valid"], "reason": held["reason"], "eligible_valid": held["eligible_valid"],
                "drawdown_valid": held["drawdown_valid"], "drawdown": held["drawdown"],
                "daily_loss_valid": held["daily_loss_valid"], "daily_loss": held["daily_loss"],
                "base_inventory": held["base_inventory"], "base_inventory_valid": held["base_inventory_valid"],
                "other_inventory": held["other_inventory"], "inventory_source": held["inventory_source"],
                "tokens": held["tokens"], "token_counter_called": held["token_counter_called"],
                "feature_bytes": held["feature_bytes"], "repriced": False, "dispatched": False}
    return body


@app.post("/api/control/kill")
def kill(request: Request):
    return gateway_for(request, mutation=True).forward(request, 'kill')


@app.post("/api/control/flatten")
def flatten(request: Request, body: FlattenRequest | None = None):
    """Bounded reduction is an authority command; under hard kill the authority's
    actual rejection is returned, never a local flatten."""
    body = body or FlattenRequest()
    payload = {'reason': body.reason}
    if body.incident_id is not None:
        payload['incident_id'] = body.incident_id
    return gateway_for(request, mutation=True).forward(request, 'reduce', payload)


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
