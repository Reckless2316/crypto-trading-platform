"""Append-only log of outbound market-data calls. No secrets are stored."""
from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class DataCallLog:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def _connect(self) -> sqlite3.Connection:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS data_calls (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            symbol TEXT,
            timeframe TEXT,
            started_at REAL NOT NULL,
            elapsed_ms INTEGER NOT NULL,
            status_code INTEGER,
            ok INTEGER NOT NULL,
            error TEXT,
            response_sha256 TEXT,
            row_count INTEGER NOT NULL
        )""")
        db.commit()
        return db

    def record(self, *, source: str, symbol: str | None, timeframe: str | None,
               started_at: float, elapsed_ms: int, ok: bool, row_count: int,
               status_code: int | None = None, error: str | None = None,
               body: bytes | None = None) -> int:
        if not source or elapsed_ms < 0 or row_count < 0:
            raise ValueError("invalid data-call record")
        digest = hashlib.sha256(body).hexdigest() if body else None
        with closing(self._connect()) as db:
            cursor = db.execute(
                """INSERT INTO data_calls(source,symbol,timeframe,started_at,elapsed_ms,status_code,ok,error,response_sha256,row_count)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (source, symbol, timeframe, started_at, elapsed_ms, status_code, int(ok),
                 None if error is None else error[:500], digest, row_count))
            db.commit()
            return int(cursor.lastrowid)

    def recent(self, limit: int = 50) -> list[dict]:
        limit = min(max(int(limit), 1), 200)
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT id,source,symbol,timeframe,started_at,elapsed_ms,status_code,ok,error,response_sha256,row_count FROM data_calls ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(row) for row in rows]
