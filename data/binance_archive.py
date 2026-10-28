"""Download and load Binance USD-M futures history from the public data.binance.vision archive.

    python -m data.binance_archive download --symbols BTCUSDT,ETHUSDT --start 2024-01-01
    python -m data.binance_archive import --symbols BTCUSDT --timeframe 1h   # into the dashboard's candle store

Three series per symbol, all public and credential-free:
- 5m klines (monthly zips)
- 5m open-interest snapshots from the "metrics" files (daily zips); OI is in coin units,
  so it does not move with price
- funding settlements (monthly zips)

Downloads are cached as the original zips. A 404 is remembered so a missing day is not
re-requested, which matters because the metrics series is roughly 25k files.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = "https://data.binance.vision/data/futures/um"
DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "data" / "cache" / "binance-vision"
ARCHIVE_EXCHANGE = "binance-usdm-archive"   # exchange name used in CandleHistoryStore
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
           "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "DOTUSDT", "BCHUSDT")
FIVE_MIN_MS = 300_000


@dataclass(frozen=True)
class Series:
    """Column arrays for one symbol. Index i of the bar arrays is one 5m candle."""
    symbol: str
    ts: list[int]            # bar open, ms
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]
    oi_ts: list[int]         # snapshot time, ms
    oi: list[float]          # sum_open_interest, coin units
    funding_ts: list[int]    # settlement time, ms
    funding: list[float]     # settled rate per interval


def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d.isoformat()
        d += timedelta(days=1)


def _urls(symbol: str, start: date, end: date) -> list[tuple[str, Path]]:
    jobs = []
    for month in _months(start, end):
        jobs.append((f"{BASE}/monthly/klines/{symbol}/5m/{symbol}-5m-{month}.zip",
                     Path("klines") / symbol / f"{month}.zip"))
        jobs.append((f"{BASE}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip",
                     Path("funding") / symbol / f"{month}.zip"))
    for day in _days(start, end):
        jobs.append((f"{BASE}/daily/metrics/{symbol}/{symbol}-metrics-{day}.zip",
                     Path("metrics") / symbol / f"{day}.zip"))
    return jobs


def _fetch(url: str, dest: Path, missing: Path) -> str:
    if dest.exists() or missing.exists():
        return "cached"
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                body = response.read()
            zipfile.ZipFile(io.BytesIO(body)).testzip()
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(body)
            tmp.replace(dest)
            return "ok"
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                missing.parent.mkdir(parents=True, exist_ok=True)
                missing.touch()
                return "404"
            time.sleep(2 ** attempt)
        except (urllib.error.URLError, TimeoutError, zipfile.BadZipFile, ConnectionError):
            time.sleep(2 ** attempt)
    return "failed"


def download(symbols, start: date, end: date, cache: Path, workers: int = 16) -> dict:
    jobs = [(url, cache / rel, cache / "_missing" / rel) for s in symbols for url, rel in _urls(s, start, end)]
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(workers) as pool:
        for i, status in enumerate(pool.map(lambda j: _fetch(*j), jobs), 1):
            counts[status] = counts.get(status, 0) + 1
            if i % 2000 == 0:
                print(f"{i}/{len(jobs)} {counts}", flush=True)
    return counts


def _rows(path: Path):
    with zipfile.ZipFile(path) as archive:
        with archive.open(archive.namelist()[0]) as handle:
            reader = csv.reader(io.TextIOWrapper(handle, "utf-8"))
            for row in reader:
                if row and row[0][:1].isdigit():  # some files carry a header row, older ones do not
                    yield row


def _utc_ms(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)


def load(symbol: str, cache: Path = DEFAULT_CACHE) -> Series:
    bars: dict[int, tuple[float, float, float, float]] = {}
    for path in sorted((cache / "klines" / symbol).glob("*.zip")):
        for row in _rows(path):
            bars[int(row[0])] = (float(row[1]), float(row[2]), float(row[3]), float(row[4]))
    oi: dict[int, float] = {}
    for path in sorted((cache / "metrics" / symbol).glob("*.zip")):
        for row in _rows(path):
            if row[2]:
                oi[_utc_ms(row[0])] = float(row[2])
    funding: dict[int, float] = {}
    for path in sorted((cache / "funding" / symbol).glob("*.zip")):
        for row in _rows(path):
            # calc_time carries a few ms of jitter; snap to the second.
            funding[int(row[0]) // 1000 * 1000] = float(row[2])
    ts = sorted(bars)
    oi_ts = sorted(oi)
    f_ts = sorted(funding)
    return Series(symbol, ts, [bars[t][0] for t in ts], [bars[t][1] for t in ts],
                  [bars[t][2] for t in ts], [bars[t][3] for t in ts],
                  oi_ts, [oi[t] for t in oi_ts], f_ts, [funding[t] for t in f_ts])


def candles(series: Series, timeframe: str = "5m"):
    """The archive's 5m bars as platform Candles, aggregated to `timeframe` (complete buckets only)."""
    from .collectors.collector_base import Candle
    from .history import TIMEFRAME_MS
    step = TIMEFRAME_MS[timeframe]
    if step % FIVE_MIN_MS:
        raise ValueError("timeframe must be a multiple of 5m")
    need = step // FIVE_MIN_MS
    out = []
    i = 0
    while i < len(series.ts):
        bucket = series.ts[i] - series.ts[i] % step
        j = i
        while j < len(series.ts) and series.ts[j] - series.ts[j] % step == bucket:
            j += 1
        if j - i == need:
            # Archive files carry volume, but Series keeps prices only; imported volume is 0.
            out.append(Candle(series.open[i], max(series.high[i:j]), min(series.low[i:j]), series.close[j - 1],
                              0.0, bucket, bucket + step))
        i = j
    return out


def import_to_store(symbol: str, timeframe: str, db_path: Path, cache: Path = DEFAULT_CACHE) -> int:
    from .history import CandleHistoryStore
    return CandleHistoryStore(db_path).save(ARCHIVE_EXCHANGE, symbol, timeframe, candles(load(symbol, cache), timeframe))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", default="download", choices=["download", "import"])
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-09-30")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeframe", default="1h", help="import: candle size to store")
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "surge.sqlite3")
    args = parser.parse_args()
    if args.command == "import":
        for symbol in args.symbols.split(","):
            print(symbol, import_to_store(symbol, args.timeframe, args.db, args.cache), "candles ->", args.db)
        return
    counts = download(args.symbols.split(","), date.fromisoformat(args.start), date.fromisoformat(args.end),
                      args.cache, args.workers)
    print(json.dumps(counts))


if __name__ == "__main__":
    main()
