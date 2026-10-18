"""Task 16: authority lease renewer and the formal decision bridge (ledger mode). Fake clocks; no network."""
import json
import unittest
from dataclasses import replace
from decimal import Decimal as D
from unittest import mock

from bot.autonomous.contracts import (AdvisoryRisk, CandidateDecision, ChoiceDistribution, DecisionBatch, Direction,
                                      DirectionDistribution, Regime)
from bot.autonomous.runtime import PaperRuntime, RuntimeConfig
from tests.autonomous.test_runtime import GOOD, FakeRules, Fixed
from tests.autonomous.test_runtime_authority import ACCOUNT, RELEASE, LedgerCase

SECOND = 1_000_000_000


class Mono:
    def __init__(self):
        self.ns = 1_000 * SECOND

    def __call__(self):
        return self.ns


class WorkerCase(LedgerCase):
    def setUp(self):
        super().setUp()
        self.mono = Mono()
        self.approve()
        self.a = self.adapter(monotonic_ns=self.mono)

    def arm(self, with_position=False):
        if with_position:
            self.open_entry(self.a)
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET permit='none', lease_boot_id=NULL, lease_deadline_mono_ns=NULL")
        if with_position:
            self.observe(self.a)
        self.a.mark()
        self.assertIsNone(self.ledger.arm(ACCOUNT, "operator:alice", boot_id="rt-boot", lease_ns=30 * SECOND,
                                          release_hash=RELEASE))

    def runtime(self, reflex=None, **config):
        config.setdefault("instruments", ("AAVE-USD",))
        config.setdefault("paper_armed", True)
        rt = PaperRuntime(RuntimeConfig(data_dir=self.dir / "rt", accounting="ledger", **config), book_source=self.books,
                          clock_us=self.clock, reflex=reflex or Fixed(GOOD), rules_source=FakeRules(), authority=self.a)
        self.addCleanup(rt.close)
        return rt

    def sleep(self, seconds):
        self.clock.us += int(seconds * 1_000_000)
        self.mono.ns += int(seconds * SECOND)

    def serve_for(self, rt, seconds):
        end = self.clock.us + seconds * 1_000_000
        rt.serve(lambda: self.clock.us >= end, sleep=self.sleep)

    def control(self):
        return self.ledger.connection.execute("SELECT * FROM account_control").fetchone()


class LeaseRenewerTests(WorkerCase):
    def test_healthy_loop_keeps_entries_armed_through_the_authority_role(self):
        self.arm()
        rt = self.runtime()
        self.serve_for(rt, 240)  # 8 lease lengths
        row = self.control()
        self.assertEqual("entry", row["permit"])
        self.assertGreater(row["lease_deadline_mono_ns"], self.mono.ns)
        renewals = self.ledger.connection.execute("SELECT COUNT(*), MIN(actor) FROM control_events WHERE kind='renew'").fetchone()
        self.assertGreaterEqual(renewals[0], 15)  # every ~10 s
        self.assertEqual("authority:worker", renewals[1])
        status = json.loads((self.dir / "rt" / "status.json").read_text())
        self.assertTrue(status["lease"]["renewed"], status["lease"])

    def test_stalled_cycle_lapses_the_lease_and_exits_still_work(self):
        self.arm(with_position=True)
        rt = self.runtime()
        def stalled():
            raise RuntimeError("cycle wedged")
        rt.cycle = stalled
        self.serve_for(rt, 240)
        self.assertLessEqual(self.control()["lease_deadline_mono_ns"], self.mono.ns)  # lapsed
        self.assertEqual("CYCLE_STALE", rt.lease_status()["blocker"])
        self.observe(self.a)
        entry = self.a.entry("AAVE-USD", D("50"), self.entry_close, decision=self.decision)
        self.assertFalse(entry.ok)
        self.assertEqual("reduce_only", self.control()["permit"])
        exit_ = self.a.exit("AAVE-USD", self.entry_close + 60_000_000)
        self.assertTrue(exit_.ok, exit_.reasons)

    def test_renewer_crash_lapses_the_lease(self):
        self.arm()
        rt = self.runtime()
        with mock.patch.object(PaperRuntime, "_renew_via_control", side_effect=RuntimeError("renewer died")):
            self.serve_for(rt, 120)
        self.assertEqual("reduce_only", self.control()["permit"])  # the cycle's deadman enforcement disarmed entries
        self.assertEqual("RENEWER_ERROR:RuntimeError", rt.lease_status()["blocker"])

    def test_renewal_blockers(self):
        self.arm()
        rt = self.runtime()
        self.serve_for(rt, 70)
        self.assertIsNone(rt.renewal_blocker())
        view = self.ledger.account_view(ACCOUNT)
        with mock.patch.object(type(self.ledger), "account_view", lambda s, a: replace(view, unknown_intent_ids=("i",))):
            self.assertEqual("UNKNOWN_INTENTS", rt.renewal_blocker())
        rt._last_books_ok_us -= 200_000_000
        self.assertEqual("BOOKS_STALE", rt.renewal_blocker())
        rt._last_books_ok_us += 200_000_000
        rt._ledger_ready_us -= 200_000_000
        self.assertEqual("UNRECONCILED", rt.renewal_blocker())
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET permit='none'")
        self.assertEqual("NOT_ARMED", rt.renewal_blocker())


