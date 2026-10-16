#!/usr/bin/env python3
"""Small reproducible local SQLite insert benchmark; output is machine-specific."""
import json
import sqlite3
import tempfile
import time
from pathlib import Path

TEST_ROWS = 100_000


def sqlite_benchmark(rows: int = TEST_ROWS) -> dict:
    with tempfile.TemporaryDirectory(prefix="surge-benchmark-") as temp_dir:
        db_path = Path(temp_dir) / "benchmark.sqlite3"
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE ticks (id INTEGER PRIMARY KEY, source TEXT, symbol TEXT, ts_source INTEGER, price REAL)")
        con.execute("CREATE INDEX idx_ticks_source_symbol_time ON ticks(source,symbol,ts_source)")
        started = time.perf_counter()
        con.executemany("INSERT INTO ticks VALUES(?,?,?,?,?)",
                        ((i, "test", "BTC-USD", 1_700_000_000 + i, 50_000.0 + i) for i in range(rows)))
        con.commit()
        write_seconds = time.perf_counter() - started
        started = time.perf_counter()
        result = con.execute("SELECT * FROM ticks WHERE source=? AND symbol=? ORDER BY ts_source LIMIT 1000", ("test", "BTC-USD")).fetchall()
        read_seconds = time.perf_counter() - started
        con.close()
        return {"engine": "sqlite", "rows_written": rows, "write_seconds": round(write_seconds, 6),
                "read_rows": len(result), "read_1000_seconds": round(read_seconds, 6)}


if __name__ == "__main__":
    print(json.dumps(sqlite_benchmark(), indent=2))
