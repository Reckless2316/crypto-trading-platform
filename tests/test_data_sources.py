import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import requests

from data.call_log import DataCallLog
from data.sources import SOURCES, Fetcher, SnapshotStore, probe, probe_all
from data.sources import catalog

# Every row of the operator's API STATUS list must have a registry entry ("Bentley" and
# "Market Intel Feed" are labels on that list, not data sources).
REQUESTED = [
    "Binance Spot", "Binance Futures", "Bybit", "OKX", "Deribit", "Hyperliquid", "Gate.io", "KuCoin Futures",
    "Coinbase Exchange", "Kraken", "BitMEX", "Bitget", "MEXC", "HTX", "Phemex", "dYdX", "Bitfinex", "WEEX",
    "CoinGecko", "Fear & Greed", "Coinalyze", "API Data Quality Log",
    "Economic calendar (ForexFactory)", "Long/short accounts (Binance, Bybit, OKX)", "Deribit volatility index",
    "Coinbase and Korea premiums", "Binance quarterly basis", "Macro markets (TradingView, CNBC fallback)",
    "Spot ETF flows (Farside, SoSoValue)", "CFTC positioning on CME", "Bitfinex margin longs and shorts",
    "OKX margin loan ratio", "Polymarket odds", "Hyperliquid top traders", "Bitcoin network (mempool.space)",
]


