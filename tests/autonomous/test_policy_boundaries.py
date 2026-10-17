"""Fail-closed candidate policy and durable escalation boundaries."""
import tempfile
import unittest
from pathlib import Path

from bot.autonomous.contracts import (
    AdvisoryRisk, CandidateDecision, ContractViolation, DecisionBatch, Direction,
    DirectionDistribution, Regime, ReviewJob, canonical_bytes, content_hash,
)
from bot.autonomous.ledger import Ledger
from decimal import Decimal
from bot.autonomous.policy import entry_gate, evaluate_candidate


class PolicyGateTests(unittest.TestCase):
    def test_confidence_boundary_and_independent_vetoes(self):
        self.assertFalse(entry_gate(2, .80, "safe", False, "long"))
        self.assertTrue(entry_gate(2, .800001, "safe", False, "long"))
        self.assertFalse(entry_gate(2, .99, "reduce", False, "long"))
        self.assertFalse(entry_gate(3, .99, "safe", True, "long"))
        self.assertFalse(entry_gate(2, .99, "safe", False, "neutral"))
        self.assertFalse(entry_gate(2, .99, "safe", False, "short"))
        for args in ((True, .99, "safe", False, "long"), (2, float("nan"), "safe", False, "long"),
                     (2, .99, "safe", 0, "long")):
            self.assertFalse(entry_gate(*args))

    def test_unbound_inputs_cannot_create_order_proposal(self):
        result = evaluate_candidate(None, None, None, None, None)
        self.assertFalse(result.propose)
        self.assertIsNone(result.proposal)
        self.assertEqual(("POLICY_INPUT_INVALID",), result.reason_codes)


class DurableEscalationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger.open(Path(self.temp.name) / "ledger.sqlite3")
        self.ledger.create_paper_account("paper-test")
        self.now = 7
        self.release_hash = "a" * 64
        self.snapshot_id = content_hash("review-snapshot")
        self.decision_id = "review-decision"
        state_json = canonical_bytes({}).decode()
        self.batch = DecisionBatch(
            "review-request", content_hash({}), self.release_hash, "model-v1",
            20, 7, self.now,
            (CandidateDecision("aave", Regime.TRENDING, Direction.LONG, 2,
                AdvisoryRisk.SAFE, DirectionDistribution(.59, .30, .11),
                .59, .7, .8, .9, .1),), None,
        )
        batch_json = self.batch.to_json()
        with self.ledger.transaction() as c:
            c.execute("INSERT INTO release_bundles VALUES (?, '{}', NULL, ?, ?)",
                (self.release_hash, 100, 1))
            c.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.snapshot_id, "paper-test", self.release_hash, 1, 1,
                 "b" * 64, state_json, "c" * 64, 1, "d" * 64, "{}", 1, None, 5))
            c.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (self.decision_id, self.snapshot_id, "review-request", 20, "model-v1",
                 batch_json, content_hash(__import__("json").loads(batch_json)),
                 "completed", None, self.now, self.now))
            c.execute("INSERT INTO candidate_results VALUES (?,?,?,?,?,?)",
                (self.decision_id, 1, "hold", "{}", "[]", self.now))

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_candidate_pause_and_review_job_commit_atomically_and_dedupe(self):
        key = canonical_bytes(("paper-test", 1, self.release_hash, self.now, "low_confidence")).decode()
        payload = self.incident_payload("low_confidence")
        job = ReviewJob("review-1", key, "low_confidence", content_hash(payload),
            snapshot_id=self.snapshot_id, decision_id=self.decision_id)
        result = self.ledger.pause_candidate_for_review(
            "paper-test", 1, self.release_hash, self.now, "low_confidence", job)
        self.assertTrue(result.appended)
        self.assertFalse(self.ledger.pause_candidate_for_review(
            "paper-test", 1, self.release_hash, self.now, "low_confidence", job).appended)
        row = self.ledger.connection.execute(
            "SELECT review_paused,review_job_id,incident_epoch FROM candidate_control WHERE account_id=? AND candidate_id=1",
            ("paper-test",)).fetchone()
        self.assertEqual((1, "review-1", 7), tuple(row))
        version_before = self.ledger.connection.execute(
            "SELECT version FROM candidate_control WHERE account_id='paper-test' AND candidate_id=1").fetchone()[0]
        self.ledger.pause_candidate_for_review(
            "paper-test", 1, self.release_hash, self.now, "low_confidence", job)
        version_after = self.ledger.connection.execute(
            "SELECT version FROM candidate_control WHERE account_id='paper-test' AND candidate_id=1").fetchone()[0]
        self.assertEqual(version_before, version_after)
        self.assertEqual(1, self.ledger.connection.execute("SELECT count(*) FROM review_jobs").fetchone()[0])

    def test_pause_rejects_wrong_dedupe_key_without_partial_write(self):
        job = ReviewJob("review-1", "wrong", "low_confidence", content_hash({}),
            snapshot_id=self.snapshot_id, decision_id=self.decision_id)
        with self.assertRaises(ContractViolation):
            self.ledger.pause_candidate_for_review(
                "paper-test", 1, self.release_hash, self.now, "low_confidence", job)
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM review_jobs").fetchone()[0])
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM candidate_control").fetchone()[0])

    def incident_payload(self, kind, *, account_id="paper-test", candidate_id=1,
            release_hash=None, incident_epoch=None, decision_id=None, snapshot_id=None,
            batch=None):
        batch = batch or self.batch
        return {
            "account_id": account_id, "candidate_id": candidate_id,
            "release_hash": release_hash or self.release_hash,
            "incident_epoch": self.now if incident_epoch is None else incident_epoch,
            "kind": kind, "decision_id": decision_id or self.decision_id,
            "snapshot_id": snapshot_id or self.snapshot_id,
            "decision_hash": content_hash(__import__("json").loads(batch.to_json())),
            "snapshot_hash": content_hash({}),
            "evidence": {"critical_confidences": {
                "direction": .59, "regime": .7, "quality": .8, "risk": .9
            }, "below_threshold": {"direction": .59}}
            if kind == "low_confidence" else {"regime": "crisis", "direction": "long",
                "direction_confidences": {"long": .59, "short": .3, "neutral": .11}},
        }

    def test_pause_requires_persisted_decision_references_and_payload_hash(self):
        key = canonical_bytes(("paper-test", 1, self.release_hash, self.now, "low_confidence")).decode()
        incomplete = ReviewJob("missing-refs", key, "low_confidence", content_hash(
            self.incident_payload("low_confidence")))
        for job in (incomplete, ReviewJob("wrong-payload", key, "low_confidence", content_hash({}),
                snapshot_id=self.snapshot_id, decision_id=self.decision_id)):
            with self.subTest(job=job.job_id), self.assertRaises(ContractViolation):
                self.ledger.pause_candidate_for_review("paper-test", 1, self.release_hash,
                    self.now, "low_confidence", job)
            self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM review_jobs").fetchone()[0])
            self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM candidate_control").fetchone()[0])

    def test_pause_rejects_cross_account_and_non_low_confidence_evidence(self):
        now = self.ledger._clock_us()
        self.ledger.connection.execute("INSERT INTO accounts VALUES ('other-account','paper','paper','USD',?,0)", (now,))
        self.ledger.connection.execute("INSERT INTO account_control VALUES ('other-account','none',0,0,0,0,0,NULL,NULL,NULL,NULL,NULL,0,?)", (now,))
        self.ledger.connection.execute("INSERT INTO account_readiness(account_id) VALUES ('other-account')")
        other_snapshot = content_hash("foreign-review-snapshot")
        self.ledger.connection.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (other_snapshot, "other-account", self.release_hash, 1, 1, "b" * 64,
             canonical_bytes({}).decode(), "c" * 64, 1, "d" * 64, "{}", 1, None, 5))
        other_batch = __import__("dataclasses").replace(self.batch,
            request_id="other-review-request")
        other_json = other_batch.to_json()
        other_decision = "other-review-decision"
        self.ledger.connection.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (other_decision, other_snapshot, "other-review-request", 20, "model-v1",
             other_json, content_hash(__import__("json").loads(other_json)),
             "completed", None, self.now, self.now))
        self.ledger.connection.execute("INSERT INTO candidate_results VALUES (?,?,?,?,?,?)",
            (other_decision, 1, "hold", "{}", "[]", self.now))
        key = canonical_bytes(("paper-test", 1, self.release_hash, self.now, "low_confidence")).decode()
        cross_account = ReviewJob("cross-account", key, "low_confidence", content_hash({}),
            snapshot_id=other_snapshot, decision_id=other_decision)
        with self.assertRaises(ContractViolation):
            self.ledger.pause_candidate_for_review("paper-test", 1, self.release_hash,
                self.now, "low_confidence", cross_account)
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM review_jobs").fetchone()[0])

        high_snapshot = content_hash("high-confidence-review-snapshot")
        self.ledger.connection.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (high_snapshot, "paper-test", self.release_hash, 1, 1, "b" * 64,
             canonical_bytes({}).decode(), "c" * 64, 1, "d" * 64, "{}", 1, None, 5))
        high_batch = DecisionBatch.from_json(self.high_confidence_batch_json())
        high_json = high_batch.to_json()
        high_decision = "high-confidence-review-decision"
        self.ledger.connection.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (high_decision, high_snapshot, high_batch.request_id, 20, "model-v1", high_json,
             content_hash(__import__("json").loads(high_json)), "completed", None, self.now, self.now))
        self.ledger.connection.execute("INSERT INTO candidate_results VALUES (?,?,?,?,?,?)",
            (high_decision, 1, "hold", "{}", "[]", self.now))
        high_key = canonical_bytes(("paper-test", 1, self.release_hash, self.now, "low_confidence")).decode()
        false_low = ReviewJob("false-low", high_key, "low_confidence",
            content_hash(self.incident_payload("low_confidence", batch=high_batch,
                decision_id=high_decision, snapshot_id=high_snapshot)),
            snapshot_id=high_snapshot, decision_id=high_decision)
        with self.assertRaises(ContractViolation):
            self.ledger.pause_candidate_for_review("paper-test", 1, self.release_hash,
                self.now, "low_confidence", false_low)
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM candidate_control").fetchone()[0])

    def test_crisis_pause_binds_persisted_crisis_decision(self):
        from dataclasses import replace
        crisis_snapshot = content_hash("crisis-review-snapshot")
        self.ledger.connection.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (crisis_snapshot, "paper-test", self.release_hash, 1, 1, "b" * 64,
             canonical_bytes({}).decode(), "c" * 64, 1, "d" * 64, "{}", 1, None, 5))
        selected = replace(self.batch.decisions[0], regime=Regime.CRISIS)
        batch = replace(self.batch, request_id="crisis-review-request", decisions=(selected,))
        batch_json = batch.to_json()
        decision_id = "crisis-review-decision"
        self.ledger.connection.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, crisis_snapshot, batch.request_id, 20, "model-v1", batch_json,
             content_hash(__import__("json").loads(batch_json)), "completed", None, self.now, self.now))
        self.ledger.connection.execute("INSERT INTO candidate_results VALUES (?,?,?,?,?,?)",
            (decision_id, 1, "hold", "{}", "[]", self.now))
        kind = "crisis"
        key = canonical_bytes(("paper-test", 1, self.release_hash, self.now, kind)).decode()
        job = ReviewJob("crisis-review-job", key, kind,
            content_hash(self.incident_payload(kind, batch=batch, decision_id=decision_id,
                snapshot_id=crisis_snapshot)), snapshot_id=crisis_snapshot,
            decision_id=decision_id)
        result = self.ledger.pause_candidate_for_review("paper-test", 1,
            self.release_hash, self.now, kind, job)
        self.assertTrue(result.appended)
        self.assertEqual(1, self.ledger.connection.execute(
            "SELECT review_paused FROM candidate_control WHERE account_id='paper-test' AND candidate_id=1").fetchone()[0])

    def high_confidence_batch_json(self):
        from dataclasses import replace
        high = replace(self.batch.decisions[0], direction_confidence=.8)
        return replace(self.batch, request_id="high-confidence-review-request",
            decisions=(high,)).to_json()

    def test_crisis_halt_is_durable_and_idempotent(self):
        self.assertTrue(self.ledger.latch_account_crisis("paper-test", "incident-1", "brain_unavailable"))
        self.assertFalse(self.ledger.latch_account_crisis("paper-test", "incident-1", "brain_unavailable"))
        row = self.ledger.connection.execute(
            "SELECT crisis_halted,permit,halt_reason FROM account_control WHERE account_id='paper-test'").fetchone()
        self.assertEqual((1, "none", "brain_unavailable"), tuple(row))


