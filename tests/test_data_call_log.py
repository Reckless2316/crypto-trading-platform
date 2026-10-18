import tempfile
import unittest
from pathlib import Path

from data.call_log import DataCallLog


class DataCallLogTests(unittest.TestCase):
    def test_records_success_and_failure_without_storing_a_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            log = DataCallLog(Path(directory) / "calls.sqlite3")
            ok_id = log.record(source="coinbase", symbol="BTC-USD", timeframe="1h",
                               started_at=1.0, elapsed_ms=12, ok=True, row_count=3,
                               status_code=200, body=b"[]")
            fail_id = log.record(source="binance", symbol="BTCUSDT", timeframe="1h",
                                 started_at=2.0, elapsed_ms=40, ok=False, row_count=0,
                                 error="401 secret=should-truncate-but-not-be-required", status_code=401)
            rows = log.recent()
            self.assertEqual([row["id"] for row in rows], [fail_id, ok_id])
            self.assertEqual(rows[1]["response_sha256"], "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945")
            self.assertEqual(rows[1]["ok"], 1)
            self.assertEqual(rows[0]["ok"], 0)
            self.assertNotIn("api_key", rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