def formal_long(candidate="aave"):
    return CandidateDecision(candidate, Regime.TRENDING, Direction.LONG, 2, AdvisoryRisk.SAFE,
                             DirectionDistribution(0.9, 0.05, 0.05), 0.85, 0.8, 0.8, 0.85, 0.1,
                             ChoiceDistribution(("trending", "mean_reverting", "high_vol", "crisis"), (0.85, 0.05, 0.05, 0.05)),
                             ChoiceDistribution(("0", "1", "2", "3"), (0.05, 0.05, 0.85, 0.05)),
                             ChoiceDistribution(("safe", "near_limit", "reduce"), (0.9, 0.05, 0.05)))


class ProviderLike:
    """A REFLEX that, like JevReflex, returns the provider's complete CandidateDecision with each judgment."""
    reflex_id = "jev:jev-1.14.0:fixture"

    def judge(self, snapshot):
        return replace(GOOD, reflex_id=self.reflex_id, formal=formal_long() if snapshot.instrument == "AAVE-USD" else None)


class DecisionBridgeTests(WorkerCase):
    def test_jev_parse_carries_the_complete_formal_decision_for_candidates_only(self):
        from tests.autonomous.test_jev_reflex import FakeTransport, Mono as JevMono, echo, snapshot
        from bot.autonomous.jev_reflex import APPROVED_SCHEMA_FILE, JevReflex
        mono = JevMono()
        reflex = JevReflex(data_dir=self.dir, api_key="k", transport=FakeTransport(mono, echo()), monotonic_ns=mono)
        (self.dir / APPROVED_SCHEMA_FILE).write_text(json.dumps(reflex.schema))
        out = reflex.judge_batch({i: snapshot(i) for i in ("AAVE-USD", "BTC-USD")})
        formal = out["AAVE-USD"].formal
        self.assertEqual(("aave", Direction.LONG, 2), (formal.candidate_id, formal.direction, formal.setup_quality))
        self.assertAlmostEqual(0.9, formal.direction_probabilities.long)
        self.assertAlmostEqual(0.1, formal.noul_probability)
        self.assertIsNone(out["BTC-USD"].formal)  # BTC-USD is not a research candidate

    def test_ledger_mode_records_formal_provenance_and_refuses_with_precise_reasons(self):
        rt = self.runtime(ProviderLike(), explore_fraction=0.001)
        self.clock.us += 1_000_000
        result = None
        for _ in range(21):
            self.sleep(60)
            result = rt.cycle()["instruments"]["AAVE-USD"]
        self.assertEqual(["AUTHORITY_REFUSED:CALIBRATION_NOT_ATTESTED", "AUTHORITY_REFUSED:SNAPSHOT_TOKEN_PROOF_UNAVAILABLE"],
                         [r for r in result["reasons"] if r.startswith("AUTHORITY_REFUSED")], result["reasons"])
        row = self.ledger.connection.execute(
            "SELECT d.model_id, d.response_json, s.valid, s.invalid_reason, s.lineage_hash, s.release_hash, c.status "
            "FROM decisions d JOIN snapshots s USING(snapshot_id) JOIN candidate_results c USING(decision_id) "
            "WHERE d.reason='runtime-bridge' ORDER BY d.recorded_at_us DESC LIMIT 1").fetchone()
        self.assertEqual("jev:jev-1.14.0:fixture", row["model_id"])
        self.assertEqual((formal_long(),), DecisionBatch.from_json(row["response_json"]).decisions)
        self.assertEqual((0, "hold", RELEASE), (row["valid"], row["status"], row["release_hash"]))
        self.assertIn("TOKENIZER_PROOF_UNAVAILABLE", row["invalid_reason"])
        runtime_hashes = {r[0] for r in rt.db.execute("SELECT snapshot_hash FROM snapshots")}
        self.assertIn(row["lineage_hash"], runtime_hashes)  # the runtime's real snapshot identity
        self.assertEqual(0, self.ledger.connection.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0])

    def test_offline_and_non_candidate_judgments_are_never_converted(self):
        rt = self.runtime(Fixed(GOOD), explore_fraction=0.001)
        self.clock.us += 1_000_000
        for _ in range(21):
            self.sleep(60)
            result = rt.cycle()["instruments"]["AAVE-USD"]
        self.assertIn("AUTHORITY_REFUSED:FORMAL_JUDGMENT_UNAVAILABLE", result["reasons"])
        self.assertEqual(0, self.ledger.connection.execute("SELECT COUNT(*) FROM decisions WHERE reason='runtime-bridge'").fetchone()[0])
        snap = rt.snapshot("AAVE-USD", {"bids": [[99.99, 1]], "asks": [[100.01, 1]]}, self.clock.us - 500_000,
                           self.clock.us - 100_000, self.clock.us, rt.mark({}, self.clock.us))
        self.assertEqual((None, ("NOT_A_CANDIDATE",)),
                         self.a.record_decision("BTC-USD", replace(snap, instrument="BTC-USD"),
                                                ProviderLike().judge(snap), gate_open=True))