class EntryCompositionTests(unittest.TestCase):
    """End-to-end entry: verified edge -> capped sizing -> bounded OrderProposal (never dispatch)."""

    def setUp(self):
        from tests.autonomous.test_sizing import VerifiedSizingTests
        self.s = VerifiedSizingTests("test_exact_quarter_kelly_capped_by_asset_room_and_rounded_down")
        self.s.setUp()
        self.addCleanup(self.s.doCleanups)
        from tests.autonomous.support import account_view
        from bot.autonomous.contracts import RiskLimits
        from bot.autonomous.policy import EntryContext
        self.account = account_view("paper")
        self.fixture_bundle = self.s.fixture.bundle
        self.context = EntryContext(self.s.batch, self.s.artifact, self.s.market, self.s.book, RiskLimits(),
            self.s.ledger, self.s.now, 3, 5_000_000_000)

    def evaluate(self, **changes):
        args = dict(decision=self.s.decision, snapshot=self.s.snapshot, bundle=self.fixture_bundle,
            edge=self.s.edge, account=self.account, context=self.context)
        args.update(changes)
        return evaluate_candidate(args["decision"], args["snapshot"], args["bundle"], args["edge"],
            args["account"], context=args["context"])

    def test_all_gates_pass_into_bounded_entry_proposal(self):
        from bot.autonomous.contracts import Side
        result = self.evaluate()
        self.assertTrue(result.propose, result.reason_codes)
        proposal = result.proposal
        self.assertEqual((Side.BUY, "entry", False), (proposal.side, proposal.action_slot, proposal.reducing_only))
        self.assertEqual((Decimal("4.995"), Decimal("100.10"), 500_000, 25_000_000),
            (proposal.base_quantity, proposal.limit_price, proposal.max_fee_usd_u, proposal.loss_budget_usd_u))
        self.assertEqual("decision-live", proposal.decision.decision_id)
        self.assertEqual(self.fixture_bundle.bundle_hash, proposal.release_hash)
        self.assertEqual(3, proposal.control_epoch)

    def test_each_composed_refusal(self):
        from dataclasses import replace
        cases = {
            "AUTHORITY_PROVENANCE_OR_SIZING_UNAVAILABLE": dict(context=None),
            "ENTRY_GATE_REFUSED": dict(decision=replace(self.s.decision, direction_confidence=.80)),
            "edge_not_reproducible": dict(edge=replace(self.s.edge, calibrated_probability=.99)),
            "RELEASE_EXPIRED": dict(context=replace(self.context, now_us=self.fixture_bundle.bundle.expires_at_us)),
        }
        for reason, change in cases.items():
            with self.subTest(reason):
                result = self.evaluate(**change)
                self.assertFalse(result.propose)
                self.assertIn(reason, result.reason_codes)


