"""Task 14: operator paper arm, deadman lease renewal by the authority role, uid-bound session issuance. No network."""
import json
import os
import stat
import tempfile
import threading
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from bot.autonomous import control
from bot.autonomous.control import (ControlRejected, ControlService, ControlSession, FileControlJournal, PeerCredentials,
                                    UnixControlServer, issue_session, load_issued_sessions, send_request)
from bot.autonomous.runtime import PaperRuntime, RuntimeConfig
from tests.autonomous.test_release_activation import operator as _session, request


def operator(**kw):
    """Fixture session with enough single-use nonces for a multi-request test."""
    return replace(_session(**kw), nonces={f"n{i}": 10**15 for i in range(1, 41)})
from tests.autonomous.test_runtime import GOOD, FakeRules, Fixed
from tests.autonomous.test_runtime_authority import ACCOUNT, RELEASE, LedgerCase

SECOND = 1_000_000_000


class Mono:
    def __init__(self):
        self.ns = 1_000 * SECOND

    def __call__(self):
        return self.ns


class ArmCase(LedgerCase):
    def setUp(self):
        super().setUp()
        self.mono = Mono()
        self.approve()
        with self.ledger.transaction() as c:  # start disarmed; the fixture release stays active
            c.execute("UPDATE account_control SET permit='none', lease_boot_id=NULL, lease_deadline_mono_ns=NULL")
        self.a = self.adapter(monotonic_ns=self.mono)
        self.journal = FileControlJournal(self.dir / "control")

    def service(self, *sessions, lease_s=30):
        backend = control.LedgerControlBackend(self.ledger, self.a.authority, account_id=ACCOUNT, mode="paper",
                                               lease_ns=lease_s * SECOND)
        return ControlService(backend=backend, journal=self.journal, sessions=list(sessions), boot_id="rt-boot",
                              monotonic_ns=lambda: 1)

    def send(self, service, session, kind, payload, rid, epoch=1, nonce=None):
        self.nonce = getattr(self, "nonce", 0) + 1
        return service.handle(request(session, kind, payload, request_id=rid, nonce=nonce or f"n{self.nonce}", epoch=epoch),
                              PeerCredentials(os.getpid(), os.getuid(), os.getgid()))

    def control_row(self):
        return self.ledger.connection.execute("SELECT * FROM account_control").fetchone()

    def arm(self, service=None, rid="arm-1", epoch=1):
        self.a.mark()
        return self.send(service or self.service(operator()), operator(), "arm",
                         {"reason": "paper", "release_hash": RELEASE}, rid, epoch=epoch)


class ArmTests(ArmCase):
    def test_operator_arms_paper_with_a_bounded_deadman_lease(self):
        receipt = self.arm()
        self.assertEqual("completed", receipt["status"], receipt)
        row = self.control_row()
        self.assertEqual(("entry", 2, "rt-boot"), (row["permit"], row["permit_epoch"], row["lease_boot_id"]))
        self.assertEqual(self.mono.ns + 30 * SECOND, row["lease_deadline_mono_ns"])
        self.assertEqual(2, receipt["control_epoch"])

    def test_lease_is_capped(self):
        self.assertEqual("completed", self.arm(self.service(operator(), lease_s=3600))["status"])
        self.assertEqual(self.mono.ns + control.MAX_LEASE_NS, self.control_row()["lease_deadline_mono_ns"])

    def test_arm_preconditions(self):
        # Unreconciled: no fresh paper mark within the last two seconds.
        self.clock.us += 3_000_000
        receipt = self.send(self.service(operator()), operator(), "arm", {"reason": "x", "release_hash": RELEASE}, "a-unrec")
        self.assertEqual(["UNRECONCILED"], receipt["reason_codes"])
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET crisis_halted=1")
        self.assertEqual(["LATCHED:crisis_halted"], self.arm(rid="a-latch")["reason_codes"])
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET crisis_halted=0")
        self.a.mark()
        receipt = self.send(self.service(operator()), operator(), "arm", {"reason": "x", "release_hash": "e" * 64}, "a-rel")
        self.assertEqual(["RELEASE_MISMATCH"], receipt["reason_codes"])
        view = self.ledger.account_view(ACCOUNT)
        with mock.patch.object(type(self.ledger), "account_view", lambda s, a: replace(view, unknown_intent_ids=("intent-x",))):
            self.assertEqual(["PENDING_UNKNOWN_ORDER"], self.arm(rid="a-unk")["reason_codes"])
        self.assertEqual("none", self.control_row()["permit"])

    def test_expired_release_blocks_arm(self):
        self.clock.us += 11 * 86_400_000_000  # past the fixture release expiry
        self.a.mark()
        receipt = self.send(self.service(operator()), operator(), "arm", {"reason": "x", "release_hash": RELEASE}, "a-exp")
        self.assertIn(receipt["reason_codes"][0], ("RELEASE_EXPIRED", "UNRECONCILED"))
        self.assertEqual("none", self.control_row()["permit"])

    def test_only_operator_can_arm_and_live_arm_is_always_refused(self):
        for role, actor in (("brain", "brain:nightly"), ("reflex", "reflex"), ("reader", "reader"), ("authority", "authority")):
            with self.subTest(role):
                session = operator(session_id="s-" + role, role=role, actor=actor)
                with self.assertRaisesRegex(ControlRejected, "ROLE_FORBIDDEN"):
                    self.send(self.service(session), session, "arm", {"reason": "x", "release_hash": RELEASE}, "r-" + role)

        class LiveBackend:
            account_id, mode = ACCOUNT, "live"
            def dispatch(self, *a, **k):
                raise AssertionError("a live arm must never reach the backend")
        live = replace(operator(session_id="live"), mode="live")
        service = ControlService(backend=LiveBackend(), journal=self.journal, sessions=[live], boot_id="rt-boot",
                                 monotonic_ns=lambda: 1)
        raw = json.loads(request(live, "arm", {"reason": "x", "release_hash": RELEASE}, request_id="live-1"))
        raw["mode"] = "live"
        with self.assertRaisesRegex(ControlRejected, "LIVE_ARM_REQUIRES_TASK22"):
            service.handle(json.dumps(raw).encode(), PeerCredentials(os.getpid(), os.getuid(), os.getgid()))
        self.assertEqual("LIVE_ARM_REQUIRES_TASK22", self.ledger.arm(ACCOUNT, "operator:alice", mode="live",
                                                                     boot_id="rt-boot", lease_ns=SECOND, release_hash=RELEASE))


