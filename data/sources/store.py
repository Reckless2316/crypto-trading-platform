"""SQLite store for source snapshots: one row per probe, newest-per-source queries."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path


class SnapshotStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        with closing(self._connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS source_snapshots (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL, fetched_at REAL NOT NULL, state TEXT NOT NULL,
                elapsed_ms INTEGER NOT NULL, data TEXT, error TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_source_snapshots ON source_snapshots(key, id)")
            db.commit()

    def _connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def save(self, results: list[dict]) -> int:
        rows = [(r["key"], r["fetched_at"], r["state"], r["elapsed_ms"],
                 json.dumps(r["data"], default=str) if "data" in r else None, r.get("error")) for r in results]
        with closing(self._connect()) as db:
            db.executemany("INSERT INTO source_snapshots(key,fetched_at,state,elapsed_ms,data,error) VALUES(?,?,?,?,?,?)", rows)
            db.commit()
        return len(rows)

    def latest(self) -> dict[str, dict]:
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT s.* FROM source_snapshots s
                JOIN (SELECT key, MAX(id) AS id FROM source_snapshots GROUP BY key) m ON s.id = m.id""").fetchall()
        return {r["key"]: {"key": r["key"], "fetched_at": r["fetched_at"], "state": r["state"], "elapsed_ms": r["elapsed_ms"],
                           "data": json.loads(r["data"]) if r["data"] else None, "error": r["error"]} for r in rows}

    def history(self, key: str, limit: int = 100) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM source_snapshots WHERE key=? ORDER BY id DESC LIMIT ?",
                              (key, min(max(int(limit), 1), 1000))).fetchall()
        return [{"fetched_at": r["fetched_at"], "state": r["state"], "data": json.loads(r["data"]) if r["data"] else None,
                 "error": r["error"]} for r in rows]
