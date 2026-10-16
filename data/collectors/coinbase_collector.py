#!/usr/bin/env python3
"""Coinbase Exchange public REST candle collector."""
from datetime import datetime, timezone
from typing import List, Optional

import requests
try:
    from collector_base import Collector, Candle
except ImportError:
    from .collector_base import Collector, Candle

class CoinbaseCollector(Collector):
    BASE = "https://api.exchange.coinbase.com"
    TIMEFRAME_MAP = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}

    def __init__(self):
        super().__init__("coinbase", {})  # No auth for public market data

    def fetch_candles(self, symbol: str, timeframe: str, start: int, end: Optional[int] = None) -> List[Candle]:
        """Fetch one Coinbase page; ``start`` and ``end`` are Unix seconds."""
        if timeframe not in self.TIMEFRAME_MAP:
            raise ValueError(f"Unsupported Coinbase timeframe: {timeframe}")
        if not symbol or not all(part.isalnum() for part in symbol.split("-")) or len(symbol.split("-")) != 2:
            raise ValueError("Coinbase product must look like BTC-USD")
        if start < 0 or (end is not None and end <= start):
            raise ValueError("Require 0 <= start < end")
        granularity = self.TIMEFRAME_MAP[timeframe]
        url = f"{self.BASE}/products/{symbol}/candles"
        end_seconds = int(datetime.now(timezone.utc).timestamp()) if end is None else end
        params = {"start": self._iso_time(start), "end": self._iso_time(end_seconds),
                  "granularity": granularity}
        r = requests.get(url, params=params, timeout=12, headers={"User-Agent": "SURGE-paper-platform/1.0"})
        r.raise_for_status()
        candles = []
        for c in r.json():
            if len(c) < 6:
                continue
            candles.append(Candle(
                open=float(c[3]), high=float(c[2]), low=float(c[1]), close=float(c[4]),
                volume=float(c[5]), ts_open=int(c[0])*1000, ts_close=(int(c[0])+granularity)*1000
            ))
        start_ms, end_ms = start * 1000, end_seconds * 1000
        return sorted((c for c in candles if start_ms <= c.ts_open < end_ms),
                      key=lambda candle: candle.ts_open)

    @staticmethod
    def _iso_time(timestamp: int) -> str:
        return datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
