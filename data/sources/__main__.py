"""Probe or continuously collect every registered data source.

    python -m data.sources status                 # one probe of each source, table output
    python -m data.sources status --only okx,mempool --json
    python -m data.sources collect --every 300    # snapshot loop into data/surge.sqlite3
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from data.call_log import DataCallLog

from .catalog import SOURCES, Fetcher, probe_all
from .store import SnapshotStore

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "surge.sqlite3"
MARK = {"ok": "OK", "needs_key": "KEY", "geo_blocked": "GEO", "unidentified": "???", "error": "ERR"}


def _summary(result: dict) -> str:
    if result["state"] != "ok":
        return result.get("error", "")
    return ", ".join(f"{k}={v:.6g}" if isinstance(v, float) else f"{k}={v}"
                     for k, v in result["data"].items() if not isinstance(v, (list, dict)))[:110]


def main() -> None:
    parser = argparse.ArgumentParser(description="SURGE data-source status and collection")
    parser.add_argument("command", choices=["status", "collect", "list"])
    parser.add_argument("--only", help="comma-separated source keys")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--every", type=float, default=0, help="collect: seconds between rounds (0 = once)")
    args = parser.parse_args()
    keys = args.only.split(",") if args.only else None

    if args.command == "list":
        for s in SOURCES:
            print(f"{s.key:18} {s.category:12} {s.method:45} {s.auth_env or '':18} {s.docs}")
        return
    fetcher = Fetcher(DataCallLog(args.db))
    store = SnapshotStore(args.db)
    while True:
        results = probe_all(fetcher, keys)
        store.save(results)
        if args.json:
            print(json.dumps(results, indent=1, default=str))
        else:
            for r in results:
                print(f"{MARK[r['state']]:3} {r['name'][:42]:42} {r['elapsed_ms']:6d}ms  {_summary(r)}")
            counts = {state: sum(r["state"] == state for r in results) for state in MARK}
            print("  ".join(f"{k}={v}" for k, v in counts.items() if v))
        if args.command == "status" or args.every <= 0:
            return
        time.sleep(args.every)


if __name__ == "__main__":
    main()
