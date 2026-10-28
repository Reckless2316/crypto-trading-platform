"""SentinelClient against a loopback fake of `sentinel serve` (no external network)."""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from strategies.sentinel_client import SentinelClient, SentinelUnavailable, to_signal

BRIEF = {
    "symbol": "BTC-USDT", "bar_time": "2026-10-28T03:00:00Z", "sentinel_score": -64.0,
    "signal": {"action": "SHORT", "actionable": True, "confidence": 55, "tier": "C",
               "edge_status": "unproven", "summary": "SHORT via the trend route"},
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/api/briefs":
            self._send(200, [BRIEF])
        elif self.path == "/api/snapshot/BTC-USDT":
            self._send(200, {"symbol": "BTC-USDT"})
        else:
            self._send(404, {"error": "not in the live universe"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        args = json.loads(self.rfile.read(length) or b"{}")
        if self.headers.get("Authorization") != "Bearer t0ken":
            self._send(401, {"error": "missing or wrong bearer token"})
            return
        self._send(200, {"tool": self.path.rsplit("/", 1)[-1], "args": args})


class SentinelClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_briefs_and_snapshot(self):
        c = SentinelClient(self.url, token="")
        self.assertEqual(c.briefs()[0]["symbol"], "BTC-USDT")
        self.assertEqual(c.snapshot("btc-usdt")["symbol"], "BTC-USDT")

    def test_tool_call_sends_token_and_arguments(self):
        c = SentinelClient(self.url, token="t0ken")
        out = c.tool("analyze_symbol", symbol="ETH-USDT")
        self.assertEqual(out, {"tool": "analyze_symbol", "args": {"symbol": "ETH-USDT"}})

    def test_errors_and_disallowed_tools(self):
        c = SentinelClient(self.url, token="")
        with self.assertRaises(SentinelUnavailable) as ctx:
            c.snapshot("NOPE-USDT")
        self.assertIn("not in the live universe", str(ctx.exception))
        with self.assertRaises(SentinelUnavailable):
            c.tool("scan_market")  # no token
        with self.assertRaises(ValueError):
            c.tool("place_order")
        with self.assertRaises(ValueError):
            SentinelClient("file:///etc/passwd")
        down = SentinelClient("http://127.0.0.1:1", timeout=0.5)
        with self.assertRaises(SentinelUnavailable):
            down.health()

    def test_to_signal_is_advisory_and_spot_aware(self):
        s = to_signal(BRIEF)
        self.assertEqual(s["direction"], "exit")  # SHORT on a spot-only venue means exit/avoid
        self.assertTrue(s["advisory_only"])
        self.assertAlmostEqual(s["strength"], 0.64)
        not_actionable = dict(BRIEF, signal=dict(BRIEF["signal"], actionable=False))
        self.assertEqual(to_signal(not_actionable)["direction"], "neutral")


if __name__ == "__main__":
    unittest.main()


try:
    import httpx  # noqa: F401
    from fastapi.testclient import TestClient
    from dashboard.backend import app as platform_app
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependencies are not installed")
class SentinelDashboardRouteTests(unittest.TestCase):
    def test_route_reports_unavailable_sentinel_as_502(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"SENTINEL_URL": "http://127.0.0.1:1"}):
            resp = TestClient(platform_app.app).get("/api/sentinel/briefs")
        self.assertEqual(resp.status_code, 502)
        self.assertIn("sentinel unavailable", resp.json()["detail"])
