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

    def test_observability_reports_health_and_buckets_unknown_paths(self):
        platform_app._OBS_ROUTES.clear()
        for n in range(30):
            self.client.get(f"/scanner-probe-{n}")
        self.client.get("/health")
        body = self.client.get("/api/observability").json()
        self.assertEqual(body["database"], "ok")
        self.assertEqual(body["mode"], "paper")
        paths = {r["path"]: r for r in body["routes"]}
        self.assertEqual(paths["<unmatched>"]["count"], 30)
        self.assertEqual(paths["<unmatched>"]["errors"], 30)
        self.assertIn("/health", paths)
        self.assertLessEqual(len(platform_app._OBS_ROUTES), 3)   # 30 random URLs did not add 30 keys

    def test_observability_counts_history_and_legacy_candles_once(self):
        from data.history import CandleHistoryStore
        legacy = self.seed_candles(count=10)                       # legacy Coinbase table
        CandleHistoryStore(self.db_path).save("coinbase", "BTC-USD", "1h", legacy[:4])   # mirrored: not double-counted
        CandleHistoryStore(self.db_path).save("okx", "BTC-USDT", "1h", legacy[:3])       # history-only
        self.assertEqual(self.client.get("/api/observability").json()["stored_candles"], 10 + 3)

    def test_sources_registry_reads_store_without_network(self):
        with patch.object(platform_app, "probe_all", side_effect=AssertionError("GET must not probe")):
            body = self.client.get("/api/sources").json()
        self.assertEqual(len(body["sources"]), len(platform_app.SOURCES))
        self.assertTrue(all(row["latest"] is None for row in body["sources"]))
        self.assertEqual(next(r for r in body["sources"] if r["key"] == "okx-us")["us_trading"][:3], "yes")

    def test_sources_refresh_stores_snapshots_and_rejects_unknown_keys(self):
        fake = [{"key": "mempool", "name": "Bitcoin network (mempool.space)", "category": "onchain",
                 "fetched_at": 1.0, "state": "ok", "elapsed_ms": 5, "data": {"fastest_sat_vb": 3}}]
        with patch.object(platform_app, "probe_all", return_value=fake) as probe:
            body = self.client.post("/api/sources/refresh", params={"only": "mempool"}).json()
        self.assertEqual(probe.call_args.args[1], ["mempool"])
        latest = next(r for r in body["sources"] if r["key"] == "mempool")["latest"]
        self.assertEqual((latest["state"], latest["data"]), ("ok", {"fastest_sat_vb": 3}))
        self.assertEqual(self.client.post("/api/sources/refresh", params={"only": "nope"}).status_code, 400)

    def test_archive_candles_are_readable_but_never_fetched(self):
        from data.history import CandleHistoryStore
        rows = [Candle(100 + i, 101 + i, 99 + i, 100 + i, 0, i * 3_600_000, (i + 1) * 3_600_000) for i in range(30)]
        CandleHistoryStore(self.db_path).save(platform_app.ARCHIVE_EXCHANGE, "BTCUSDT", "1h", rows)
        got = self.client.get("/api/candles", params={"exchange": "binance-usdm-archive", "symbol": "BTCUSDT",
                                                     "timeframe": "1h", "limit": 5}).json()
        self.assertEqual([c["close"] for c in got], [125, 126, 127, 128, 129])
        refresh = self.client.post("/api/market/refresh", params={"exchange": "binance-usdm-archive", "symbol": "BTCUSDT"})
        self.assertEqual(refresh.status_code, 422)

    def test_candles_endpoint_returns_latest_window_ascending(self):
        candles = self.seed_candles(count=10)

        rows = self.client.get("/api/candles", params={"limit": 3}).json()

        self.assertEqual([r["ts_open"] for r in rows], [c.ts_open for c in candles[-3:]])
        self.assertEqual(len(self.client.get("/api/candles", params={"limit": 500}).json()), 10)

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


    def test_strategy_queue_scores_markov_and_elliott_without_orders(self):
        self.seed_candles()
        for name in ("markov_regime", "elliott_wave_heuristic"):
            queued = self.client.post("/api/strategy/queue", json={"strategy": name, "symbol": "BTC-USD", "timeframe": "1h"})
            self.assertEqual(queued.status_code, 200, queued.text)
            self.assertFalse(queued.json()["executes_orders"])
        before = self.order_count()
        ran = self.client.post("/api/strategy/queue/run")
        self.assertEqual(ran.status_code, 200, ran.text)
        body = ran.json()
        self.assertFalse(body["executes_orders"])
        self.assertEqual([item["strategy"] for item in body["triggers"]], ["markov_regime", "elliott_wave_heuristic"])
        self.assertTrue(all(item["order"] is None for item in body["triggers"]))
        self.assertEqual(self.order_count(), before)

    def test_runtime_file_command_writes_kill_without_arming(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(platform_app, "_runtime_dir", return_value=Path(directory)):
                response = self.client.post("/api/autonomous/runtime/command", json={"action": "kill"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue((Path(directory) / "KILL").exists())
            self.assertFalse(response.json()["armed"])
            self.assertFalse(response.json()["executes_orders"])
        refused = self.client.post("/api/autonomous/runtime/command", json={"action": "arm"})
        self.assertEqual(refused.status_code, 422)


if __name__ == "__main__":
    unittest.main()
