#!/usr/bin/env python3
"""One-shot, unauthenticated Coinbase public candle request."""
import argparse
import time

try:
    from .coinbase_collector import CoinbaseCollector
except ImportError:
    from coinbase_collector import CoinbaseCollector

parser = argparse.ArgumentParser()
parser.add_argument("--symbol", default="BTC-USD")
parser.add_argument("--timeframe", default="1h", choices=CoinbaseCollector.TIMEFRAME_MAP)
parser.add_argument("--count", type=int, default=25)
args = parser.parse_args()
if not 1 <= args.count <= 250:
    parser.error("--count must be between 1 and 250")
end = int(time.time())
candles = CoinbaseCollector().fetch_candles(args.symbol, args.timeframe,
    end - args.count * CoinbaseCollector.TIMEFRAME_MAP[args.timeframe], end)
print(f"Fetched {len(candles)} {args.symbol} {args.timeframe} candles from Coinbase public REST.")
if candles:
    print(f"Latest close: {candles[-1].close} at {candles[-1].ts_open}")
