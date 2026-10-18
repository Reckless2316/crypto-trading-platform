"""Task 15 durable activation, ledger-mode midnight readiness and control-path latch clearing. No network."""
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from bot.autonomous import release as r
from bot.autonomous.contracts import (AdvisoryRisk, CandidateBundle, CandidateDefinition, DecisionReference, GateApproval,
                                      RiskLimits, content_hash)
from bot.autonomous.control import (ControlService, ControlSession, FileControlJournal, LedgerControlBackend, PeerCredentials,
                                    UnixControlClient, UnixControlServer)
from bot.autonomous.ledger import Ledger
from bot.autonomous.runtime import PaperRuntime, RuntimeConfig
from tests.autonomous.test_runtime import GOOD, FakeRules, Fixed
from tests.autonomous.test_runtime_authority import ACCOUNT, HASH, LedgerCase, SNAPSHOT

HOUR = 3_600_000_000
DAY = 86_400_000_000


class Releases:
    """A verifier-authenticated release fixture with real-microsecond windows."""

    def __init__(self, now_us, *, expires_after_us=2 * DAY):
        self.now = now_us
        self.expires_after = expires_after_us
        self.artifacts = tuple(r.ReleaseArtifact(role, ("real-" + role).encode(), now_us + 30 * DAY) for role in r.ARTIFACT_ROLES)
        self.evidence = r.ReleaseArtifact("candidate-evidence:aave", b"aave-evidence", now_us + 30 * DAY)
        owner = self

        class Verifier(r.ReleaseEvidenceVerifier):
            def verify(self, request):
                return r.VerifiedReleaseEvidence(self, request.request_hash, r.compatibility_manifest(request.bundle),
                                                 ("operator:alice",), tuple(content_hash(a) for a in request.approvals),
                                                 owner.now - 1, owner.now + 30 * DAY)
        self.verifier = Verifier()
        self.catalog = {}

    def bundle(self, model="model-v1", parent=None):
        hashes = {a.role: a.content_hash for a in self.artifacts}
        bundle = CandidateBundle(model, hashes["schema"], hashes["dictionary"], hashes["tokenizer"], hashes["policy"],
                                 hashes["risk"], hashes["calibrator"], hashes["code"], hashes["evidence"],
                                 self.now + self.expires_after,
                                 (CandidateDefinition("aave", "AAVE-USD", True, False, (), self.evidence.content_hash),), (),
                                 parent_hash=parent)
        approvals = tuple(GateApproval("specification" if g == "spec" else g, "operator:alice", bundle.bundle_hash,
                                       content_hash(("signed-event", g, model)), self.now - 10) for g in r.GATES)
        self.catalog[bundle.bundle_hash] = (bundle, approvals, self.artifacts + (self.evidence,))
        return bundle


def operator(session_id="op", role="operator", actor="operator:alice", account=ACCOUNT):
    return ControlSession(session_id, actor, role, account, "paper", "rt-boot", "token-" + session_id,
                          PeerCredentials(os.getpid(), os.getuid(), os.getgid()), 10**15, {"n1": 10**15, "n2": 10**15, "n3": 10**15})


def request(session, kind, payload, *, request_id, nonce="n1", epoch=0):
    return json.dumps({"version": 1, "request_id": request_id, "account_id": session.account_id, "mode": "paper", "kind": kind,
                       "actor": session.actor, "expected_epoch": epoch, "boot_id": "rt-boot", "deadline_mono_ns": 10**15,
                       "session_id": session.session_id, "token": session.token, "nonce": nonce, "payload": payload}).encode()


class ActivationCase(LedgerCase):
    def setUp(self):
        super().setUp()
        self.clock.us += 10 * 60_000_000  # 00:10, so the next safe boundary is 01:00
        self.releases = Releases(self.clock.us)
        self.adapter_ = self.adapter()
        self.journal = FileControlJournal(self.dir / "control")
        os.chmod(self.dir / "control", 0o700)

    def service(self, *sessions, on_clear=None):
        backend = LedgerControlBackend(self.ledger, self.adapter_.authority, account_id=ACCOUNT, mode="paper",
                                       release_catalog=self.releases.catalog.get, release_verifier=self.releases.verifier,
                                       on_clear=on_clear)
        return ControlService(backend=backend, journal=self.journal, sessions=list(sessions), boot_id="rt-boot",
                              monotonic_ns=lambda: 1)

    def handle(self, service, raw):
        return service.handle(raw, PeerCredentials(os.getpid(), os.getuid(), os.getgid()))