class ReviewFixTests(WorkerCase):
    def test_stale_books_let_the_lease_lapse(self):
        self.arm()
        self.books.lag_us = 600_000_000  # every fetched book is ten minutes old
        rt = self.runtime()
        self.serve_for(rt, 240)
        self.assertLessEqual(self.control()["lease_deadline_mono_ns"], self.mono.ns)
        self.assertIn(rt.lease_status()["blocker"], ("BOOKS_STALE", "CYCLE_STALE"))

    def test_latches_and_kill_file_block_renewal(self):
        self.arm()
        rt = self.runtime()
        self.serve_for(rt, 70)
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET crisis_halted=1")
        self.assertEqual("LATCHED", rt.renewal_blocker())
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET crisis_halted=0")
        (self.dir / "rt" / "KILL").write_text("operator")
        self.assertEqual("KILL_SWITCH", rt.renewal_blocker())

    def bridge_snapshot(self, rt):
        self.observe(self.a)
        return rt.snapshot("AAVE-USD", {"bids": [[99.99, 1]], "asks": [[100.01, 1]]}, self.clock.us - 500_000,
                           self.clock.us - 100_000, self.clock.us, rt.mark({}, self.clock.us))

    def test_bridge_is_idempotent_uses_engine_definitions_and_never_verifies(self):
        from bot.autonomous.contracts import NumericSnapshot, CausalCutoffs, AccountRevision, Feature, default_field_dictionary
        rt = self.runtime(ProviderLike())
        snap = self.bridge_snapshot(rt)
        call = {"started_mono_ns": 5, "received_mono_ns": 9, "deadline_mono_ns": 20, "response_hash": "a" * 64}
        first = self.a.record_decision("AAVE-USD", snap, ProviderLike().judge(snap), gate_open=True, reflex_call=call)
        self.clock.us += 1_000
        again = self.a.record_decision("AAVE-USD", snap, ProviderLike().judge(snap), gate_open=True, reflex_call=call)
        self.assertEqual(first, again)
        row = self.ledger.connection.execute("SELECT d.response_json, s.* FROM decisions d JOIN snapshots s USING(snapshot_id) "
                                             "WHERE d.decision_id=?", (first[0],)).fetchone()
        batch = DecisionBatch.from_json(row["response_json"])
        self.assertEqual((20, 9, snap.cutoff_us), (batch.deadline_mono_ns, batch.received_mono_ns, batch.received_at_us))
        names = [f.name for f in default_field_dictionary().candidate_fields]
        wire = json.loads(row["state_json"])[1][0]
        mask = wire[-1]
        valid = {names[i] for i in range(len(names)) if mask >> i & 1}
        self.assertTrue({"rv_60m", "rv_samples", "return_1h"}.isdisjoint(valid))
        # A cloned VALID row with the same state and lineage never verifies a bridge decision.
        clone = dict(row)
        self.ledger.connection.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("clone", clone["account_id"], clone["release_hash"], clone["candle_close_us"], clone["knowledge_cutoff_us"],
             clone["field_hash"], clone["state_json"], "c" * 64, 10, clone["lineage_hash"], clone["account_revision"], 1,
             None, clone["recorded_at_us"] + 1))
        fake = NumericSnapshot("aave", "AAVE-USD", "e" * 64, CausalCutoffs(clone["candle_close_us"], clone["knowledge_cutoff_us"], 0, "b"),
                               tuple(Feature(n, 0.0, False) for n in names), 0, clone["lineage_hash"], AccountRevision(),
                               snapshot_id="clone", account_id=clone["account_id"], release_hash=clone["release_hash"])
        self.assertIsNone(self.ledger.verified_decision_snapshot(batch, fake))


if __name__ == "__main__":
    unittest.main()