class LeaseTests(ArmCase):
    def authority_session(self):
        return operator(session_id="auth", role="authority", actor="authority:worker")

    def test_authority_role_renews_and_operator_cannot(self):
        self.arm()
        self.mono.ns += 10 * SECOND
        auth = self.authority_session()
        service = self.service(operator(), auth)
        receipt = self.send(service, auth, "renew", {"reason": "heartbeat"}, "renew-1", epoch=2)
        self.assertEqual("completed", receipt["status"], receipt)
        self.assertEqual(self.mono.ns + 30 * SECOND, self.control_row()["lease_deadline_mono_ns"])
        with self.assertRaisesRegex(ControlRejected, "ROLE_FORBIDDEN"):
            self.send(service, operator(), "renew", {"reason": "x"}, "renew-op", epoch=2, nonce="n2")

    def test_expired_lease_cannot_be_renewed_and_disarms_entries_but_not_exits(self):
        # Open a position while armed, through the fixture's entry path.
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET permit='entry', lease_boot_id='rt-boot', lease_deadline_mono_ns=?",
                      (self.mono.ns + 30 * SECOND,))
        self.open_entry(self.a)
        self.mono.ns += 31 * SECOND  # the deadman lease lapses
        auth = self.authority_session()
        receipt = self.send(self.service(auth), auth, "renew", {"reason": "late"}, "renew-late")
        self.assertEqual(["LEASE_EXPIRED"], receipt["reason_codes"])
        self.observe(self.a)
        entry = self.a.entry("AAVE-USD", D("50"), self.entry_close, decision=self.decision)
        self.assertFalse(entry.ok)
        self.assertEqual("reduce_only", self.control_row()["permit"])  # expiry disarmed entries
        exit_ = self.a.exit("AAVE-USD", self.entry_close + 60_000_000)
        self.assertTrue(exit_.ok, exit_.reasons)