class EscalationThroughPolicyTests(EntryCompositionTests):
    """Escalation must fire through evaluate_candidate even when other checks would also refuse."""

    def test_crisis_halts_before_any_other_refusal(self):
        from dataclasses import replace
        from bot.autonomous.contracts import AccountRevision, Regime
        crisis = replace(self.s.decision, regime=Regime.CRISIS)
        for name, change in {"clean": {}, "stale_revision": dict(snapshot=replace(self.s.snapshot, account_revision=AccountRevision(9, 9))),
                             "release_mismatch": dict(snapshot=replace(self.s.snapshot, release_hash="0"*64)),
                             "no_context": dict(context=None)}.items():
            with self.subTest(name):
                result = self.evaluate(decision=crisis, **change)
                self.assertFalse(result.propose)
                self.assertEqual(("crisis_halt",), result.halt_transitions)
                self.assertIn("BRAIN_CRISIS", result.escalation_codes)

    def test_low_confidence_pauses_strictly_below_point_six(self):
        from dataclasses import replace
        from bot.autonomous.contracts import AccountRevision
        stale = replace(self.s.snapshot, account_revision=AccountRevision(9, 9))
        for field in ("direction_confidence", "regime_confidence", "quality_confidence", "risk_confidence"):
            with self.subTest(field):
                low = self.evaluate(decision=replace(self.s.decision, **{field: .5999}), snapshot=stale)
                self.assertIn("PAUSE_CANDIDATE_AND_REVIEW", low.escalation_codes)
                self.assertIn("LOW_CRITICAL_CHOICE_CONFIDENCE", low.reason_codes)
                boundary = self.evaluate(decision=replace(self.s.decision, **{field: .60}))
                self.assertEqual((), boundary.escalation_codes)
                self.assertFalse(boundary.propose)
        # Exactly 0.60 direction confidence does not pause but still fails the 0.80 entry gate.
        self.assertIn("ENTRY_GATE_REFUSED", self.evaluate(decision=replace(self.s.decision, direction_confidence=.60)).reason_codes)

    def test_policy_only_gates_refuse(self):
        from dataclasses import replace
        from bot.autonomous.contracts import AccountRevision, ApprovedBundle, GateApproval
        bundle = self.fixture_bundle.bundle
        ineligible = replace(bundle, candidates=tuple(replace(c, eligible=False, evidence_manifest_hash=None) for c in bundle.candidates))
        approved = ApprovedBundle(ineligible, (GateApproval("evaluation", "test", ineligible.bundle_hash, "2"*64, 1),), 1, "3"*64)
        self.assertIn("CANDIDATE_NOT_ELIGIBLE", self.evaluate(bundle=approved,
            snapshot=replace(self.s.snapshot, release_hash=approved.bundle_hash)).reason_codes)
        self.assertIn("STALE_ACCOUNT_SNAPSHOT", self.evaluate(
            snapshot=replace(self.s.snapshot, account_revision=AccountRevision(1, 1))).reason_codes)


