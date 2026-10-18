"""Dashboard read of the paper runtime status: read-only, staleness-labeled."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import httpx  # noqa: F401
    from fastapi.testclient import TestClient
    from dashboard.backend import app as platform_app
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI dependencies are not installed")
class RuntimeDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"AUTONOMOUS_RUNTIME_DIR": self.tmp.name}); env.start(); self.addCleanup(env.stop)
        self.client = TestClient(platform_app.app); self.addCleanup(self.client.close)

    def test_missing_status_is_unavailable_not_an_error(self):
        body = self.client.get("/api/autonomous/runtime").json()
        self.assertFalse(body["available"])

    def test_fresh_and_stale_status(self):
        path = Path(self.tmp.name) / "status.json"
        path.write_text(json.dumps({"mode": "paper", "live": False, "at_us": int(time.time() * 1e6)}))
        body = self.client.get("/api/autonomous/runtime").json()
        self.assertTrue(body["available"]); self.assertFalse(body["stale"]); self.assertFalse(body["status"]["live"])
        path.write_text(json.dumps({"mode": "paper", "at_us": int((time.time() - 600) * 1e6)}))
        self.assertTrue(self.client.get("/api/autonomous/runtime").json()["stale"])

    def test_corrupt_status_is_503_and_endpoint_is_get_only(self):
        (Path(self.tmp.name) / "status.json").write_text("{not json")
        self.assertEqual(503, self.client.get("/api/autonomous/runtime").status_code)
        self.assertEqual(405, self.client.post("/api/autonomous/runtime").status_code)


if __name__ == "__main__":
    unittest.main()
