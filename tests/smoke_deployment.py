#!/usr/bin/env python3
"""Read-only health check for an already running SURGE deployment."""
import json
import sys
import urllib.error
import urllib.request


def get(base: str, path: str):
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=8) as response:
        return response.status, response.headers.get("content-type", ""), response.read()


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    try:
        status, _, body = get(base, "/health")
        health = json.loads(body)
        if status != 200 or health.get("status") != "ok":
            raise RuntimeError(f"health check failed: {health}")
        status, content_type, _ = get(base, "/")
        if status != 200 or "text/html" not in content_type:
            raise RuntimeError("dashboard page is unavailable")
        _, _, body = get(base, "/api/status")
        current = json.loads(body)
        _, _, body = get(base, "/api/candles?symbol=BTC-USD&timeframe=1h&limit=100")
        candles = json.loads(body)
        print(json.dumps({"result":"PASS","health":health,"mode":current["mode"],
                          "trading_allowed":current["gates"]["allowed"],"stored_candles":len(candles)}, indent=2))
        if not candles:
            print("No candles stored yet; use Refresh Coinbase data in the dashboard.")
        return 0
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
