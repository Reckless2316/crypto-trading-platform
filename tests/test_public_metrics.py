import tempfile
import unittest
from pathlib import Path
from data.call_log import DataCallLog
from data.public_metrics import fetch_global


class Response:
    status_code = 200
    content = b'{"data":{"total_market_cap":{"usd":1},"market_cap_percentage":{"btc":50}}}'

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": {"total_market_cap": {"usd": 1}, "market_cap_percentage": {"btc": 50}}}


class PublicMetricTests(unittest.TestCase):
    def test_missing_eth_dominance_stays_missing_and_the_call_is_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            log = DataCallLog(Path(directory) / "calls.sqlite3")
            snapshot = fetch_global(log, getter=lambda *args, **kwargs: Response())
            self.assertEqual(snapshot["btc_dominance_pct"], 50)
            self.assertIsNone(snapshot["eth_dominance_pct"])
            self.assertEqual(snapshot["missing"], ["eth_dominance_pct"])
            self.assertEqual(log.recent()[0]["source"], "coingecko-global")
            self.assertEqual(log.recent()[0]["ok"], 1)

    def test_transport_failure_is_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            log = DataCallLog(Path(directory) / "calls.sqlite3")

            def fail(*args, **kwargs):
                raise TimeoutError("timed out")

            with self.assertRaises(TimeoutError):
                fetch_global(log, getter=fail)
            self.assertEqual(log.recent()[0]["ok"], 0)
            self.assertNotIn("timed out", log.recent()[0]["error"] or "")


if __name__ == "__main__":
    unittest.main()
