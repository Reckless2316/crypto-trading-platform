"""Standalone account-scoped paper execution helper; prices must be injected."""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Callable


DB_PATH = Path(os.environ.get("SURGE_PAPER_DB", "data/paper_accounts.sqlite3"))


class PaperCoinbaseAccount:
    def __init__(self, account_id: str, starting_equity: float = 10_000.0,
                 slippage_pct: float = 0.001, price_provider: Callable[[str], float] | None = None,
                 db_path: str | Path | None = None):
        if starting_equity <= 0 or slippage_pct < 0:
            raise ValueError("Starting equity must be positive and slippage cannot be negative")
        self.account_id = account_id
        self.equity = starting_equity
        self.slippage_pct = slippage_pct
        self.price_provider = price_provider
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as con:
            con.execute("CREATE TABLE IF NOT EXISTS positions (account TEXT NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL, avg_price REAL NOT NULL, ts REAL NOT NULL, PRIMARY KEY(account,symbol))")
            con.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, account TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL, ts REAL NOT NULL)")

    def get_price(self, symbol: str) -> float:
        if not self.price_provider:
            raise RuntimeError("A market-data price_provider is required; paper fills never use hard-coded prices")
        price = float(self.price_provider(symbol))
        if price <= 0:
            raise ValueError("Price provider returned a non-positive price")
        return price

    def place_order(self, symbol: str, side: str, qty: float, order_type: str = "market") -> dict:
        side = side.lower()
        if side not in {"buy", "sell"} or order_type != "market" or qty <= 0:
            raise ValueError("Only positive-quantity market buy/sell paper orders are supported")
        market_price = self.get_price(symbol)
        fill_price = market_price * (1 + self.slippage_pct if side == "buy" else 1 - self.slippage_pct)
        with sqlite3.connect(self.db_path) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT qty,avg_price FROM positions WHERE account=? AND symbol=?", (self.account_id,symbol)).fetchone()
            current_qty, average = (float(row[0]), float(row[1])) if row else (0.0, 0.0)
            if side == "sell" and qty > current_qty:
                raise ValueError("Sell quantity exceeds this account's long position")
            if side == "buy":
                new_qty = current_qty + qty
                average = (current_qty * average + qty * fill_price) / new_qty
            else:
                new_qty = current_qty - qty
            fee = qty * fill_price * 0.001
            con.execute("INSERT INTO positions(account,symbol,qty,avg_price,ts) VALUES(?,?,?,?,?) ON CONFLICT(account,symbol) DO UPDATE SET qty=excluded.qty,avg_price=excluded.avg_price,ts=excluded.ts",
                        (self.account_id,symbol,new_qty,average,time.time()))
            cur = con.execute("INSERT INTO trades(account,symbol,side,qty,price,fee,ts) VALUES(?,?,?,?,?,?,?)",
                              (self.account_id,symbol,side,qty,fill_price,fee,time.time()))
            return {"id":cur.lastrowid,"status":"filled","symbol":symbol,"side":side,"qty":qty,"price":fill_price,"fee":fee,"mode":"paper","account":self.account_id}

    def get_positions(self) -> list[dict]:
        with sqlite3.connect(self.db_path) as con:
            rows = con.execute("SELECT symbol,qty,avg_price FROM positions WHERE account=? AND qty>0", (self.account_id,)).fetchall()
        return [{"symbol":row[0],"qty":row[1],"avg_price":row[2]} for row in rows]

    def flatten(self, symbol: str | None = None) -> list[dict]:
        rows = self.get_positions()
        if symbol:
            rows = [row for row in rows if row["symbol"] == symbol]
        return [self.place_order(row["symbol"], "sell", row["qty"]) for row in rows]
