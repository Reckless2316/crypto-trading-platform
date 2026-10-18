"""Causal ingestion, paper outcome verifier, attestation job and the attested entry path. No network.

Everything here goes through the real APIs: books are SourceEvents in the StateEngine archive, decisions are
persisted over persist_encoded_state snapshots, labels are resolved by the paper verifier from archived books,
the calibrator is built and attested from those resolved labels, and an operator-approved release names it.
The only fixture is the ProviderTokenCounter: production has no tokenizer proof, which is exactly the blocker.
"""
import base64
import json
import math
import os
import unittest
from dataclasses import replace
from decimal import Decimal as D

from bot.autonomous import release as r
from bot.autonomous.contracts import (AdvisoryRisk, CandidateBundle, CandidateDefinition, CausalCutoffs, ContractViolation,
                                      Direction, GateApproval, Regime, TokenCount, content_hash)
from bot.autonomous.control import ControlSession, PeerCredentials
from bot.autonomous.runtime import MarketRules as RuntimeRules, PaperRuntime, RuntimeConfig
from bot.autonomous.runtime_formal import (FormalPipeline, PaperBookOutcomeVerifier, configure_paper_verifier,
                                           paper_execution_policy_hash)
from tests.autonomous.test_authority_worker import ProviderLike, formal_long
from tests.autonomous.test_runtime import GOOD, FakeRules, Fixed, online_rules
from tests.autonomous.test_runtime_authority import ACCOUNT, LedgerCase

HOUR = 3_600_000_000
DAY = 24 * HOUR
SECOND = 1_000_000_000


class FixtureTokenCounter:
    """Test-only stand-in for a provider tokenizer proof (production has none)."""

    def count_state(self, raw):
        return TokenCount(math.ceil(len(raw) / 3), "fixture-tokenizer", "fixture-provider",
                          "provider_approved_upper_bound", "f" * 64)


class Mono:
    def __init__(self):
        self.ns = 1_000 * SECOND

    def __call__(self):
        return self.ns


class Release:
    """Verifier-authenticated releases with long-lived artifacts (real-microsecond windows)."""

    def __init__(self, now_us, lifetime_us=120 * DAY):
        self.now, self.lifetime = now_us, lifetime_us
        self.evidence = r.ReleaseArtifact("candidate-evidence:aave", b"aave-evidence", now_us + 2 * lifetime_us)
        owner = self

        class Verifier(r.ReleaseEvidenceVerifier):
            def verify(self, request):
                return r.VerifiedReleaseEvidence(self, request.request_hash, r.compatibility_manifest(request.bundle),
                                                 ("operator:alice",), tuple(content_hash(a) for a in request.approvals),
                                                 owner.now - 1, owner.now + 2 * owner.lifetime)
        self.verifier = Verifier()

    def build(self, calibrator_payload=b"real-calibrator", parent=None):
        artifacts = {role: r.ReleaseArtifact(role, ("real-" + role).encode() if role != "calibrator" else calibrator_payload,
                                             self.now + 2 * self.lifetime) for role in r.ARTIFACT_ROLES}
        h = {role: a.content_hash for role, a in artifacts.items()}
        bundle = CandidateBundle("model-v1", h["schema"], h["dictionary"], h["tokenizer"], h["policy"], h["risk"],
                                 h["calibrator"], h["code"], h["evidence"], self.now + self.lifetime,
                                 (CandidateDefinition("aave", "AAVE-USD", True, False, (), self.evidence.content_hash),), (),
                                 parent_hash=parent)
        approvals = tuple(GateApproval("specification" if g == "spec" else g, "operator:alice", bundle.bundle_hash,
                                       content_hash(("signed", g, bundle.bundle_hash)), self.now - 10) for g in r.GATES)
        return bundle, approvals, tuple(artifacts.values()) + (self.evidence,)


def operator(account=ACCOUNT):
    return ControlSession("op", "operator:alice", "operator", account, "paper", "rt-boot", "tok",
                          PeerCredentials(os.getpid(), os.getuid(), os.getgid()), 10**18, {"n": 10**18})