class ProposalThroughRiskTests(EntryCompositionTests):
    """The composed proposal must still pass the deterministic risk layer, which the model cannot override."""

    def assess(self, *, control_changes=None, limits=None):
        from dataclasses import replace
        from bot.autonomous.contracts import ControlState, Mode, Permit, RiskLimits
        from bot.autonomous.risk import assess
        proposal = self.evaluate().proposal
        control = ControlState(Mode.PAPER, Permit.ENTRY, control_epoch=3, boot_id="boot",
            deadman_deadline_mono_ns=10_000_000_000, active_release_hash=proposal.release_hash,
            reconciliation_ready=True, armed=True)
        control = replace(control, **(control_changes or {}))
        now = self.s.now
        account = replace(self.account, reconciled_at_us=now, private_heartbeat_us=now, marked_at_us=now)
        return assess(proposal, account, control, limits or RiskLimits(armed=True, candidate_eligible=True,
            tokenizer_config_hash="a"*64, calibration_config_hash="b"*64),
            replace(self.s.market, book_at_us=now), now, now_mono_ns=1_000_000_000, current_boot_id="boot")

    def test_armed_paper_account_approves_capped_entry(self):
        verdict = self.assess()
        self.assertTrue(verdict.allowed, verdict.reason_codes)

    def test_kill_switch_disarm_and_latches_veto_regardless_of_model(self):
        from bot.autonomous.contracts import RiskLimits
        from dataclasses import replace
        from bot.autonomous.contracts import ContractViolation, ControlState, Permit
        # A killed control cannot even be represented as armed.
        with self.assertRaises(ContractViolation):
            ControlState(permit=Permit.ENTRY, boot_id="b", deadman_deadline_mono_ns=1, active_release_hash="a"*64,
                reconciliation_ready=True, armed=True, hard_killed=True)
        for change, reason in (({"hard_killed": True, "armed": False}, "HARD_KILL"), ({"armed": False}, "DISARMED"),
                               ({"drawdown_latched": True}, "DRAWDOWN_LATCH"), ({"daily_halted": True}, "DAILY_HALT"),
                               ({"crisis_halted": True}, "CRISIS_HALT")):
            with self.subTest(reason):
                verdict = self.assess(control_changes=change)
                self.assertFalse(verdict.allowed)
                self.assertIn(reason, verdict.reason_codes)
        self.assertIn("EXPOSURE_DISABLED", self.assess(limits=RiskLimits()).reason_codes)
