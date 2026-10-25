import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from data.collectors.collector_base import Candle
from data.history import CandleHistoryStore


class RedundantCandleIndexTests(unittest.TestCase):
    def test_history_store_drops_legacy_duplicate_index_and_keeps_pk_lookups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.db"
            with closing(sqlite3.connect(path)) as db:
                db.execute("""CREATE TABLE candle_history (
                    exchange TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                    ts_open INTEGER NOT NULL, ts_close INTEGER NOT NULL,
                    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
                    close REAL NOT NULL, volume REAL NOT NULL, fetched_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(exchange, symbol, timeframe, ts_open))""")
                db.execute("CREATE INDEX idx_history_lookup ON candle_history(exchange,symbol,timeframe,ts_open)")
                db.commit()
            store = CandleHistoryStore(path)
            with closing(sqlite3.connect(path)) as db:
                names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertNotIn("idx_history_lookup", names)
                plan = " ".join(str(r) for r in db.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM candle_history WHERE exchange=? AND symbol=? "
                    "AND timeframe=? ORDER BY ts_open DESC LIMIT 5", ("a", "b", "1h")))
                self.assertIn("sqlite_autoindex_candle_history", plan)
                self.assertNotIn("TEMP B-TREE", plan)
            candles = [Candle(1, 2, 0.5, 1.5, 3, i * 3_600_000, (i + 1) * 3_600_000) for i in range(5)]
            store.save("a", "b", "1h", candles)
            self.assertEqual([c.ts_open for c in store.load("a", "b", "1h", limit=2)],
                             [3 * 3_600_000, 4 * 3_600_000])


if __name__ == "__main__":
    unittest.main()
