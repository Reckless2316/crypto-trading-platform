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


    def test_ledger_awaiting_midnight_mark_is_projected(self):
        path = Path(self.tmp.name) / "status.json"
        nxt = 1_792_281_600_000_000
        path.write_text(json.dumps({"mode": "paper", "at_us": int(time.time() * 1e6),
                                    "ledger": {"state": "LEDGER_AWAITING_MIDNIGHT_MARK", "next_eligible_us": nxt,
                                               "ready": False}}))
        body = self.client.get("/api/autonomous/runtime").json()
        self.assertEqual("LEDGER_AWAITING_MIDNIGHT_MARK", body["ledger"]["state"])
        self.assertEqual(nxt, body["ledger"]["next_eligible_us"])
        self.assertEqual("2026-10-18T00:00:00Z", body["ledger"]["next_eligible_utc"])
        path.write_text(json.dumps({"mode": "paper", "at_us": int(time.time() * 1e6), "ledger": None}))
        self.assertIsNone(self.client.get("/api/autonomous/runtime").json()["ledger"])

    def write_proposals(self, day, entries):
        directory = Path(self.tmp.name) / "proposals"
        directory.mkdir(exist_ok=True)
        (directory / f"{day}.json").write_text(json.dumps({"version": 1, "day": day, "proposals": entries}))

    def entry(self, status, created, action="propose_schema"):
        return {"kind": "nightly", "status": status, "approved": False, "applied": False, "created_us": created,
                "entry_hash": "h" * 64, "usage": {"input_tokens": 10, "output_tokens": 2},
                "governance": "long text", "inputs_hash": "i" * 64,
                "proposal": None if status != "PENDING_OPERATOR_REVIEW" else {
                    "action": action, "finding": "<img src=x onerror=alert(1)>", "hypothesis": "h",
                    "evidence_refs": ["a"], "schema_diff": "- x\n+ y", "model_id": "claude-opus-5-5"}}

    def test_pending_proposals_are_listed_read_only_newest_first(self):
        self.write_proposals("2026-10-16", [self.entry("PENDING_OPERATOR_REVIEW", 1)])
        self.write_proposals("2026-10-17", [self.entry("PENDING_OPERATOR_REVIEW", 2),
                                            self.entry("PROVIDER_UNAVAILABLE", 3)])
        (Path(self.tmp.name) / "proposals" / "notes.json").write_text("{}")
        (Path(self.tmp.name) / "proposals" / "2026-10-15.json").write_text("{corrupt")
        before = {p.name: p.read_bytes() for p in (Path(self.tmp.name) / "proposals").iterdir()}
        body = self.client.get("/api/autonomous/proposals").json()
        self.assertEqual([2, 1], [p["created_us"] for p in body["pending"]])
        self.assertEqual({"pending": 2, "provider_unavailable": 1}, body["counts"])
        self.assertEqual(["2026-10-15.json"], body["unreadable"])
        first = body["pending"][0]
        self.assertEqual("<img src=x onerror=alert(1)>", first["finding"])  # data, rendered as text by the UI
        self.assertEqual({"input_tokens": 10, "output_tokens": 2}, first["usage"])
        self.assertFalse(first["approved"])
        self.assertNotIn("governance", first)
        after = {p.name: p.read_bytes() for p in (Path(self.tmp.name) / "proposals").iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(405, self.client.post("/api/autonomous/proposals").status_code)

    def test_no_proposals_directory_and_oversized_file(self):
        self.assertEqual([], self.client.get("/api/autonomous/proposals").json()["pending"])
        directory = Path(self.tmp.name) / "proposals"
        directory.mkdir()
        (directory / "2026-10-17.json").write_text(" " * (platform_app.MAX_PROPOSAL_FILE_BYTES + 1))
        self.assertEqual(["2026-10-17.json"], self.client.get("/api/autonomous/proposals").json()["unreadable"])

    def test_frontend_renders_runtime_and_proposals_with_text_content_only(self):
        source = Path(platform_app.FRONTEND).read_text()
        start = source.index("async function loadRuntime")
        end = source.index("\n", start)
        block = source[start:end]
        self.assertIn("/api/autonomous/proposals", block)
        self.assertIn("next_eligible_utc", block)
        self.assertIn("textContent", block)
        self.assertNotIn("innerHTML", block)
        self.assertIn('id="runtimeProposals"', source)


if __name__ == "__main__":
    unittest.main()
