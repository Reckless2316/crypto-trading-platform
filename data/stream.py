#!/usr/bin/env python3
"""Stream live OKX tickers for BTC, ETH and XRP into one JSON-lines log file each.

Usage: python -m data.stream [--interval 2] [--log-dir logs/stream] [--max-ticks N]
Output: <log-dir>/BTC-USDT.log, ETH-USDT.log, XRP-USDT.log (one JSON object per line).
Uses OKX's public market API; no credentials are needed or read.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

TICKER_URL = "https://www.okx.com/api/v5/market/ticker"
INSTRUMENTS = {"BTC": "BTC-USDT", "ETH": "ETH-USDT", "XRP": "XRP-USDT"}


def fetch_tick(inst_id: str, session=requests) -> dict:
    r = session.get(TICKER_URL, params={"instId": inst_id}, timeout=8,
                    headers={"User-Agent": "SURGE-stream/1.0"})
    r.raise_for_status()
    body = r.json()
    if body.get("code") != "0" or not body.get("data"):
        raise RuntimeError(f"OKX ticker error: {body.get('msg') or body.get('code')}")
    d = body["data"][0]
    return {"exchange": "okx", "instrument": inst_id,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "exchange_ts_ms": int(d["ts"]), "last": float(d["last"]),
            "bid": float(d["bidPx"]), "ask": float(d["askPx"]), "volume_24h": float(d["vol24h"])}


def stream_one(inst_id: str, path: Path, interval: float, stop: threading.Event,
               max_ticks: int | None = None, fetch=fetch_tick) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ticks = 0
    with path.open("a", encoding="utf-8") as fh:
        while not stop.is_set() and (max_ticks is None or ticks < max_ticks):
            try:
                record = fetch(inst_id)
            except Exception as exc:  # keep streaming through transient errors
                record = {"exchange": "okx", "instrument": inst_id, "error": type(exc).__name__,
                          "fetched_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
            fh.write(json.dumps(record) + "\n")
            fh.flush()
            ticks += 1
            stop.wait(interval)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between ticks (default 2)")
    ap.add_argument("--log-dir", default="logs/stream")
    ap.add_argument("--max-ticks", type=int, default=None, help="stop after N ticks per coin")
    args = ap.parse_args(argv)
    if args.interval < 0.5:
        ap.error("--interval must be >= 0.5 to respect exchange rate limits")
    stop = threading.Event()
    threads = [threading.Thread(target=stream_one, daemon=True,
                                args=(inst, Path(args.log_dir) / f"{inst}.log", args.interval, stop, args.max_ticks))
               for inst in INSTRUMENTS.values()]
    for t in threads:
        t.start()
    print(f"Streaming {', '.join(INSTRUMENTS.values())} to {args.log_dir}/ (Ctrl-C to stop)")
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop.set()
    for t in threads:
        t.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
