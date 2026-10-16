#!/usr/bin/env python3
"""Binance collector — public REST endpoints."""
import requests
from typing import List, Optional
try:
    from collector_base import Collector, Candle
except ImportError:
    from .collector_base import Collector, Candle

class BinanceCollector(Collector):
    BASE = "https://api.binance.com/api/v3"
    INTERVAL_MAP = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}

    def __init__(self):
        super().__init__("binance", {})

    def fetch_candles(self, symbol: str, timeframe: str, start: int, end: Optional[int] = None) -> List[Candle]:
        """Fetch a single Binance page; ``start`` and ``end`` are Unix milliseconds."""
        if timeframe not in self.INTERVAL_MAP:
            raise ValueError(f"Unsupported Binance timeframe: {timeframe}")
        if not symbol or not symbol.replace("-", "").isalnum():
            raise ValueError("Binance symbol must be alphanumeric, optionally separated by '-'")
        if start < 0 or (end is not None and end <= start):
            raise ValueError("Require 0 <= start < end")
        interval = self.INTERVAL_MAP[timeframe]
        url = f"{self.BASE}/klines"
        params = {"symbol": symbol.replace("-", ""), "interval": interval, "startTime": start, "limit": 1000}
        if end is not None:
            # Binance endTime is inclusive. The history service filters to its
            # half-open interval after receiving this page.
            params["endTime"] = end - 1
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        candles = []
        for k in r.json():
            # Binance: [open_time, open, high, low, close, volume, ...]
            ts_open = int(k[0])  # Binance timestamps are already milliseconds.
            candles.append(Candle(
                open=float(k[1]), high=float(k[2]), low=float(k[3]), close=float(k[4]),
                volume=float(k[5]), ts_open=ts_open,
                ts_close=ts_open + self._interval_ms(timeframe)
            ))
        return [c for c in candles if c.ts_open >= start and (end is None or c.ts_open < end)]

    @staticmethod
    def _interval_ms(timeframe: str) -> int:
        return {"1m": 60_000, "5m": 300_000, "15m": 900_000,
                "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[timeframe]
