#!/usr/bin/env python3
"""CryptoCompare free tier collector - public REST endpoint for historical crypto prices."""
import requests, time
try:
    from collector_base import Collector, Candle
except ImportError:
    from .collector_base import Collector, Candle

class CryptoCompareCollector(Collector):
    """Free crypto market data from CryptoCompare."""
    BASE = "https://min-api.cryptocompare.com/data"

    def __init__(self):
        super().__init__("cryptocompare", {})

    def fetch_candles(self, symbol: str, timeframe: str, start: int = None, end: int = None) -> list:
        # CryptoCompare histoday/hour/minute endpoints
        url = f"{self.BASE}/v2/histoday"
        params = {
            "fsym": symbol.replace("USDT", ""),  # BTC, ETH, XRP
            "tsym": "USD",
            "limit": 2000,
            "aggregate": 1  # 1-minute aggregation for histoday (simplified)
        }
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        candles = []
        # Response: {"Response":"Success","TimeTo":..., "TimeFrom":..., "Data":{"Data":[{"time":..., "open":..., "high":..., "low":..., "close":..., "volume"...}]}}
        for c in data.get("Data", {}).get("Data", []):
            candles.append(Candle(
                open=c.get("open", 0), high=c.get("high", 0), low=c.get("low", 0), close=c.get("close", 0),
                volume=c.get("volume", 0),
                ts_open=c.get("time", 0) * 1000,
                ts_close=c.get("time", 0) * 1000 + 60000
            ))
        return candles
