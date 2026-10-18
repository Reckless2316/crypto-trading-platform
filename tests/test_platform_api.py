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
        # Non-managed POSTs need loopback/configured Host, same Origin and CSRF.
        self.previous_hosts = getattr(platform_app.app.state, "allowed_hosts", None)
        platform_app.app.state.allowed_hosts = frozenset({"testserver"})
        token = self.client.get("/api/csrf").json()["csrf_token"]
        self.client.headers.update({"Origin": "http://testserver", "X-CSRF-Token": token})

    def tearDown(self):
        platform_app.app.state.allowed_hosts = self.previous_hosts
        self.client.close()
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

    def test_automation_requires_authenticated_authority_and_defaults_off(self):
        self.assertFalse(self.client.get("/api/automation").json()["enabled"])
        blocked = self.client.post("/api/automation", json={"enabled": True})
        self.assertEqual(blocked.status_code, 401)
        self.assertFalse(self.client.get("/api/automation").json()["enabled"])

        with closing(platform_app.db()) as con:
            con.execute("UPDATE runtime_state SET kill_switch=0,armed_until=? WHERE id=1", (platform_app.time.time() + 600,))
            con.commit()
        enabled = self.client.post("/api/automation", json={"enabled": True})
        self.assertEqual(enabled.status_code, 401, enabled.text)
        self.assertFalse(self.client.get("/api/automation").json()["enabled"])
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

    def test_dashboard_cannot_fill_paper_orders(self):
        self.seed_candles()
        with closing(platform_app.db()) as con:
            con.execute("UPDATE runtime_state SET kill_switch=0,armed_until=? WHERE id=1", (platform_app.time.time() + 600,))
            con.commit()
        order = self.client.post("/api/orders", json={"symbol": "BTC-USD", "side": "buy", "usd_amount": 100})
        flatten = self.client.post("/api/control/flatten")
        strategy = self.client.post("/api/strategy/run", params={"execute": True, "strategy": "ema_12_26_crossover"})
        self.assertEqual(order.status_code, 401)
        self.assertEqual(flatten.status_code, 401)
        self.assertEqual(strategy.status_code, 401, strategy.text)
        self.assertEqual(self.order_count(), 0)
        with closing(platform_app.db()) as con:
            cash = con.execute("SELECT cash FROM account_state").fetchone()[0]
        self.assertEqual(cash, 10000)

    def test_live_status_and_order_fail_closed_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            status = self.client.get("/api/live/status")
            self.assertEqual(status.status_code, 200)
            self.assertFalse(status.json()["configured"])
            self.assertFalse(status.json()["user_armed"])
            self.assertEqual(status.json()["mode"], "paper")

            order = self.client.post("/api/live/orders", json={"symbol": "BTC/USD", "side": "buy", "quote_amount": 25})
            self.assertEqual(order.status_code, 401)
            self.assertIn("session", order.json()["detail"])
            armed = self.client.post("/api/live/arm", json={"acknowledgement": "I UNDERSTAND THIS SENDS REAL ORDERS"})
            self.assertEqual(armed.status_code, 401)
        with closing(platform_app.db()) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0], 0)


    def test_display_posts_without_csrf_are_refused_before_work(self):
        with patch("dashboard.backend.app.fetch_listed_markets", side_effect=AssertionError("fetched")):
            response = self.client.post("/api/market/listings", headers={"X-CSRF-Token": "wrong"})
        self.assertEqual(response.status_code, 403)

    def test_listings_stay_ineligible(self):
        payload = {"source_url": "https://api.coingecko.com/api/v3/coins/markets",
                   "rows": [{"symbol": "AAVE", "id": "aave", "current_price": 1}], "unresolved": ["HYPE"]}
        with patch("dashboard.backend.app.fetch_listed_markets", return_value=payload):
            response = self.client.post("/api/market/listings")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["eligible"])
        self.assertEqual(body["unresolved"], ["HYPE"])
        self.assertEqual(body["rows"][0]["symbol"], "AAVE")
        self.assertFalse(body["rows"][0]["eligible"])


if __name__ == "__main__":
    unittest.main()
