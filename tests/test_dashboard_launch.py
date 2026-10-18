"""Task 17 launcher: builds the authority gateway from issued control sessions. Fakes and local sockets only."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.autonomous.control import issue_session

from dashboard import launch


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "rt"
        self.sessions = self.runtime / "control" / "sessions"

    def issue(self, **kw):
        args = dict(role="operator", actor="operator:alice", account_id="paper-test", mode="paper", boot_id="boot",
                    ttl_s=600)
        args.update(kw)
        return issue_session(self.sessions, **args)

    def config(self, **kw):
        args = dict(runtime_dir=self.runtime, session_file=None, session_dir=None, state_dir=self.root / "state",
                    host="127.0.0.1", port=8765, epoch=0)
        args.update(kw)
        return launch.LaunchConfig(**args)


class RefusalTests(Fixture):
    def test_non_loopback_host_is_refused(self):
        path = self.issue()
        for host in ("0.0.0.0", "::", "192.168.1.5", "localhost", ""):
            with self.subTest(host=host):
                with self.assertRaises(launch.LaunchRefused) as caught:
                    launch.build(self.config(session_file=path, host=host))
                self.assertIn("LOOPBACK_ONLY", str(caught.exception))

    def test_missing_session_file_is_refused(self):
        with self.assertRaises(launch.LaunchRefused) as caught:
            launch.build(self.config(session_file=self.sessions / "absent.session.json"))
        self.assertIn("SESSION_FILE_MISSING", str(caught.exception))

    def test_loose_permissions_or_symlink_are_refused(self):
        path = self.issue()
        os.chmod(path, 0o644)
        with self.assertRaises(launch.LaunchRefused) as caught:
            launch.build(self.config(session_file=path))
        self.assertIn("SESSION_FILE_PERMISSIONS", str(caught.exception))
        os.chmod(path, 0o600)
        link = self.root / "link.session.json"
        link.symlink_to(path)
        with self.assertRaises(launch.LaunchRefused):
            launch.build(self.config(session_file=link))

    def test_reader_or_expired_or_malformed_session_is_refused(self):
        reader = self.issue(role="reader", actor="reader:bob")
        with self.assertRaises(launch.LaunchRefused) as caught:
            launch.build(self.config(session_file=reader))
        self.assertIn("OPERATOR_SESSION_REQUIRED", str(caught.exception))
        bad = self.sessions / "bad.session.json"
        fd = os.open(bad, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, b"{not json")
        os.close(fd)
        with self.assertRaises(launch.LaunchRefused):
            launch.build(self.config(session_file=bad))
        path = self.issue()
        with patch("time.monotonic_ns", return_value=2**62):
            with self.assertRaises(launch.LaunchRefused) as caught:
                launch.build(self.config(session_file=path))
        self.assertIn("SESSION_EXPIRED", str(caught.exception))

    def test_cli_used_nonces_are_excluded(self):
        path = self.issue()
        issued = json.loads(path.read_text())
        used = path.parent / (path.name + ".used")
        fd = os.open(used, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, json.dumps(issued["nonces"][:3]).encode())
        os.close(fd)
        session = launch.load_session_file(path)
        self.assertEqual(set(issued["nonces"][3:]), set(session.nonces))
        self.assertEqual("uid", session.peer_match)

    def test_session_dir_picks_the_newest_valid_operator_session(self):
        self.issue(role="reader", actor="reader:bob")
        newest = self.issue()
        session = launch.load_session_dir(self.sessions)
        self.assertEqual(json.loads(newest.read_text())["session_id"], session.session_id)
        empty = self.root / "empty"
        empty.mkdir(mode=0o700)
        with self.assertRaises(launch.LaunchRefused):
            launch.load_session_dir(empty)


class FakeStatusClient:
    def __init__(self, epoch=3, status="completed", reason=()):
        self.epoch, self.status, self.reason, self.calls = epoch, status, list(reason), []

    def request(self, raw):
        value = json.loads(raw)
        self.calls.append(value)
        return {"version": 1, "request_id": value["request_id"], "status": self.status,
                "control_epoch": self.epoch if self.status == "completed" else value["expected_epoch"],
                "reason_codes": self.reason, "local_inhibited": False, "cancellations_pending": [],
                "data": {"hard_killed": False} if self.status == "completed" else {}}


class BuildTests(Fixture):
    def test_build_wires_gateway_store_bootstrap_and_discovers_the_epoch(self):
        path = self.issue()
        client = FakeStatusClient(epoch=0)
        result = launch.build(self.config(session_file=path), client=client)
        self.assertEqual(0, result.epoch)
        self.assertEqual("completed", result.status["status"])
        self.assertEqual("status", client.calls[0]["kind"])
        self.assertEqual("operator:alice", client.calls[0]["actor"])
        self.assertTrue((self.root / "state" / "nonces.json").exists())
        self.assertEqual(0o600, os.stat(self.root / "state" / "nonces.json").st_mode & 0o777)
        self.assertEqual("http://127.0.0.1:8765/#bootstrap=" + result.bootstrap_token, result.bootstrap_url)
        self.assertEqual(0, result.gateway.control_epoch)
        self.assertNotIn(result.bootstrap_token, (self.root / "state" / "nonces.json").read_text())

    def test_stale_epoch_is_reported_not_guessed(self):
        path = self.issue()
        client = FakeStatusClient(status="rejected", reason=["STALE_EPOCH"])
        result = launch.build(self.config(session_file=path, epoch=0), client=client)
        self.assertIsNone(result.epoch)
        self.assertIn("EPOCH_UNKNOWN:STALE_EPOCH", result.notes)
        self.assertEqual(1, len(client.calls))  # one probe; epochs are never brute-forced

    def test_unavailable_authority_still_starts_read_only_with_a_note(self):
        path = self.issue()
        result = launch.build(self.config(session_file=path))  # no socket exists
        self.assertIsNone(result.epoch)
        self.assertTrue(any(n.startswith("AUTHORITY_UNAVAILABLE") for n in result.notes), result.notes)

    def test_install_and_bootstrap_flow_through_the_app(self):
        from fastapi.testclient import TestClient
        from dashboard.backend import app as web
        path = self.issue()
        result = launch.build(self.config(session_file=path), client=FakeStatusClient(epoch=2))
        previous = (getattr(web.app.state, "authority_gateway", None), getattr(web.app.state, "allowed_hosts", None))
        self.addCleanup(lambda: (setattr(web.app.state, "authority_gateway", previous[0]),
                                 setattr(web.app.state, "allowed_hosts", previous[1])))
        launch.install(web.app, result)
        self.assertEqual(frozenset({"127.0.0.1"}), web.app.state.allowed_hosts)
        client = TestClient(web.app, base_url="http://127.0.0.1:8765")
        self.addCleanup(client.close)
        reply = client.post("/api/session/bootstrap", json={"token": result.bootstrap_token},
                            headers={"Origin": "http://127.0.0.1:8765"})
        self.assertEqual(200, reply.status_code, reply.text)
        self.assertEqual(2, reply.json()["control_epoch"])
        self.assertEqual(2, client.get("/api/session").json()["control_epoch"])

    def test_main_binds_loopback_and_prints_the_bootstrap_url_once(self):
        path = self.issue()
        seen = {}
        out = io.StringIO()
        with patch.object(launch, "serve", lambda app, host, port: seen.update(host=host, port=port)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = launch.main(["--runtime-dir", str(self.runtime), "--session-file", str(path),
                                "--state-dir", str(self.root / "state"), "--port", "8765"])
        self.assertEqual(0, code)
        self.assertEqual({"host": "127.0.0.1", "port": 8765}, seen)
        printed = out.getvalue()
        self.assertEqual(1, printed.count("#bootstrap="))
        self.assertNotIn(json.loads(path.read_text())["token"], printed)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                launch.main(["--runtime-dir", str(self.runtime), "--session-file", str(path), "--host", "0.0.0.0"])


class FrontendBootstrapTests(unittest.TestCase):
    def test_page_exchanges_the_fragment_token_once_and_adopts_the_epoch(self):
        from dashboard.backend import app as web
        source = Path(web.FRONTEND).read_text()
        self.assertIn("#bootstrap=", source)
        self.assertIn("/api/session/bootstrap", source)
        self.assertIn("history.replaceState", source)  # the token leaves the address bar immediately
        self.assertIn("control_epoch", source)


class RealControlServerTests(Fixture):
    def test_launcher_reads_status_from_a_real_unix_control_server(self):
        from bot.autonomous.control import (ControlService, FileControlJournal, LedgerControlBackend,
                                            UnixControlServer, load_issued_sessions)
        from bot.autonomous.execution import OrderAuthority
        from tests.autonomous.test_ledger import LedgerTests
        path = self.issue()
        socket_path = self.runtime / "control" / "control.sock"
        ready, errors = threading.Event(), []

        def serve():
            fixture = LedgerTests()
            try:
                fixture.setUp()
                authority = OrderAuthority(fixture.ledger, None, book_source=lambda _: None,
                                           market_source=lambda _: None, entry_verifier=lambda *a: False,
                                           clock_us=lambda: 1)
                backend = LedgerControlBackend(fixture.ledger, authority, account_id="paper-test", mode="paper")
                service = ControlService(backend=backend, journal=FileControlJournal(self.root / "journal"),
                                         sessions=load_issued_sessions(self.sessions, boot_id="boot"),
                                         boot_id="boot", monotonic_ns=__import__("time").monotonic_ns)
                with UnixControlServer(socket_path, service) as server:
                    ready.set()
                    server.serve_once()
            except BaseException as exc:
                errors.append(exc)
                ready.set()
            finally:
                fixture.tearDown()

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            self.assertTrue(ready.wait(5))
            self.assertFalse(errors, errors)
            result = launch.build(self.config(session_file=path))
        finally:
            thread.join(5)
        self.assertFalse(errors, errors)
        self.assertEqual(0, result.epoch, result.notes)
        self.assertEqual("completed", result.status["status"])
        self.assertIs(False, result.status["data"]["hard_killed"])


if __name__ == "__main__":
    unittest.main()
