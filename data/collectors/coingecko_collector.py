#!/usr/bin/env python3
"""CoinGecko free tier collector - public REST endpoint for crypto prices and historical data."""
import requests, time
from typing import List, Optional

# Import with fallback
try:
    from collector_base import Collector, Candle
except ImportError:
    from .collector_base import Collector, Candle

class CoinGeckoCollector(Collector):
    """Free crypto data from CoinGecko (no auth required for basic endpoints)."""
    BASE = "https://api.coingecko.com/api/v3"

    def __init__(self):
        super().__init__("coingecko", {})

    def fetch_candles(self, symbol: str, timeframe: str, start: int, end: Optional[int] = None) -> List[Candle]:
        # CoinGecko supports: market_chart/range with from/to timestamps (unix)
        # For BTC: coin_id = "bitcoin"; for ETH: "ethereum"; for XRP: "ripple"
        coin_id_map = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "XRPUSDT": "ripple"}
        coin_id = coin_id_map.get(symbol, "bitcoin")
        url = f"{self.BASE}/coins/{coin_id}/market_chart/range"
        params = {"vs_currency": "usd", "from": start, "to": end or int(time.time())}
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        candles = []
        # Data format: {"prices": [[timestamp_ms, price], ...], "market_caps": ..., "total_volumes": ...}
        prices = data.get("prices", [])
        # Aggregate to the requested timeframe (simplified: take start/end of range for demo)
        if prices:
            open_p = prices[0][1]
            close_p = prices[-1][1]
            high_p = max(p[1] for p in prices)
            low_p = min(p[1] for p in prices)
            candles.append(Candle(
                open=open_p, high=high_p, low=low_p, close=close_p,
                volume=sum(p[2] if len(p) > 2 else 0 for p in prices) / len(prices) if prices else 0,
                ts_open=prices[0][0], ts_close=prices[-1][0]
            ))
        return candles
