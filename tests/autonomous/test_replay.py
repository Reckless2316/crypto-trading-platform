"""Task 19: causal replay through the paper runtime and pre-registered promotion criteria.

All sources are generated locally by the real PaperRuntime with fake books and a fake clock.
"""
import hashlib
import json
import math
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
from pathlib import Path

from bot.autonomous import replay as r
from bot.autonomous.runtime import PaperRuntime, RuntimeConfig

T0 = 1_792_000_020_000_000 - (1_792_000_020_000_000 % 60_000_000)  # a minute boundary
STEP = 60_000_000
OVERRIDES = dict(horizon_candles=5, lookback_candles=20)


def rules(instrument):
    return r._replay_rules(instrument)


def record(directory: Path, mids, *, instrument="BTC-USD", spread=0.0001, imbalance=0.1, start=T0):
    """Run the real PaperRuntime once over synthetic books, producing a recorded runtime.sqlite3."""
    clock = {"now": start}
    book = {}
    runtime = PaperRuntime(RuntimeConfig(instruments=(instrument,), data_dir=directory, **OVERRIDES),
                           book_source=lambda i: book[i], clock_us=lambda: clock["now"], rules_source=rules)
    try:
        for k, mid in enumerate(mids):
            event = start + k * STEP
            clock["now"] = event + 2_000_000
            half = mid * spread / 2
            book[instrument] = r.recorded_book(mid - half, mid + half, imbalance, event)
            runtime.cycle()
    finally:
        runtime.close()


def trending(n, *, drift=0.0004, wobble=0.00005):
    return [100.0 * math.exp(drift * k + wobble * math.sin(k)) for k in range(n)]


def choppy(n):
    return [100.0 * (1 + 0.003 * math.sin(k * 1.7) + 0.002 * math.cos(k * 0.9)) for k in range(n)]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self, name, mids, **kw):
        directory = self.root / name
        record(directory, mids, **kw)
        return directory

    def replay(self, directory, **kw):
        return r.AutonomousReplay(directory, config_overrides=OVERRIDES, **kw).run()

    def runner(self, **kw):
        return r.EvaluationRunner(r.Criteria(bootstrap=kw.pop("bootstrap", 200), **kw))

    def register(self, name="register.json"):
        return r.HoldoutRegister(self.root / name)


class ReplayPathTests(Fixture):
    def test_holdout_floor(self):
        self.assertGreaterEqual(r.minimum_holdout_blocks(), 785)
        self.assertGreater(r.minimum_holdout_blocks(delta=.025), r.minimum_holdout_blocks())
        with self.assertRaises(ValueError):
            r.minimum_holdout_blocks(delta=float("nan"))
        self.assertGreaterEqual(r.Criteria().min_holdout_blocks, 800)

    def test_replay_reproduces_recorded_decisions_and_labels_through_runtime(self):
        directory = self.source("trend", trending(80))
        result = self.replay(directory)
        self.assertTrue(result.fidelity["exact"], result.fidelity)
        self.assertGreater(result.fidelity["compared"], 40)
        self.assertGreater(result.fidelity["labels_compared"], 30)
        self.assertEqual(80, result.cycles)
        self.assertEqual(0, result.decision_errors)
        self.assertTrue(all(s.reflex_id == "offline-reflex-v1" for s in result.samples))

    def test_source_database_is_never_written(self):
        directory = self.source("trend", trending(40))
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()}
        self.replay(directory)
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()}
        self.assertEqual(before["runtime.sqlite3"], after["runtime.sqlite3"])

    def test_future_perturbation_cannot_change_earlier_decisions(self):
        base = trending(70)
        perturbed = base[:50] + [m * 0.9 for m in base[50:]]
        a = self.replay(self.source("a", base))
        b = self.replay(self.source("b", perturbed))
        cut = T0 + 50 * STEP
        early = lambda res: [s for s in res.samples if s.close_us + res.horizon_us < cut]
        self.assertTrue(early(a))
        self.assertEqual(early(a), early(b))
        self.assertNotEqual(a.samples, b.samples)

    def test_as_of_cutoff_ignores_later_rows(self):
        directory = self.source("trend", trending(70))
        cut = T0 + 40 * STEP
        limited = self.replay(directory, as_of_us=cut)
        self.assertEqual(40, limited.cycles)
        self.assertTrue(all(close < cut for close in limited.snapshot_closes))