class LedgerActivationTests(ActivationCase):
    def plan(self, bundle, state=None):
        self.adapter_.mark()  # a fresh paper reconciliation; planning requires a reconciled account
        b, approvals, artifacts = self.releases.catalog[bundle.bundle_hash]
        return r.plan_activation(bundle=b, approvals=approvals, artifacts=artifacts, verifier=self.releases.verifier,
                                 now_us=self.clock.us, state=state or self.ledger.activation_state(ACCOUNT),
                                 session=operator(), boundary_us=(self.clock.us // HOUR + 1) * HOUR)

    def test_activation_commits_only_at_its_boundary_and_preserves_history_and_latches(self):
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET drawdown_latched=1, crisis_halted=1")
        bundle = self.releases.bundle()
        plan = self.plan(bundle)
        self.assertIsInstance(plan, r.ActivationPlan, getattr(plan, "reason_codes", None))
        self.ledger.schedule_activation(plan, "req-activate-1")
        self.assertTrue(self.ledger.activation_state(ACCOUNT).boundary_in_progress)
        self.assertIsNone(self.ledger.apply_due_activation(ACCOUNT))  # before the boundary nothing changes
        self.assertIsNone(self.ledger.connection.execute("SELECT active_release_hash FROM account_control").fetchone()[0])
        self.clock.us = plan.effective_at_us
        self.assertEqual("applied", self.ledger.apply_due_activation(ACCOUNT))
        row = self.ledger.connection.execute("SELECT * FROM account_control").fetchone()
        self.assertEqual(bundle.bundle_hash, row["active_release_hash"])
        self.assertEqual((1, 1), (row["drawdown_latched"], row["crisis_halted"]))  # latches are never reset
        state = self.ledger.activation_state(ACCOUNT)
        self.assertEqual((bundle.bundle_hash,), state.history)
        self.assertEqual(bundle.bundle_hash, state.active.bundle_hash)
        self.assertFalse(state.boundary_in_progress)
        self.assertEqual(1, self.ledger.connection.execute("SELECT COUNT(*) FROM release_events WHERE kind='activation'").fetchone()[0])
        # The registry survives reopening (release identity passes the ledger integrity check).
        self.ledger.close()
        reopened = self.open_ledger()
        self.assertEqual((bundle.bundle_hash,), reopened.activation_state(ACCOUNT).history)

    def test_forward_release_and_rollback_keep_growing_history(self):
        first = self.releases.bundle()
        plan = self.plan(first)
        self.ledger.schedule_activation(plan, "req-a")
        self.clock.us = plan.effective_at_us + 1
        self.ledger.apply_due_activation(ACCOUNT)
        self.releases.now = self.clock.us
        child = self.releases.bundle("model-v2", parent=first.bundle_hash)
        plan = self.plan(child)
        self.assertIsInstance(plan, r.ActivationPlan, getattr(plan, "reason_codes", None))
        self.ledger.schedule_activation(plan, "req-b")
        self.clock.us = plan.effective_at_us
        self.ledger.apply_due_activation(ACCOUNT)
        self.assertEqual((first.bundle_hash, child.bundle_hash), self.ledger.activation_state(ACCOUNT).history)

    def test_apply_refuses_when_state_moved_and_never_retries(self):
        bundle = self.releases.bundle()
        plan = self.plan(bundle)
        self.ledger.schedule_activation(plan, "req-x")
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET permit='entry'")  # armed after planning
        self.clock.us = plan.effective_at_us
        self.assertEqual("refused:ARMED", self.ledger.apply_due_activation(ACCOUNT))
        self.assertIsNone(self.ledger.apply_due_activation(ACCOUNT))  # a refused plan is closed, not re-attempted
        self.assertIsNone(self.ledger.connection.execute("SELECT active_release_hash FROM account_control").fetchone()[0])

    def test_one_scheduled_plan_at_a_time(self):
        bundle = self.releases.bundle()
        plan = self.plan(bundle)
        self.ledger.schedule_activation(plan, "req-1")
        with self.assertRaises(Exception):
            self.ledger.schedule_activation(replace(plan, actor="operator:bob"), "req-2")

    def test_entries_consult_entries_allowed_for_registry_releases(self):
        bundle = self.releases.bundle()
        plan = self.plan(bundle)
        self.ledger.schedule_activation(plan, "req-e")
        self.clock.us = plan.effective_at_us
        self.ledger.apply_due_activation(ACCOUNT)
        self.assertTrue(self.ledger.entries_allowed_now(ACCOUNT))
        self.clock.us = bundle.expires_at_us
        self.assertFalse(self.ledger.entries_allowed_now(ACCOUNT))


class ControlActivationTests(ActivationCase):
    def test_operator_activates_through_the_authenticated_control_protocol(self):
        bundle = self.releases.bundle()
        service = self.service(operator())  # the handler records its own fresh paper reconciliation
        receipt = self.handle(service, request(operator(), "activate_release",
                                               {"release_hash": bundle.bundle_hash, "reason": "ship"}, request_id="act-1"))
        self.assertEqual("pending", receipt["status"], receipt)
        self.assertEqual((self.clock.us // HOUR + 1) * HOUR, receipt["data"]["effective_at_us"])
        self.assertTrue(self.ledger.activation_state(ACCOUNT).boundary_in_progress)

    def test_model_nightly_and_reflex_roles_cannot_activate_or_clear(self):
        bundle = self.releases.bundle()
        for role, actor in (("brain", "brain:nightly"), ("reflex", "reflex"), ("reader", "reader")):
            for kind, payload in (("activate_release", {"release_hash": bundle.bundle_hash, "reason": "x"}),
                                  ("clear_latch", {"latch": "crisis", "reason": "x"})):
                with self.subTest(role=role, kind=kind):
                    session = operator(session_id=role + kind, role=role, actor=actor)
                    service = self.service(session)
                    with self.assertRaisesRegex(Exception, "ROLE_FORBIDDEN"):
                        self.handle(service, request(session, kind, payload, request_id=role + kind))
        self.assertFalse(self.ledger.activation_state(ACCOUNT).boundary_in_progress)

    def test_unknown_release_is_refused(self):
        receipt = self.handle(self.service(operator()), request(operator(), "activate_release",
                              {"release_hash": "e" * 64, "reason": "x"}, request_id="act-u"))
        self.assertEqual(["RELEASE_UNKNOWN"], receipt["reason_codes"])

    def test_no_auto_activation_without_an_operator_request(self):
        self.releases.bundle()
        self.clock.us += 2 * HOUR
        self.assertIsNone(self.ledger.apply_due_activation(ACCOUNT))
        self.assertEqual((), self.ledger.activation_state(ACCOUNT).history)


class LatchClearTests(ActivationCase):
    def test_operator_clears_latches_over_a_real_socket_and_model_cannot(self):
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET crisis_halted=1")
        cleared = []
        op, model = operator(), operator(session_id="model", role="brain", actor="brain:nightly")
        service = self.service(op, model, on_clear=cleared.append)
        path = self.dir / "control" / "sock"
        with UnixControlServer(path, service) as server:
            client = UnixControlClient(path)
            for session, rid, expected in ((model, "clear-model", "rejected"), (op, "clear-op", "completed")):
                # The client runs in a thread; the server (which owns the ledger connection) stays on this thread.
                out = {}
                thread = threading.Thread(target=lambda: out.update(result=client.request(
                    request(session, "clear_latch", {"latch": "crisis", "reason": "reviewed"}, request_id=rid))))
                thread.start()
                server.serve_once()
                thread.join()
                result = out["result"]
                self.assertEqual(expected, result["status"], result)
                if session is model:
                    self.assertEqual(["ROLE_FORBIDDEN"], result["reason_codes"])
                    self.assertEqual(1, self.ledger.connection.execute("SELECT crisis_halted FROM account_control").fetchone()[0])
        self.assertEqual(0, self.ledger.connection.execute("SELECT crisis_halted FROM account_control").fetchone()[0])
        self.assertEqual(["crisis"], cleared)

    def test_kill_clear_requires_no_unresolved_orders_and_never_rearms(self):
        self.approve()
        self.adapter_.kill()
        service = self.service(operator())
        receipt = self.handle(service, request(operator(), "clear_latch", {"latch": "kill", "reason": "ok"},
                                               request_id="k1", epoch=1))
        self.assertEqual("completed", receipt["status"], receipt)
        row = self.ledger.connection.execute("SELECT hard_killed, permit FROM account_control").fetchone()
        self.assertEqual((0, "none"), tuple(row))  # cleared kill leaves the account unarmed

    def test_drawdown_clear_is_refused_while_the_loss_limit_is_breached(self):
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET drawdown_latched=1")
        from unittest import mock
        with mock.patch("bot.autonomous.risk.loss_triggers", return_value=(True, False)):
            receipt = self.handle(self.service(operator()), request(operator(), "clear_latch",
                                  {"latch": "drawdown", "reason": "x"}, request_id="d1"))
        self.assertEqual(["LOSS_LIMIT_STILL_BREACHED"], receipt["reason_codes"])
        self.assertEqual(1, self.ledger.connection.execute("SELECT drawdown_latched FROM account_control").fetchone()[0])

    def test_runtime_refuses_local_latch_clear_in_ledger_mode(self):
        rt = PaperRuntime(RuntimeConfig(data_dir=self.dir / "rt", instruments=("AAVE-USD",), accounting="ledger"),
                          book_source=self.books, clock_us=self.clock, reflex=Fixed(GOOD), rules_source=FakeRules(),
                          authority=self.adapter_)
        self.addCleanup(rt.close)
        rt.db.execute("INSERT INTO latches(kind,action,reason,at_us) VALUES ('CRISIS_HALT','set','t',0)")
        with self.assertRaisesRegex(ValueError, "control"):
            rt.clear_latch("CRISIS_HALT")
        self.assertIn("CRISIS_HALT", rt.latched())
        rt.control_cleared("crisis")  # the control backend's on_clear hook (operator-authenticated upstream)
        self.assertNotIn("CRISIS_HALT", rt.latched())


class MidnightReadinessTests(LedgerCase):
    def test_pre_midnight_reads_and_exact_midnight_mark_make_the_account_ready(self):
        self.approve()
        a = self.adapter()
        self.open_entry(a)  # hold AAVE so the boundary mark needs executable depth
        rt = PaperRuntime(RuntimeConfig(data_dir=self.dir / "rt", instruments=("AAVE-USD",), accounting="ledger",
                                        paper_armed=True, horizon_candles=3000), book_source=self.books, clock_us=self.clock, reflex=Fixed(GOOD),
                          rules_source=FakeRules(), authority=a)
        self.addCleanup(rt.close)
        midnight = self.clock.us + DAY
        self.clock.us = midnight - 10_000_000
        stopped = {"after": midnight + 70_000_000}

        def sleep(seconds):
            self.clock.us += int(seconds * 1_000_000)
        rt.serve(lambda: self.clock.us >= stopped["after"], sleep=sleep)
        marks = self.ledger.connection.execute("SELECT event_at_us FROM accounting_evidence WHERE kind='mark' AND event_at_us=?",
                                               (midnight,)).fetchall()
        self.assertEqual(1, len(marks))
        status = json.loads((self.dir / "rt" / "status.json").read_text())
        self.assertTrue(status["ledger"]["ready"])

    def test_without_the_boundary_mark_the_next_day_is_not_ready(self):
        self.approve()
        a = self.adapter()
        self.open_entry(a)
        self.clock.us += DAY + 1_000_000
        self.observe(a)
        self.assertFalse(a.mark())


if __name__ == "__main__":
    unittest.main()