class Response:
    def __init__(self, status=200, payload=None, text=None, content=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)
        self.content = content if content is not None else self.text.encode()

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class Session:
    """Routes by URL substring; the first matching route wins. Records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, params=None, json=None, timeout=None, headers=None):
        self.calls.append((method, url, params, headers))
        for fragment, response in self.routes:
            if fragment in url:
                return response
        raise AssertionError(f"unexpected request {url}")


def setUpModule():
    # Keep probes hermetic: never read the operator's real .env during tests.
    global _env_patch
    _env_patch = mock.patch.object(catalog, "ENV_FILE", Path("/nonexistent/surge/.env"))
    _env_patch.start()


def tearDownModule():
    _env_patch.stop()


def fetcher(routes, log=None):
    return Fetcher(log, Session(routes))


class RegistryTests(unittest.TestCase):
    def test_every_requested_source_is_registered_once(self):
        names = [s.name for s in SOURCES]
        self.assertEqual(sorted(set(names)), sorted(names))
        self.assertEqual([n for n in REQUESTED if n not in names], [])

    def test_exchanges_declare_us_trading_access(self):
        for s in SOURCES:
            if s.category == "exchange":
                self.assertTrue(s.us_trading, s.key)


class ProbeTests(unittest.TestCase):
    def test_ok_probe_and_call_log(self):
        with tempfile.TemporaryDirectory() as d:
            log = DataCallLog(Path(d) / "log.sqlite3")
            f = fetcher([("mempool.space/api/v1/fees", Response(payload={"fastestFee": 5, "hourFee": 2})),
                         ("mempool.space/api/mempool", Response(payload={"count": 10, "vsize": 99}))], log)
            r = probe(catalog.BY_KEY["mempool"], f)
            self.assertEqual(r["state"], "ok")
            self.assertEqual(r["data"]["fastest_sat_vb"], 5)
            self.assertEqual([c["source"] for c in log.recent()], ["mempool", "mempool"])

    def test_states_needs_key_geo_blocked_error(self):
        with mock.patch.dict(os.environ, {"COINALYZE_API_KEY": ""}):
            self.assertEqual(probe(catalog.BY_KEY["coinalyze"], fetcher([]))["state"], "needs_key")
        self.assertEqual(probe(catalog.BY_KEY["bitmex"], fetcher([("bitmex", Response(403, {}))]))["state"], "geo_blocked")
        self.assertEqual(probe(catalog.BY_KEY["kraken"], fetcher([("kraken", Response(500, {}))]))["state"], "error")

    def test_binance_spot_falls_back_to_binance_us_on_451(self):
        f = fetcher([("api.binance.com", Response(451, {})),
                     ("api.binance.us", Response(payload={"lastPrice": "80000", "quoteVolume": "5"}))])
        r = probe(catalog.BY_KEY["binance-spot"], f)
        self.assertEqual((r["state"], r["data"]["venue"]), ("ok", "binance.us (fallback)"))

    def test_binance_futures_falls_back_to_public_archive(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("m.csv", "create_time,symbol,sum_open_interest,v,a,b,count_long_short_ratio,c\n"
                                "2026-10-24 23:55:00,BTCUSDT,95000.5,1,1,1,2.1,1\n")
        f = fetcher([("fapi.binance.com", Response(451, {})),
                     ("data.binance.vision", Response(200, None, text="", content=buf.getvalue()))])
        with mock.patch.dict(os.environ, {"COINALYZE_API_KEY": ""}):
            r = probe(catalog.BY_KEY["binance-futures"], f)
        self.assertEqual(r["state"], "ok")
        self.assertEqual((r["data"]["open_interest_btc"], r["data"]["account_long_short"]), (95000.5, 2.1))

    def test_coinalyze_key_goes_in_a_header_not_the_url(self):
        markets = [{"symbol": "BTCUSD.6", "exchange": "6", "is_perpetual": True, "base_asset": "BTC", "quote_asset": "USD"},
                   {"symbol": "BTCUSDT.6", "exchange": "6", "is_perpetual": True, "base_asset": "BTC", "quote_asset": "USDT"}]
        f = fetcher([("/exchanges", Response(payload=[{"code": "6", "name": "Bybit"}])),
                     ("/future-markets", Response(payload=markets)),
                     ("/open-interest", Response(payload=[{"symbol": "BTCUSDT.6", "value": 1e9}])),
                     ("/funding-rate", Response(payload=[{"symbol": "BTCUSDT.6", "value": 0.01}])),
                     ("bybit.com", Response(403, {}))])
        with mock.patch.dict(os.environ, {"COINALYZE_API_KEY": "secret-key"}):
            r = probe(catalog.BY_KEY["bybit"], f)
        self.assertEqual((r["state"], r["data"]["open_interest_usd"]), ("ok", 1e9))
        self.assertEqual(r["data"]["via"], "coinalyze (BTCUSDT.6)")      # USDT perp preferred over coin-margined
        self.assertAlmostEqual(r["data"]["funding"], 0.0001)             # Coinalyze percent -> fraction
        coinalyze_calls = [c for c in f.session.calls if "coinalyze" in c[1]]
        self.assertTrue(all("secret-key" not in c[1] and "secret-key" not in json.dumps(c[2]) for c in coinalyze_calls))
        self.assertTrue(all(c[3]["api_key"] == "secret-key" for c in coinalyze_calls))

    def test_long_short_survives_one_venue_refusing(self):
        f = fetcher([("fapi.binance.com", Response(500, {})),
                     ("bybit.com", Response(403, {})),
                     ("okx.com", Response(payload={"data": [["1", "2.13"]]}))])
        r = probe(catalog.BY_KEY["long-short"], f)
        self.assertEqual(r["state"], "ok")
        self.assertEqual(r["data"]["okx"], 2.13)
        self.assertEqual(r["data"]["bybit_error"], "geo-blocked")

    def test_farside_total_column_parses_outflows(self):
        html = ('<tr> <td><span class="tabletext">22 Oct 2026</span></td><td><span class="tabletext">1.0</span></td>'
                '<td><span class="tabletext">5.0</span></td></tr>'
                '<tr> <td><span class="tabletext">23 Oct 2026</span></td><td><span class="tabletext">(110.3)</span></td>'
                '<td><span class="tabletext">0.0</span></td><td><span class="tabletext">(137.9)</span></td></tr>')
        with mock.patch.dict(os.environ, {"SOSOVALUE_API_KEY": ""}):
            r = probe(catalog.BY_KEY["etf-flows"], fetcher([("farside", Response(200, None, text=html))]))
        self.assertEqual(r["data"], {"via": "farside", "date": "23 Oct 2026", "btc_net_flow_musd": -137.9})

    def test_snapshot_store_keeps_latest_per_source(self):
        with tempfile.TemporaryDirectory() as d:
            store = SnapshotStore(Path(d) / "s.sqlite3")
            store.save([{"key": "a", "fetched_at": 1, "state": "error", "elapsed_ms": 3, "error": "x"}])
            store.save([{"key": "a", "fetched_at": 2, "state": "ok", "elapsed_ms": 3, "data": {"v": 1}}])
            self.assertEqual(store.latest()["a"]["data"], {"v": 1})
            self.assertEqual([h["state"] for h in store.history("a")], ["ok", "error"])

    def test_data_key_reads_only_named_keys_from_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("SURGE_OKX_API_SECRET=trading-secret\nCOINALYZE_API_KEY=from-file\n")
            with mock.patch.object(catalog, "ENV_FILE", env), mock.patch.dict(os.environ, {"COINALYZE_API_KEY": ""}):
                self.assertEqual(catalog.data_key("COINALYZE_API_KEY"), "from-file")
                with self.assertRaises(ValueError):
                    catalog.data_key("SURGE_OKX_API_SECRET")
            with mock.patch.object(catalog, "ENV_FILE", env), mock.patch.dict(os.environ, {"COINALYZE_API_KEY": "from-env"}):
                self.assertEqual(catalog.data_key("COINALYZE_API_KEY"), "from-env")

    def test_probe_all_never_raises(self):
        results = probe_all(fetcher([("", Response(500, {}))]), ["kraken", "okx", "mempool"])
        self.assertEqual({r["state"] for r in results}, {"error"})


if __name__ == "__main__":
    unittest.main()
