"""Offline API integration checks for the local paper-trading platform."""
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

try:
    import httpx  # TestClient's transport dependency is optional in FastAPI installs.
    from fastapi.testclient import TestClient
    from dashboard.backend import app as platform_app
    from data.collectors.collector_base import Candle
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependencies are not installed")
class PlatformApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "platform.sqlite3"
        self.original_db_path = platform_app.DB_FILE
        platform_app.DB_FILE = self.db_path
        platform_app.init_db()
        platform_app.live_armed_until = 0
        # TestClient is deliberately not entered as a context manager, so the
        # startup collector task cannot make a network request during tests.
        self.client = TestClient(platform_app.app)

    def tearDown(self):
        platform_app.DB_FILE = self.original_db_path
        self.temp_dir.cleanup()

    def seed_candles(self, count=40, symbol="BTC-USD"):
        candles = []
        for index in range(count):
            ts = (index + 1) * 3_600_000
            price = 100 + index
            candles.append(Candle(price, price + 1, price - 1, price, 10, ts, ts + 3_600_000))
        platform_app.store_candles(symbol, "1h", candles)
        return candles

    def order_count(self):
        with closing(platform_app.db()) as con:
            return con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    def test_strategy_run_records_signal_without_opt_in_order(self):
        self.seed_candles()

        response = self.client.post("/api/strategy/run", params={"strategy": "ema_12_26_crossover"})

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertIsNone(result["order"])
        self.assertGreater(result["signal_id"], 0)
        history = self.client.get("/api/signals").json()
        self.assertEqual(history[0]["id"], result["signal_id"])
        self.assertEqual(history[0]["direction"], result["direction"])
        self.assertEqual(self.order_count(), 0)

    def test_ai_analysis_records_decision_but_never_places_order(self):
        self.seed_candles()
        before = self.order_count()

        response = self.client.post("/api/ai/analyze", params={"symbol": "BTC-USD"})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["execution"], "decision_only")
        self.assertEqual(self.order_count(), before)
        self.assertEqual(len(self.client.get("/api/ai/decisions").json()), 1)

    def test_automation_requires_paper_arm_and_defaults_off(self):
        self.assertFalse(self.client.get("/api/automation").json()["enabled"])
        blocked = self.client.post("/api/automation", json={"enabled": True})
        self.assertEqual(blocked.status_code, 409)
        self.assertFalse(self.client.get("/api/automation").json()["enabled"])

        with closing(platform_app.db()) as con:
            con.execute("UPDATE runtime_state SET kill_switch=0,armed_until=? WHERE id=1", (platform_app.time.time() + 600,))
            con.commit()
        enabled = self.client.post("/api/automation", json={"enabled": True})
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.assertTrue(enabled.json()["enabled"])
        self.assertEqual(self.order_count(), 0)

    def test_backtest_reads_stored_candles_without_fetching(self):
        self.seed_candles()
        # Exercise the API's compatibility path for candles stored in its
        # original table, without depending on the exchange-aware history table.
        with closing(platform_app.db()) as con:
            con.execute("DELETE FROM candle_history")
            con.commit()

        response = self.client.post("/api/backtest", json={"exchange": "coinbase", "symbol": "BTC-USD",
                                                              "timeframe": "1h", "strategy": "ema_12_26_crossover"})

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["result"]["candles"], 40)
        self.assertIn("Historical simulation only", body["caveat"])
        self.assertEqual(self.order_count(), 0)

    def test_live_status_and_order_fail_closed_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            status = self.client.get("/api/live/status")
            self.assertEqual(status.status_code, 200)
            self.assertFalse(status.json()["configured"])
            self.assertFalse(status.json()["user_armed"])
            self.assertEqual(status.json()["mode"], "paper")

            order = self.client.post("/api/live/orders", json={"symbol": "BTC/USD", "side": "buy", "quote_amount": 25})
            self.assertEqual(order.status_code, 409)
            self.assertIn("not user-armed", order.json()["detail"])
        with closing(platform_app.db()) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
