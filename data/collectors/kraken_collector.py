#!/usr/bin/env python3
"""Kraken collector — public REST endpoint for OHLC candles."""
import requests
from typing import List, Optional
try:
    from collector_base import Collector, Candle
except ImportError:
    from .collector_base import Collector, Candle

class KrakenCollector(Collector):
    BASE = "https://api.kraken.com/0/public"
    PAIR_MAP = {"BTCUSDT": "XXBTZUSD", "ETHUSDT": "XETHZUSD", "XRPUSDT": "XXRPZUSD"}

    def __init__(self):
        super().__init__("kraken", {})

    def fetch_candles(self, symbol: str, timeframe: str, start: int, end: Optional[int] = None) -> List[Candle]:
        """Fetch one Kraken page; ``start`` and ``end`` are Unix seconds."""
        interval_map = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
        if timeframe not in interval_map:
            raise ValueError(f"Unsupported Kraken timeframe: {timeframe}")
        if not symbol:
            raise ValueError("Kraken pair is required")
        if start < 0 or (end is not None and end <= start):
            raise ValueError("Require 0 <= start < end")
        pair = self.PAIR_MAP.get(symbol, symbol)
        # Kraken OHLC: interval in minutes (1, 5, 15, 60, 240, 1440, 10080, 21600)
        interval = interval_map[timeframe]
        url = f"{self.BASE}/OHLC"
        params = {"pair": pair, "interval": interval, "since": start}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        # Response: [pair_name, [ts, open, high, low, close, vwap, volume, count]...]
        if data.get("error"):
            raise RuntimeError(f"Kraken OHLC error: {', '.join(map(str, data['error']))}")
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Kraken OHLC response is missing result data")
        pair_name = next((key for key in result if key != "last"), None)
        if pair_name is None:
            return []
        candles_raw = result[pair_name]
        end_seconds = float("inf") if end is None else end
        candles = []
        for c in candles_raw:
            ts_seconds = float(c[0])
            if ts_seconds < start or ts_seconds >= end_seconds:
                continue
            ts_open = int(ts_seconds * 1000)
            candles.append(Candle(
                open=float(c[1]), high=float(c[2]), low=float(c[3]), close=float(c[4]),
                volume=float(c[6]), ts_open=ts_open,
                ts_close=ts_open + interval * 60_000
            ))
        return sorted(candles, key=lambda candle: candle.ts_open)
