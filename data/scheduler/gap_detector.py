"""Find missing intervals in the candle table used by SURGE."""
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = Path(os.environ.get("SURGE_DB_PATH", str(ROOT / "data" / "surge.sqlite3")))
TIMEFRAME_MS = {"1m":60_000,"5m":300_000,"15m":900_000,"1h":3_600_000,"1d":86_400_000}


def detect_gaps(source: str, symbol: str, timeframe: str, since_hours: int = 24) -> list[dict]:
    if timeframe not in TIMEFRAME_MS:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    if since_hours <= 0:
        raise ValueError("since_hours must be positive")
    cutoff = int((time.time() - since_hours * 3600) * 1000)
    with sqlite3.connect(DB) as con:
        rows = con.execute("SELECT ts_open FROM candles WHERE source=? AND symbol=? AND timeframe=? AND ts_open>=? ORDER BY ts_open",
                           (source,symbol,timeframe,cutoff)).fetchall()
    timestamps = [int(row[0]) for row in rows]
    interval = TIMEFRAME_MS[timeframe]
    gaps = []
    for previous, current in zip(timestamps, timestamps[1:]):
        missing = max(0, round((current - previous) / interval) - 1)
        if missing:
            gaps.append({"start_ms":previous,"end_ms":current,"missing_candles":missing})
    return gaps