class FormalCase(LedgerCase):
    def setUp(self):
        super().setUp()
        self.mono = Mono()
        self.a = self.adapter(monotonic_ns=self.mono)
        self.releases = Release(self.clock.us)

    def at(self, us):
        self.mono.ns += max(0, us - self.clock.us) * 1000
        self.clock.us = us

    def activate(self, bundle, approvals, artifacts):
        """Operator plan -> ledger schedule -> apply at the next hour on a reconciled account."""
        self.releases.now = self.clock.us
        self.a.mark()
        plan = r.plan_activation(bundle=bundle, approvals=approvals, artifacts=artifacts, verifier=self.releases.verifier,
                                 now_us=self.clock.us, state=self.ledger.activation_state(ACCOUNT), session=operator(),
                                 boundary_us=(self.clock.us // HOUR + 1) * HOUR)
        self.assertIsInstance(plan, r.ActivationPlan, getattr(plan, "reason_codes", None))
        self.ledger.schedule_activation(plan, "activate-" + bundle.bundle_hash[:8], account_id=ACCOUNT)
        self.at(plan.effective_at_us + 500_000)
        self.a.mark()
        self.assertEqual("applied", self.ledger.apply_due_activation(ACCOUNT))
        return plan.approved

    def pipeline(self, counter=True):
        p = FormalPipeline(self.a, self.dir / "archive", token_counter=FixtureTokenCounter() if counter else None)
        configure_paper_verifier(self.ledger, PaperBookOutcomeVerifier(self.ledger, fee_rate=D("0.006")))
        return p

    def ingest(self, pipeline, price, at_us):
        """One public book fetched at ``at_us`` (exchange time 0.5 s earlier)."""
        self.at(at_us)
        self.price["v"] = price
        book = self.books("AAVE-USD")
        event = at_us - self.books.lag_us
        return pipeline.ingest_book("AAVE-USD", book["bids"], book["asks"], event_us=event, received_us=at_us,
                                    sequence=book["sequence"])


class IngestionTests(FormalCase):
    def test_runtime_feeds_the_state_engine_and_formal_features_match_the_paper_snapshot(self):
        bundle, approvals, artifacts = self.releases.build()
        self.activate(bundle, approvals, artifacts)
        pipeline = self.pipeline()
        rt = PaperRuntime(RuntimeConfig(data_dir=self.dir / "rt", instruments=("AAVE-USD",), accounting="ledger"),
                          book_source=self.books, clock_us=self.clock, reflex=Fixed(GOOD), rules_source=FakeRules(),
                          authority=self.a, formal=pipeline)
        self.addCleanup(rt.close)
        end = self.clock.us + 200_000_000
        rt.serve(lambda: self.clock.us >= end, sleep=lambda s: self.at(self.clock.us + int(s * 1_000_000)))
        events = self.ledger.connection.execute("SELECT event_at_us, received_at_us, sequence FROM source_events").fetchall()
        self.assertGreaterEqual(len(events), 5)
        self.assertEqual(sorted(e["sequence"] for e in events), [e["sequence"] for e in events])  # monotonic sequence
        self.assertTrue(all(e["received_at_us"] >= e["event_at_us"] for e in events))
        last_close = rt.db.execute("SELECT MAX(candle_close_us) FROM snapshots").fetchone()[0]
        formal = pipeline.engine.snapshot("AAVE-USD", CausalCutoffs(last_close, self.clock.us, 0, "rt-boot"), eligibility=True)
        features = {f.name: f for f in formal.features}
        paper = json.loads(rt.db.execute("SELECT wire FROM snapshots WHERE candle_close_us=?", (last_close,)).fetchone()[0])
        for name, key, tolerance in (("mid", "mid", 1e-6), ("spread_bps", "spr", 1e-3), ("imbalance", "imb", 1e-4)):
            with self.subTest(name):
                self.assertTrue(features[name].valid)
                self.assertAlmostEqual(paper[key], features[name].value, delta=tolerance)

    def test_unchanged_sequence_is_not_reingested(self):
        pipeline = self.pipeline()
        book = self.books("AAVE-USD")
        args = dict(event_us=self.clock.us - 1, received_us=self.clock.us, sequence=book["sequence"])
        self.assertEqual("accepted", pipeline.ingest_book("AAVE-USD", book["bids"], book["asks"], **args))
        self.assertEqual("UNCHANGED_SEQUENCE", pipeline.ingest_book("AAVE-USD", book["bids"], book["asks"], **args))

    def test_production_without_tokenizer_proof_refuses_exactly(self):
        bundle, approvals, artifacts = self.releases.build()
        self.activate(bundle, approvals, artifacts)
        pipeline = self.pipeline(counter=False)
        out = pipeline.decide("AAVE-USD", ProviderLike().judge(type("S", (), {"instrument": "AAVE-USD"})()),
                              close_us=self.clock.us // 60_000_000 * 60_000_000, now_us=self.clock.us, mono_ns=self.mono.ns,
                              gate_open=True, boot_id="rt-boot")
        self.assertEqual(("SNAPSHOT_TOKEN_PROOF_UNAVAILABLE",), out.reasons)

    def test_runtime_entry_reason_is_exactly_the_missing_token_proof(self):
        bundle, approvals, artifacts = self.releases.build()
        self.activate(bundle, approvals, artifacts)
        rt = PaperRuntime(RuntimeConfig(data_dir=self.dir / "rt", instruments=("AAVE-USD",), accounting="ledger",
                                        paper_armed=True, explore_fraction=0.001),
                          book_source=self.books, clock_us=self.clock, reflex=ProviderLike(), rules_source=FakeRules(),
                          authority=self.a, formal=self.pipeline(counter=False))
        self.addCleanup(rt.close)
        result = None
        for _ in range(21):
            self.at(self.clock.us + 60_000_000)
            result = rt.cycle()["instruments"]["AAVE-USD"]
        self.assertEqual(["AUTHORITY_REFUSED:SNAPSHOT_TOKEN_PROOF_UNAVAILABLE"],
                         [x for x in result["reasons"] if x.startswith("AUTHORITY_REFUSED")], result["reasons"])

    def test_paper_verifier_is_never_configured_for_a_live_account(self):
        from bot.autonomous.ledger import Ledger
        live = Ledger.open(self.dir / "live.sqlite3")
        self.addCleanup(live.close)
        live.connection.execute("INSERT INTO accounts VALUES ('live-1','coinbase','live','USD',0,0)")
        with self.assertRaises(ContractViolation):
            configure_paper_verifier(live, PaperBookOutcomeVerifier(live, fee_rate=D("0.006")))


class AttestedEntryTests(FormalCase):
    """200+ real paper labels -> attested calibrator -> operator release naming it -> attested entry."""

    WIN, LOSS = 104.0, 98.0

    def history(self, pipeline, count):
        judgment = replace(GOOD, reflex_id=ProviderLike.reflex_id, formal=formal_long())
        start = (self.clock.us // HOUR + 1) * HOUR + 10 * 60_000_000
        for i in range(count):
            close = start + i * 5 * HOUR
            self.ingest(pipeline, 100.0, close - 1_000_000)  # the close book (exchange time <= close)
            self.at(close + 1_000_000)
            out = pipeline.decide("AAVE-USD", judgment, close_us=close, now_us=self.clock.us, mono_ns=self.mono.ns,
                                  gate_open=True, boot_id="rt-boot")
            self.assertEqual((), out.reasons)
            self.ingest(pipeline, 100.0, close + 2_000_000)  # first executable quote after the forecast
            hour = close // HOUR
            for k in range(1, 5):
                final = self.WIN if i % 10 < 7 else self.LOSS
                self.ingest(pipeline, final if k == 4 else 100.0, (hour + k) * HOUR + 1_000_000)
            self.at((hour + 4) * HOUR + 6_000_000)
            pipeline.resolve_matured_labels(self.clock.us)
        return start

    def midnight_mark(self):
        midnight = (self.clock.us // DAY + 1) * DAY
        self.at(midnight + 500_000)
        self.assertTrue(self.a.boundary_mark(midnight))

    def test_end_to_end_attested_entry(self):
        r1, approvals, artifacts = self.releases.build()
        self.activate(r1, approvals, artifacts)
        pipeline = self.pipeline()
        self.history(pipeline, 205)
        resolved = self.ledger.connection.execute("SELECT COUNT(*) FROM outcome_labels WHERE status='resolved'").fetchone()[0]
        self.assertGreaterEqual(resolved, 200)
        job = pipeline.attestation_job(instrument="AAVE-USD", regime=Regime.TRENDING, model_id=ProviderLike.reflex_id,
                                       now_us=self.clock.us, out_dir=self.dir / "reviews")
        self.assertEqual("ATTESTED_PROPOSAL_ONLY", job["status"], job)  # evidence only: no release names it yet
        self.assertIsNone(self.ledger.attested_calibration(job["artifact_hash"], r1.bundle_hash))
        # The operator packages the calibrator into a child release and activates it (operator-only).
        self.midnight_mark()
        r2, approvals2, artifacts2 = self.releases.build(base64.b64decode(job["calibrator_payload_b64"]), parent=r1.bundle_hash)
        self.assertEqual(job["artifact_hash"], r2.calibrator_hash)
        approved = self.activate(r2, approvals2, artifacts2)
        recorded = pipeline.attestation_job(instrument="AAVE-USD", regime=Regime.TRENDING, model_id=ProviderLike.reflex_id,
                                            now_us=self.clock.us)
        self.assertEqual("ATTESTED_RECORDED", recorded["status"], recorded)
        self.assertIsNotNone(self.ledger.attested_calibration(r2.calibrator_hash, r2.bundle_hash))
        # Arm (operator); at the next close reconcile, decide under the activated release, then the attested entry.
        close = (self.clock.us // 60_000_000 + 1) * 60_000_000
        self.ingest(pipeline, 100.0, close - 1_000_000)
        self.at(close + 500_000)
        book = self.books("AAVE-USD")
        self.a.observe("AAVE-USD", book["bids"], book["asks"], self.clock.us - self.books.lag_us,
                       RuntimeRules.parse(online_rules(), self.clock.us, "AAVE-USD"))
        self.a.mark()
        self.assertIsNone(self.ledger.arm(ACCOUNT, "operator:alice", boot_id="rt-boot", lease_ns=30 * SECOND,
                                          release_hash=r2.bundle_hash))
        self.assertTrue(self.a.mark())
        decision = pipeline.decide("AAVE-USD", replace(GOOD, reflex_id=ProviderLike.reflex_id, formal=formal_long()),
                                   close_us=close, now_us=self.clock.us, mono_ns=self.mono.ns, gate_open=True,
                                   boot_id="rt-boot")
        self.assertEqual((), decision.reasons)
        outcome = self.a.formal_entry("AAVE-USD", decision)
        self.assertTrue(outcome.ok, outcome.reasons)
        self.assertGreater(self.a.view()["positions"]["AAVE-USD"], 0)
        intent = self.ledger.connection.execute("SELECT decision_id, action_slot FROM order_intents").fetchone()
        self.assertEqual((decision.decision_id, "entry"), tuple(intent))
        # The same paper evidence is never accepted for a live account.
        from bot.autonomous.calibration import OutcomeVerificationRequest
        row = self.ledger.connection.execute(
            "SELECT l.label_id, f.forecast_id, f.decision_id, f.candidate_id, f.recorded_at_us, s.candle_close_us, "
            "s.field_hash, d.model_id FROM outcome_labels l JOIN forecasts f USING(forecast_id) JOIN decisions d "
            "USING(decision_id) JOIN snapshots s USING(snapshot_id) WHERE l.status='resolved' LIMIT 1").fetchone()
        request = OutcomeVerificationRequest(row["label_id"], row["forecast_id"], row["decision_id"], row["candidate_id"],
                                             "buy", row["model_id"], r1.schema_hash, row["field_hash"],
                                             paper_execution_policy_hash(D("0.006")), row["candle_close_us"],
                                             row["recorded_at_us"], self.clock.us, "x", "{}")
        verifier = self.ledger._outcome_verifier
        self.assertIsNotNone(verifier.verify(request))
        self.ledger.connection.execute("UPDATE accounts SET mode='live'")
        self.assertIsNone(verifier.verify(request))


if __name__ == "__main__":
    unittest.main()