def sample(close_us, *, hit=1, confidence=0.9, instrument="BTC-USD", regime="trending", gate=True,
           direction="long", ret=0.01):
    return r.Sample(f"d-{instrument}-{close_us}", instrument, close_us, "offline-reflex-v1", regime, direction,
                    confidence, hit, gate, ret)


class SplitAndBlockTests(unittest.TestCase):
    def test_purge_and_embargo_remove_overlap(self):
        criteria = r.Criteria(embargo_us=0, bootstrap=100)
        closes = [T0 + k * STEP for k in range(1000)]
        horizon = 30 * STEP
        plan = r.seal_splits(closes, horizon_us=horizon, criteria=criteria, fingerprint="f")
        self.assertEqual(horizon, plan.embargo_us)  # never shorter than the label horizon
        splits, dropped = r.assign_splits([sample(c) for c in closes], plan)
        for earlier, later in (("train", "validation"), ("validation", "holdout")):
            start = plan.window(later)[0]
            self.assertTrue(all(s.close_us + horizon < start for s in splits[earlier]))
            self.assertTrue(all(s.close_us >= start + plan.embargo_us for s in splits[later]))
            self.assertGreater(dropped[earlier]["purged"], 0)
            self.assertGreater(dropped[later]["embargoed"], 0)
        self.assertEqual(0, dropped["holdout"]["purged"])

    def test_seal_depends_on_times_not_outcomes(self):
        criteria = r.Criteria(bootstrap=100)
        closes = [T0 + k * STEP for k in range(100)]
        a = r.seal_splits(closes, horizon_us=STEP, criteria=criteria, fingerprint="x")
        self.assertEqual(a, r.seal_splits(closes, horizon_us=STEP, criteria=criteria, fingerprint="x"))
        self.assertNotEqual(a.seal, r.seal_splits(closes, horizon_us=STEP, criteria=replace(criteria, alpha=.01),
                                                  fingerprint="x").seal)

    def test_independent_blocks_not_rows(self):
        rows = [sample(T0 + k * STEP, instrument=i) for k in range(200) for i in ("BTC-USD", "ETH-USD")]
        stats = r.block_stats(rows, 4 * 3_600_000_000)
        self.assertEqual(400, stats["rows"])
        self.assertLessEqual(stats["blocks"], 2)
        self.assertLessEqual(stats["effective_blocks"], stats["blocks"])

    def test_autocorrelated_blocks_shrink_the_effective_count(self):
        block = 3_600_000_000
        rows = [sample(k * block, hit=int((k // 10) % 2)) for k in range(100)]
        stats = r.block_stats(rows, block)
        self.assertEqual(100, stats["blocks"])
        self.assertGreater(stats["lag1_autocorrelation"], 0.5)
        self.assertLess(stats["effective_blocks"], 30)


class StatisticsTests(unittest.TestCase):
    def test_wilson_interval_and_cohort_brier(self):
        lo, hi = r.wilson_interval(8, 10)
        self.assertAlmostEqual(0.4902, lo, places=3)
        self.assertAlmostEqual(0.9433, hi, places=3)
        self.assertIsNone(r.wilson_interval(0, 0))
        rows = [sample(T0 + k, hit=k % 2, confidence=0.7) for k in range(10)] + \
               [sample(T0 + k, instrument="ETH-USD", regime="high_vol", hit=1, confidence=0.6) for k in range(4)]
        cohorts = r.cohort_calibration(rows)
        self.assertEqual([("BTC-USD", "trending"), ("ETH-USD", "high_vol")],
                         [(c["instrument"], c["regime"]) for c in cohorts])
        self.assertAlmostEqual((5 * 0.09 + 5 * 0.49) / 10, cohorts[0]["brier"])
        self.assertAlmostEqual(0.16, cohorts[1]["brier"])

    def test_bootstrap_is_deterministic_by_seed(self):
        values = [(k // 3, math.sin(k)) for k in range(60)]
        a = r.block_bootstrap(values, replications=500, seed=7)
        self.assertEqual(a, r.block_bootstrap(values, replications=500, seed=7))
        self.assertNotEqual(a, r.block_bootstrap(values, replications=500, seed=8))
        self.assertEqual([], r.block_bootstrap([(1, 0.5), (1, 0.4)], replications=500, seed=7))

    def test_holm_step_down(self):
        self.assertEqual({"a": True, "b": True}, r.holm({"a": 0.01, "b": 0.04}, 0.05))
        self.assertEqual({"a": True, "b": False}, r.holm({"a": 0.01, "b": 0.06}, 0.05))
        self.assertEqual({"a": False, "b": False}, r.holm({"a": 0.03, "b": 0.03}, 0.05))
        self.assertEqual({"a": False, "b": False}, r.holm({"a": None, "b": 0.0}, 0.05) | {"b": False})

    def test_fees_spread_and_latency_matter_and_baseline_matches_runtime_label(self):
        prices = {("BTC-USD", T0 + k * STEP): (100.0 + k - 0.05, 100.0 + k + 0.05) for k in range(20)}
        s = sample(T0)
        kw = dict(prices=prices, fee_rate=0.006, step_us=STEP, horizon_us=5 * STEP)
        base = r.stressed_return(s, stress=r.Stress("b"), **kw)
        self.assertAlmostEqual(prices[("BTC-USD", T0 + 5 * STEP)][0] / prices[("BTC-USD", T0)][1] - 1 - 0.012, base)
        self.assertLess(r.stressed_return(s, stress=r.Stress("f", fee_multiplier=1.5), **kw), base)
        self.assertLess(r.stressed_return(s, stress=r.Stress("s", spread_multiplier=2), **kw), base)
        self.assertNotEqual(r.stressed_return(s, stress=r.Stress("l", latency_candles=1), **kw), base)
        self.assertIsNone(r.stressed_return(sample(T0 + 18 * STEP), stress=r.Stress("b"), **kw))

    def test_day_concentration(self):
        day = 86_400_000_000
        self.assertAlmostEqual(0.5, r.day_concentration([(0, 1.0), (day, 1.0)]))
        self.assertAlmostEqual(0.25, r.day_concentration([(k * day, 1.0) for k in range(4)]))
        self.assertIsNone(r.day_concentration([]))

    def test_criteria_cannot_be_loosened_below_floors(self):
        with self.assertRaises(ValueError):
            r.Criteria(min_holdout_blocks=10)
        with self.assertRaises(ValueError):
            r.Criteria(fee_multiplier=0.5)


class EvaluationTests(Fixture):
    def test_toy_success_remains_promotion_ineligible(self):
        replay = self.replay(self.source("trend", trending(200, drift=0.001)))
        report = self.runner().evaluate(replay, register=self.register())
        self.assertFalse(report["promotion_eligible"])
        self.assertIn("holdout_effective_blocks", report["ineligible_reasons"])
        self.assertTrue(report["criteria_results"]["replay_fidelity_exact"]["pass"])
        json.dumps(report)  # serializable

    def test_same_manifest_and_seed_give_the_same_report(self):
        replay = self.replay(self.source("chop", choppy(120)))
        a = self.runner().evaluate(replay, register=self.register("a.json"))
        b = self.runner().evaluate(replay, register=self.register("b.json"))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        again = self.replay(self.root / "chop")
        c = self.runner().evaluate(again, register=self.register("c.json"))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(c, sort_keys=True))

    def test_holdout_reuse_rejected_and_relook_never_promotes(self):
        replay = self.replay(self.source("chop", choppy(120)))
        register = self.register()
        first = self.runner().evaluate(replay, register=register)
        self.assertTrue(first["criteria_results"]["holdout_unseen"]["pass"])
        with self.assertRaises(r.HoldoutReused):
            self.runner().evaluate(replay, register=register)
        relook = self.runner().evaluate(replay, register=register, relook="operator re-check 2026-10-18")
        self.assertFalse(relook["criteria_results"]["holdout_unseen"]["pass"])
        self.assertFalse(relook["promotion_eligible"])
        self.assertEqual([first["holdout"]["evaluation_hash"]], relook["holdout"]["prior_looks"])
        self.assertEqual(2, len(register.entries()))

    def test_new_data_after_a_seen_holdout_is_a_fresh_holdout(self):
        directory = self.source("chop", choppy(150))
        register = self.register()
        self.runner().evaluate(self.replay(directory, as_of_us=T0 + 90 * STEP), register=register)
        fresh = self.runner().evaluate(self.replay(directory), register=register)
        self.assertTrue(fresh["criteria_results"]["holdout_unseen"]["pass"])

    def test_overlapping_later_holdout_is_also_a_reuse(self):
        directory = self.source("chop", choppy(150))
        register = self.register()
        self.runner().evaluate(self.replay(directory, as_of_us=T0 + 140 * STEP), register=register)
        with self.assertRaises(r.HoldoutReused):
            self.runner().evaluate(self.replay(directory), register=register)

    def synthetic(self, *, blocks, edge=0.004, confidence_good=True, concentrate=False):
        """A direct ReplayResult (bypassing the runtime) to prove every criterion can pass."""
        block = 4 * 3_600_000_000
        day = 86_400_000_000
        samples, prices = [], {}
        for k in range(blocks):
            close = T0 + k * block
            hit = int((k * 7919) % 10 < 8)
            confidence = (0.85 if hit else 0.3) if confidence_good else 0.5
            samples.append(r.Sample(f"d{k}", "BTC-USD", close, "offline-reflex-v1", "trending", "long",
                                    confidence, hit, True, None))
            boost = 50 if concentrate and close // day == T0 // day else 1
            for j in range(0, 3):
                prices[("BTC-USD", close + j * STEP)] = (100.0, 100.01)
            exit_bid = 100.01 * (1 + 0.006 * 2 * 1.5 + edge * boost) / 1  # beats stressed fees
            prices[("BTC-USD", close + 5 * STEP)] = (exit_bid * (1 + 0.0002), exit_bid * (1 + 0.0003))
            prices[("BTC-USD", close + 6 * STEP)] = (exit_bid * (1 + 0.0002), exit_bid * (1 + 0.0003))
        closes = tuple(sorted({c for _, c in prices}))
        return r.ReplayResult("synthetic", "fp", closes[-1], STEP, 5 * STEP, 0.006, len(closes), 0, tuple(samples),
                              prices, closes, {"exact": True, "mismatched": 0})

    def test_every_criterion_can_pass_with_enough_independent_evidence(self):
        criteria = dict(min_train_blocks=200, min_validation_blocks=200, min_holdout_blocks=200, embargo_us=0)
        report = self.runner(**criteria).evaluate(self.synthetic(blocks=2500), register=self.register())
        self.assertTrue(report["promotion_eligible"], report["ineligible_reasons"])

    def test_concentrated_or_uncalibrated_evidence_fails(self):
        criteria = dict(min_train_blocks=200, min_validation_blocks=200, min_holdout_blocks=200, embargo_us=0)
        flat = self.runner(**criteria).evaluate(self.synthetic(blocks=2500, confidence_good=False),
                                                register=self.register("a.json"))
        self.assertIn("brier_beats_base_rate", flat["ineligible_reasons"])
        losing = self.runner(**criteria).evaluate(self.synthetic(blocks=2500, edge=-0.02),
                                                  register=self.register("b.json"))
        self.assertIn("stressed_net_lower_bound_positive", losing["ineligible_reasons"])

    def test_diagnostic_mode_never_looks_at_the_holdout(self):
        replay = self.replay(self.source("chop", choppy(120)))
        report = self.runner().diagnose(replay)
        self.assertFalse(report["promotion_eligible"])
        self.assertEqual(["holdout_not_evaluated"], report["ineligible_reasons"])
        self.assertIsNone(report["holdout"])
        self.assertFalse((self.root / "register.json").exists())
        first = self.runner().evaluate(replay, register=self.register())  # the holdout is still unseen
        self.assertTrue(first["criteria_results"]["holdout_unseen"]["pass"])

    def test_cli_writes_report_reads_source_read_only_and_refuses_reuse(self):
        directory = self.source("chop", choppy(90))
        copy = self.root / "copy"
        shutil.copytree(directory, copy)
        digest = hashlib.sha256((copy / "runtime.sqlite3").read_bytes()).hexdigest()
        out, register = self.root / "report.json", self.root / "reg.json"
        args = ["--data-dir", str(copy), "--out", str(out), "--register", str(register), "--bootstrap", "200"]
        self.assertEqual(0, r.main(args))
        report = json.loads(out.read_text())
        self.assertFalse(report["promotion_eligible"])
        self.assertEqual(digest, hashlib.sha256((copy / "runtime.sqlite3").read_bytes()).hexdigest())
        with redirect_stderr(StringIO()):
            self.assertEqual(2, r.main(args))
        self.assertEqual(0, r.main(args + ["--relook", "explicit"]))


if __name__ == "__main__":
    unittest.main()
