#!/usr/bin/env python3
"""OKX public REST candle collector (no credentials required)."""
from typing import List, Optional

import requests
try:
    from collector_base import Collector, Candle
except ImportError:
    from .collector_base import Collector, Candle


class OkxCollector(Collector):
    BASE = "https://www.okx.com/api/v5/market"
    BAR_MAP = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}
    INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
                   "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

    def __init__(self):
        super().__init__("okx", {})

    def fetch_candles(self, symbol: str, timeframe: str, start: int, end: Optional[int] = None) -> List[Candle]:
        """Fetch one OKX page; ``start`` and ``end`` are Unix seconds. Symbol looks like BTC-USDT."""
        if timeframe not in self.BAR_MAP:
            raise ValueError(f"Unsupported OKX timeframe: {timeframe}")
        parts = symbol.split("-") if symbol else []
        if len(parts) != 2 or not all(p.isalnum() for p in parts):
            raise ValueError("OKX instrument must look like BTC-USDT")
        if start < 0 or (end is not None and end <= start):
            raise ValueError("Require 0 <= start < end")
        step = self.INTERVAL_MS[timeframe]
        start_ms = start * 1000
        end_ms = None if end is None else end * 1000
        # history-candles pages backwards: "after" returns records older than the given ts.
        params = {"instId": symbol, "bar": self.BAR_MAP[timeframe], "limit": "100"}
        if end_ms is not None:
            params["after"] = str(end_ms)
        collected = {}
        for _ in range(50):
            r = requests.get(f"{self.BASE}/history-candles", params=params, timeout=12,
                             headers={"User-Agent": "SURGE-paper-platform/1.0"})
            r.raise_for_status()
            body = r.json()
            if body.get("code") != "0":
                raise RuntimeError(f"OKX candles error: {body.get('msg') or body.get('code')}")
            rows = body.get("data") or []
            if not rows:
                break
            for c in rows:
                ts = int(c[0])
                if ts >= start_ms and (end_ms is None or ts < end_ms) and (len(c) < 9 or c[8] == "1"):
                    collected[ts] = Candle(open=float(c[1]), high=float(c[2]), low=float(c[3]),
                                           close=float(c[4]), volume=float(c[5]),
                                           ts_open=ts, ts_close=ts + step)
            oldest = min(int(c[0]) for c in rows)
            if oldest <= start_ms:
                break
            params["after"] = str(oldest)
        return [collected[k] for k in sorted(collected)]
