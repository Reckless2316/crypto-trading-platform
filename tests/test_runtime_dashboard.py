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

    def test_proposals_dir_can_be_a_separate_read_only_mount(self):
        other = Path(self.tmp.name) / "brain-out" / "proposals"
        other.mkdir(parents=True)
        (other / "2026-10-17.json").write_text(json.dumps({"proposals": [self.entry("PENDING_OPERATOR_REVIEW", 9)]}))
        with patch.dict(os.environ, {"AUTONOMOUS_PROPOSALS_DIR": str(other)}):
            self.assertEqual([9], [p["created_us"] for p in self.client.get("/api/autonomous/proposals").json()["pending"]])

    def test_bad_files_never_500_the_endpoint(self):
        directory = Path(self.tmp.name) / "proposals"
        directory.mkdir()
        (directory / "2026-10-14.json").write_text("[" * 200_000 + "]" * 200_000)  # RecursionError in json
        (directory / "2026-10-13.json").write_text(json.dumps({"proposals": ["x", 1, {"status": 3}]}))
        (directory / "2026-10-12.json").write_text(json.dumps([1, 2]))
        target = Path(self.tmp.name) / "elsewhere.json"
        target.write_text(json.dumps({"proposals": [self.entry("PENDING_OPERATOR_REVIEW", 5)]}))
        (directory / "2026-10-11.json").symlink_to(target)
        self.write_proposals("2026-10-17-1", [self.entry("PENDING_OPERATOR_REVIEW", 7)])
        reply = self.client.get("/api/autonomous/proposals")
        self.assertEqual(200, reply.status_code)
        body = reply.json()
        self.assertEqual([7], [p["created_us"] for p in body["pending"]])  # rollover files are read
        self.assertIn("2026-10-14.json", body["unreadable"])
        self.assertIn("2026-10-12.json", body["unreadable"])
        self.assertIn("2026-10-11.json", body["unreadable"])  # symlinks are refused

    def test_directory_listing_is_bounded(self):
        directory = Path(self.tmp.name) / "proposals"
        directory.mkdir()
        for i in range(30):
            (directory / f"junk-{i}.txt").write_text("x")
        self.write_proposals("2026-10-17", [self.entry("PENDING_OPERATOR_REVIEW", 1)])
        with patch.object(platform_app, "_PROPOSAL_SCAN_LIMIT", 10):
            body = self.client.get("/api/autonomous/proposals").json()
        self.assertTrue(body["scan_truncated"])
        self.assertLessEqual(len(body["pending"]), 1)

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
        # A proposals failure must not blank the runtime panel: the fetch has its own try/catch.
        self.assertIn("try{const pr=await api('/api/autonomous/proposals')", block)


if __name__ == "__main__":
    unittest.main()
