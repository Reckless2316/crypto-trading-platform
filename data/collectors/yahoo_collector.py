#!/usr/bin/env python3
"""Yahoo Finance crypto collector - free public REST endpoint for crypto prices."""
import requests, time
try:
    from collector_base import Collector, Candle
except ImportError:
    from .collector_base import Collector, Candle

class YahooFinanceCollector(Collector):
    """Free cryptocurrency price data from Yahoo Finance."""
    BASE = "https://query1.finance.yahoo.com/v8/finance/chart"

    def __init__(self):
        super().__init__("yahoo-finance", {})

    def fetch_candles(self, symbol: str, timeframe: str, start: int = None, end: int = None) -> list:
        # Yahoo uses crypto pairs like BTC-USD, ETH-USD, XRP-USD
        ticker = symbol.replace("USDT", "-USD")
        url = f"{self.BASE}/{ticker}"
        params = {
            "range": "1d",
            "interval": "1m",
            "events": "div,splits"
        }
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        candles = []
        # Response structure: chart.result[0].indicators.quote[0] -> open, high, low, close, volume arrays
        quote = data.get("chart", {}).get("result", [{}])[0].get("indicators", {}).get("quote", [{}])[0]
        opens = quote.get("open", [])
        highs = quote.get("high", [])
        lows = quote.get("low", [])
        closes = quote.get("close", [])
        volumes = quote.get("volume", [])
        timestamps = data.get("chart", {}).get("result", [{}])[0].get("timestamp", [])
        for i in range(min(len(opens), len(highs), len(lows), len(closes))):
            if opens[i] is not None:
                candles.append(Candle(
                    open=float(opens[i]), high=float(highs[i]), low=float(lows[i]), close=float(closes[i]),
                    volume=float(volumes[i]) if volumes[i] else 0,
                    ts_open=timestamps[i] * 1000 if i < len(timestamps) else 0,
                    ts_close=(timestamps[i] * 1000 + 60000) if i < len(timestamps) else 60000
                ))
        return candles