class SessionIssuanceTests(ArmCase):
    def test_issue_session_writes_a_private_uid_bound_short_lived_file(self):
        directory = self.dir / "sessions"
        path = issue_session(directory, role="operator", actor="operator:alice", account_id=ACCOUNT, mode="paper",
                             boot_id="rt-boot", ttl_s=600, nonces=3)
        info = path.stat()
        self.assertEqual(0o600, stat.S_IMODE(info.st_mode))
        self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
        issued = json.loads(path.read_text())
        self.assertEqual(os.getuid(), issued["uid"])
        self.assertNotIn("pid", issued)
        sessions = load_issued_sessions(directory, boot_id="rt-boot")
        self.assertEqual(1, len(sessions))
        self.assertEqual("uid", sessions[0].peer_match)
        self.assertLessEqual(sessions[0].expires_mono_ns, __import__("time").monotonic_ns() + 600 * SECOND)

    def test_model_reflex_brain_and_authority_roles_are_never_issued(self):
        for role in ("brain", "reflex", "authority", "nightly", "model"):
            with self.subTest(role), self.assertRaises(ControlRejected):
                issue_session(self.dir / "s", role=role, actor="x:" + role, account_id=ACCOUNT, mode="paper",
                              boot_id="rt-boot", ttl_s=60)
        with self.assertRaises(ControlRejected):
            issue_session(self.dir / "s", role="operator", actor="operator:alice", account_id=ACCOUNT, mode="paper",
                          boot_id="rt-boot", ttl_s=10 * 3600)  # longer than the cap

    def test_issued_session_authenticates_another_process_of_the_same_uid_only(self):
        directory = self.dir / "sessions"
        path = issue_session(directory, role="operator", actor="operator:alice", account_id=ACCOUNT, mode="paper",
                             boot_id="rt-boot", ttl_s=600)
        session = load_issued_sessions(directory, boot_id="rt-boot")[0]
        service = self.service(session)
        service.monotonic_ns = __import__("time").monotonic_ns
        issued = json.loads(path.read_text())
        raw = json.dumps({"version": 1, "request_id": "status-1", "account_id": ACCOUNT, "mode": "paper", "kind": "status",
                          "actor": "operator:alice", "expected_epoch": 1, "boot_id": "rt-boot", "deadline_mono_ns": 2**62,
                          "session_id": issued["session_id"], "token": issued["token"], "nonce": issued["nonces"][0],
                          "payload": {}}).encode()
        other_pid = PeerCredentials(os.getpid() + 1, os.getuid(), os.getgid())
        self.assertEqual("completed", service.handle(raw, other_pid)["status"])
        with self.assertRaisesRegex(ControlRejected, "AUTHENTICATION_FAILED"):
            service.handle(raw.replace(b"status-1", b"status-2"), PeerCredentials(os.getpid(), os.getuid() + 1, os.getgid()))

    def test_operator_cli_request_over_the_socket_with_an_issued_session(self):
        import time
        directory = self.dir / "sessions"
        path = issue_session(directory, role="operator", actor="operator:alice", account_id=ACCOUNT, mode="paper",
                             boot_id="rt-boot", ttl_s=600)
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET crisis_halted=1")
        self.a.mark()  # crisis clears only on a ready, freshly marked, unarmed account
        service = self.service(*load_issued_sessions(directory, boot_id="rt-boot"))
        service.monotonic_ns = time.monotonic_ns
        socket_path = self.dir / "control" / "sock"
        with UnixControlServer(socket_path, service) as server:
            out = {}
            thread = threading.Thread(target=lambda: out.update(r=send_request(path, socket_path, "clear_latch",
                                                                                {"latch": "crisis", "reason": "ok"}, epoch=1)))
            thread.start()
            server.serve_once()
            thread.join()
        self.assertEqual("completed", out["r"]["status"], out["r"])
        self.assertEqual(0, self.control_row()["crisis_halted"])
        # Each request consumes a fresh nonce from the issued set.
        self.assertEqual(1, len(json.loads((path.parent / (path.name + ".used")).read_text())))


class AwaitingMidnightTests(LedgerCase):
    def test_status_states_awaiting_midnight_mark_with_the_next_eligible_time(self):
        self.approve()
        a = self.adapter()
        rt = PaperRuntime(RuntimeConfig(data_dir=self.dir / "rt", instruments=("AAVE-USD",), accounting="ledger"),
                          book_source=self.books, clock_us=self.clock, reflex=Fixed(GOOD), rules_source=FakeRules(),
                          authority=a)
        self.addCleanup(rt.close)
        self.clock.us += 86_400_000_000 + 61_000_000  # the process was down across midnight
        status = rt.cycle()
        self.assertEqual("LEDGER_AWAITING_MIDNIGHT_MARK", status["ledger"]["state"])
        next_midnight = (self.clock.us // 86_400_000_000 + 1) * 86_400_000_000
        self.assertEqual(next_midnight, status["ledger"]["next_eligible_us"])
        self.assertFalse(status["ledger"]["ready"])


class StaleEpochTests(ArmCase):
    def test_stale_epoch_is_rejected_but_reports_the_current_epoch(self):
        service = self.service(operator())
        for kind, payload in (("status", {}), ("arm", {"reason": "x", "release_hash": RELEASE}),
                              ("clear_latch", {"latch": "crisis", "reason": "x"})):
            with self.subTest(kind):
                receipt = self.send(service, operator(), kind, payload, "stale-" + kind, epoch=7)
                self.assertEqual("rejected", receipt["status"])
                self.assertEqual(["STALE_EPOCH"], receipt["reason_codes"])
                self.assertEqual(1, receipt["data"]["current_epoch"])
        self.assertEqual("none", self.control_row()["permit"])  # nothing was applied


if __name__ == "__main__":
    unittest.main()
