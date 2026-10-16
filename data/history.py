"""Exchange-aware candle history fetching and local SQLite storage.

All public APIs in this module use Unix milliseconds. Existing collector APIs
use provider-specific units; the adapter below keeps those details contained.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Mapping, Optional

from .collectors.collector_base import Candle, Collector
from .collectors.binance_collector import BinanceCollector
from .collectors.coinbase_collector import CoinbaseCollector
from .collectors.kraken_collector import KrakenCollector

TIMEFRAME_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
                "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


class CandleHistoryStore:
    """Persist candles with exchange included in the primary key."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        with closing(self._connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS candle_history (
                exchange TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                ts_open INTEGER NOT NULL, ts_close INTEGER NOT NULL,
                open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
                close REAL NOT NULL, volume REAL NOT NULL, fetched_at_ms INTEGER NOT NULL,
                PRIMARY KEY(exchange, symbol, timeframe, ts_open))""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_history_lookup ON candle_history(exchange,symbol,timeframe,ts_open)")
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def save(self, exchange: str, symbol: str, timeframe: str, candles: list[Candle]) -> int:
        now_ms = int(time.time() * 1000)
        rows = [(exchange, symbol, timeframe, c.ts_open, c.ts_close, c.open, c.high,
                 c.low, c.close, c.volume, now_ms) for c in candles]
        with closing(self._connect()) as db:
            db.executemany("""INSERT INTO candle_history
                (exchange,symbol,timeframe,ts_open,ts_close,open,high,low,close,volume,fetched_at_ms)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(exchange,symbol,timeframe,ts_open)
                DO UPDATE SET ts_close=excluded.ts_close,open=excluded.open,high=excluded.high,
                low=excluded.low,close=excluded.close,volume=excluded.volume,
                fetched_at_ms=excluded.fetched_at_ms""", rows)
            db.commit()
        return len(rows)

    def load(self, exchange: str, symbol: str, timeframe: str,
             start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> list[Candle]:
        sql = "SELECT open,high,low,close,volume,ts_open,ts_close FROM candle_history WHERE exchange=? AND symbol=? AND timeframe=?"
        params: list[object] = [exchange, symbol, timeframe]
        if start_ms is not None:
            sql += " AND ts_open>=?"; params.append(int(start_ms))
        if end_ms is not None:
            sql += " AND ts_open<?"; params.append(int(end_ms))
        sql += " ORDER BY ts_open"
        with closing(self._connect()) as db:
            return [Candle(*row) for row in db.execute(sql, params).fetchall()]


class ExchangeHistory:
    """Fetch public candles using installed exchange collectors and store them."""

    def __init__(self, collectors: Optional[Mapping[str, Collector]] = None):
        self.collectors = dict(collectors or {
            "coinbase": CoinbaseCollector(), "binance": BinanceCollector(), "kraken": KrakenCollector()
        })

    def fetch(self, exchange: str, symbol: str, timeframe: str, start_ms: int,
              end_ms: int, store: Optional[CandleHistoryStore] = None) -> list[Candle]:
        if exchange not in self.collectors:
            raise ValueError(f"Unsupported exchange: {exchange}")
        if timeframe not in TIMEFRAME_MS:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        self._validate_symbol(exchange, symbol)
        if start_ms < 0 or end_ms <= start_ms:
            raise ValueError("Require 0 <= start_ms < end_ms")
        collector = self.collectors[exchange]
        supported = {
            "coinbase": set(CoinbaseCollector.TIMEFRAME_MAP),
            "binance": set(BinanceCollector.INTERVAL_MAP),
            "kraken": {"1m", "5m", "15m", "1h", "4h", "1d"},
        }.get(exchange, set())
        if timeframe not in supported:
            raise ValueError(f"Unsupported {exchange} timeframe: {timeframe}")
        step = TIMEFRAME_MS[timeframe]
        # Keep each call within the providers' documented candle page sizes.
        page_size = {"coinbase": 300, "binance": 1000, "kraken": 700}.get(exchange)
        if page_size is None:
            raise ValueError(f"No pagination policy configured for exchange: {exchange}")
        result: dict[int, Candle] = {}
        cursor = start_ms
        while cursor < end_ms:
            page_end = min(end_ms, cursor + page_size * step)
            if exchange == "binance":
                raw = collector.fetch_candles(symbol, timeframe, cursor, page_end)
            else:  # Coinbase and Kraken collectors take Unix seconds.
                raw = collector.fetch_candles(symbol, timeframe, cursor // 1000, page_end // 1000)
            for c in raw:
                if start_ms <= c.ts_open < end_ms:
                    # Correct the legacy Binance collector's fixed one-minute close time.
                    result[c.ts_open] = Candle(c.open, c.high, c.low, c.close, c.volume,
                                               c.ts_open, c.ts_open + step)
            cursor = page_end
        candles = [result[k] for k in sorted(result)]
        if store is not None:
            store.save(exchange, symbol, timeframe, candles)
        return candles

    @staticmethod
    def _validate_symbol(exchange: str, symbol: str) -> None:
        if not symbol:
            raise ValueError("Symbol is required")
        if exchange == "coinbase":
            if len(symbol.split("-")) != 2 or not all(p.isalnum() for p in symbol.split("-")):
                raise ValueError("Coinbase symbol must look like BTC-USD")
        elif exchange == "binance":
            if not symbol.replace("-", "").isalnum():
                raise ValueError("Binance symbol must be alphanumeric, optionally separated by '-'")
        elif exchange == "kraken":
            supported = {"BTCUSDT", "ETHUSDT", "XRPUSDT", "XXBTZUSD", "XETHZUSD", "XXRPZUSD"}
            if symbol not in supported:
                raise ValueError(f"Unsupported Kraken symbol: {symbol}")
