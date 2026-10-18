"""Paper runtime: causal snapshot, code gate, hard risk, adverse fills, labels, review. No network."""
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

from bot.autonomous.contracts import AdvisoryRisk, Direction, Regime
from bot.autonomous.runtime import (
    _day,
    Judgment, OfflineReflex, PaperRuntime, RuntimeConfig, Snapshot, Unavailable, main, nightly_review, open_db,
    paper_fill,
)

START_US = 1_760_000_000 * 1_000_000 // 60_000_000 * 60_000_000


def stamp(us):
    return datetime.fromtimestamp(us // 1_000_000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".%06dZ" % (us % 1_000_000)


class Clock:
    def __init__(self, us):
        self.us = us

    def __call__(self):
        return self.us


class FakeBooks:
    def __init__(self, clock, mids, spread=0.0001, lag_us=500_000):
        self.clock, self.mids, self.spread, self.lag_us, self.calls = clock, mids, spread, lag_us, 0

    def __call__(self, product):
        self.calls += 1
        mid = self.mids[product] if not callable(self.mids[product]) else self.mids[product]()
        half = mid * self.spread / 2
        return {"exchange_time": stamp(self.clock.us - self.lag_us), "sequence": self.calls,
                "bids": [[mid - half, 50.0], [mid - 2 * half, 50.0]], "asks": [[mid + half, 50.0], [mid + 2 * half, 50.0]]}


def online_rules(**changes):
    rules = {"id": "AAVE-USD", "base_increment": "0.00000001", "quote_increment": "0.01", "min_market_funds": "1",
             "status": "online", "trading_disabled": False, "post_only": False, "limit_only": False, "cancel_only": False}
    rules.update(changes)
    return rules


class FakeRules:
    def __init__(self, **changes):
        self.changes, self.calls, self.fail = changes, 0, False

    def __call__(self, product):
        self.calls += 1
        if self.fail:
            raise ConnectionError("rules outage")
        return dict(online_rules(**self.changes), id=product)


class Fixed:
    reflex_id = "fixed-test"

    def __init__(self, judgment):
        self.judgment = judgment

    def judge(self, snapshot):
        return self.judgment


GOOD = Judgment(Regime.TRENDING, Direction.LONG, 0.85, False, 2, AdvisoryRisk.SAFE, "fixed-test")


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.clock = Clock(START_US + 1_000_000)
        self.price = {"v": 100.0}

    def runtime(self, judgment=None, rules=None, **config):
        config.setdefault("instruments", ("AAVE-USD",))
        config.setdefault("paper_armed", True)
        books = FakeBooks(self.clock, {"AAVE-USD": lambda: self.price["v"]})
        self.rules = rules or FakeRules()
        rt = PaperRuntime(RuntimeConfig(data_dir=self.dir, **config), book_source=books, clock_us=self.clock,
                          reflex=Fixed(judgment) if judgment else None, rules_source=self.rules)
        self.addCleanup(rt.close)
        return rt

    def advance(self, rt, n, drift=0.0):
        out = None
        for _ in range(n):
            self.clock.us += 60_000_000
            self.price["v"] *= 1 + drift
            out = rt.cycle()
        return out



class RuntimeTests(RuntimeCase):
    def test_future_and_stale_observations_are_rejected(self):
        rt = self.runtime()
        account = rt.mark({}, self.clock.us)
        book = {"bids": [[99.9, 1]], "asks": [[100.1, 1]]}
        with self.assertRaisesRegex(Unavailable, "FUTURE_OBSERVATION"):
            rt.snapshot("AAVE-USD", book, self.clock.us + 1, self.clock.us, self.clock.us, account)
        with self.assertRaisesRegex(Unavailable, "FUTURE_OBSERVATION"):
            rt.snapshot("AAVE-USD", book, self.clock.us, self.clock.us + 1, self.clock.us, account)
        with self.assertRaisesRegex(Unavailable, "STALE_BOOK"):
            rt.snapshot("AAVE-USD", book, self.clock.us - 10_000_001, self.clock.us, self.clock.us, account)

    def test_history_never_reads_rows_received_after_cutoff(self):
        rt = self.runtime()
        self.advance(rt, 3)
        rt.db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      ("AAVE-USD", self.clock.us - 30_000_000, 0, self.clock.us + 10**9, 0, 1e9, 0, 0, "{}", 1, "x"))
        snap = rt.snapshot("AAVE-USD", {"bids": [[99.99, 1]], "asks": [[100.01, 1]]}, self.clock.us + 59_000_000,
                           self.clock.us + 59_500_000, self.clock.us + 60_000_000, rt.mark({}, self.clock.us))
        self.assertEqual(3, snap.history)
        self.assertLess(abs(snap.momentum_bps), 5)

    def test_snapshot_is_compact_under_token_budget(self):
        rt = self.runtime()
        status = self.advance(rt, 25)
        snap = status["instruments"]["AAVE-USD"]
        self.assertLess(snap["token_estimate"], 400)
        self.assertLess(snap["token_estimate"], 100)
        with self.assertRaises(ValueError):
            from bot.autonomous.state import require_token_budget
            require_token_budget(400)

    def test_warmup_holds_without_judgment(self):
        rt = self.runtime(GOOD)
        status = self.advance(rt, 5)
        self.assertEqual("warmup", status["instruments"]["AAVE-USD"]["status"])
        self.assertEqual(0, rt.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0])

    def test_gate_boundary_point_eight_refuses(self):
        rt = self.runtime(replace(GOOD, direction_confidence=0.80))
        result = self.advance(rt, 22)["instruments"]["AAVE-USD"]
        self.assertFalse(result["gate_open"])
        self.assertIn("ENTRY_GATE_REFUSED", result["reasons"])
        for change in (dict(setup_quality=1), dict(risk_state=AdvisoryRisk.NEAR_LIMIT), dict(toxic_flow=True),
                       dict(direction=Direction.SHORT)):
            with self.subTest(change):
                rt.reflex = Fixed(replace(GOOD, **change))
                self.assertIn("ENTRY_GATE_REFUSED", self.advance(rt, 1)["instruments"]["AAVE-USD"]["reasons"])

    def test_open_gate_without_calibration_sizes_zero(self):
        rt = self.runtime(GOOD)
        result = self.advance(rt, 22)["instruments"]["AAVE-USD"]
        self.assertTrue(result["gate_open"])
        self.assertEqual("hold", result["status"])
        self.assertIn("CALIBRATION_INSUFFICIENT", result["reasons"])
        self.assertEqual(0, rt.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0])

    def test_escalation_below_point_six_and_crisis(self):
        rt = self.runtime(replace(GOOD, direction_confidence=0.5999))
        self.assertIn("LOW_CONFIDENCE", self.advance(rt, 22)["instruments"]["AAVE-USD"]["reasons"])
        rt.reflex = Fixed(replace(GOOD, direction_confidence=0.60))
        self.assertNotIn("LOW_CONFIDENCE", self.advance(rt, 1)["instruments"]["AAVE-USD"]["reasons"])
        rt.reflex = Fixed(replace(GOOD, regime=Regime.CRISIS))
        status = self.advance(rt, 1)
        self.assertIn("BRAIN_CRISIS", status["instruments"]["AAVE-USD"]["reasons"])
        self.assertIn("CRISIS_HALT", status["latches"])
        codes = {r["code"] for r in rt.db.execute("SELECT code FROM escalations")}
        self.assertEqual({"LOW_CONFIDENCE", "BRAIN_CRISIS"}, codes)
        # Crisis latch blocks later entries even when the reflex recovers, until an operator clears it.
        rt.reflex = Fixed(GOOD)
        rt.config = replace(rt.config, explore_fraction=0.001)
        self.assertIn("CRISIS_HALT", self.advance(rt, 1)["instruments"]["AAVE-USD"]["reasons"])
        rt.clear_latch("CRISIS_HALT")
        self.assertEqual("buy", self.advance(rt, 1)["instruments"]["AAVE-USD"]["status"])

    def test_exploration_entry_fills_and_kill_file_blocks(self):
        rt = self.runtime(GOOD, explore_fraction=0.001)
        result = self.advance(rt, 21)["instruments"]["AAVE-USD"]
        self.assertEqual("buy", result["status"], result["reasons"])
        fill = rt.db.execute("SELECT * FROM fills").fetchone()
        self.assertEqual(1, fill["exploration"])
        self.assertLessEqual(D(fill["notional_usd"]) + D(fill["fee_usd"]), D("10.06"))  # 0.1% NAV notional plus fee bound
        self.assertIn("ENTRY_SLOT_OCCUPIED", self.advance(rt, 1)["instruments"]["AAVE-USD"]["reasons"])
        (self.dir / "KILL").write_text("operator")
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
        self.assertIn("KILL_SWITCH", result["reasons"])
        rt.reflex = Fixed(replace(GOOD, risk_state=AdvisoryRisk.REDUCE))  # even an exit is refused while killed
        self.assertIn("KILL_SWITCH", self.advance(rt, 1)["instruments"]["AAVE-USD"]["reasons"])
        self.assertEqual(1, rt.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0])

    def test_code_exits_on_reduce_and_time_stop(self):
        rt = self.runtime(GOOD, explore_fraction=0.001, horizon_candles=3)
        self.assertEqual("buy", self.advance(rt, 21)["instruments"]["AAVE-USD"]["status"])
        self.advance(rt, 2)
        self.assertEqual("sell", self.advance(rt, 1)["instruments"]["AAVE-USD"]["status"])  # time stop at horizon
        self.assertEqual({}, rt.positions())
        self.assertLess(rt.cash(), D(10000))  # round trip paid both fees
        self.assertEqual("buy", self.advance(rt, 1)["instruments"]["AAVE-USD"]["status"])
        rt.reflex = Fixed(replace(GOOD, risk_state=AdvisoryRisk.REDUCE))
        self.assertEqual("sell", self.advance(rt, 1)["instruments"]["AAVE-USD"]["status"])

    def test_unarmed_paper_never_fills(self):
        rt = self.runtime(GOOD, explore_fraction=0.001, paper_armed=False)
        self.assertIn("PAPER_NOT_ARMED", self.advance(rt, 22)["instruments"]["AAVE-USD"]["reasons"])

    def test_drawdown_and_daily_latches_block_buys(self):
        rt = self.runtime(GOOD, explore_fraction=0.001)
        account = {"drawdown": 0.15, "daily_loss": 0.0, "positions": {}, "equity": D(10000), "cash": D(10000), "bid": {}}
        self.assertIn("DRAWDOWN_LATCH", rt.risk_check("buy", "AAVE-USD", D(1), account))
        self.assertNotIn("DRAWDOWN_LATCH", rt.risk_check("sell", "AAVE-USD", D(0), account))
        daily = dict(account, drawdown=0.0, daily_loss=0.02)
        self.assertIn("DAILY_HALT", rt.risk_check("buy", "AAVE-USD", D(1), daily))
        self.assertEqual((), rt.risk_check("buy", "AAVE-USD", D(1), dict(account, drawdown=0.0)))
        cap = dict(account, drawdown=0.0)
        self.assertIn("MAX_POSITION", rt.risk_check("buy", "AAVE-USD", D("500.01"), cap))
        # Latches persist through the append-only log after a real loss is marked.
        rt.db.execute("INSERT INTO fills VALUES ('f','d','AAVE-USD','buy','30','100','3000','0',0,0)")
        rt.mark({"AAVE-USD": 40.0}, self.clock.us)
        self.assertIn("DRAWDOWN_LATCH", rt.latched())
        self.assertTrue(any(k.startswith("DAILY_HALT:") for k in rt.latched()))

    def test_hard_ceilings_cannot_be_loosened(self):
        for change in (dict(max_drawdown=0.2), dict(max_daily_loss=0.05), dict(max_position_fraction=0.1),
                       dict(explore_fraction=0.01)):
            with self.subTest(change), self.assertRaises(ValueError):
                RuntimeConfig(data_dir=self.dir, **change)

    def test_adverse_fill_rounds_quantity_down_and_fee_up(self):
        asks = [(100.0, 0.3), (100.05, 10.0), (101.0, 10.0)]
        qty, avg, notional, fee = paper_fill(asks, "buy", D("50.30"), None, D("100.10"), D("0.006"))
        self.assertTrue(D("0.49") < qty < D("0.5"))  # 0.3 at 100, remainder at 100.05, never 101
        self.assertLessEqual(notional + fee, D("50.30"))
        self.assertEqual(fee, (notional * D("0.006")).quantize(D("0.000001"), rounding="ROUND_CEILING"))
        self.assertGreater(avg, D("100"))
        self.assertEqual(qty, qty.quantize(D("0.00000001")))
        self.assertIsNone(paper_fill(asks, "buy", D("50"), None, D("99.99"), D("0.006")))
        sold = paper_fill([(99.0, 1.0), (98.0, 5.0)], "sell", None, D("2"), D("98.5"), D("0.006"))
        self.assertEqual(D("1.0"), sold[0])

    def test_labels_and_brier(self):
        rt = self.runtime(GOOD, horizon_candles=5)
        self.advance(rt, 22, drift=0.004)
        status = self.advance(rt, 10, drift=0.004)
        labels = rt.db.execute("SELECT * FROM labels").fetchall()
        self.assertTrue(labels)
        self.assertTrue(all(l["hit"] == 1 for l in labels))  # ~+2% over 5 candles beats the 1.2% round-trip fee
        rt.reflex = Fixed(replace(GOOD, direction_confidence=0.9))
        self.advance(rt, 7, drift=0.0)  # flat: long decisions lose to costs
        self.assertTrue(rt.db.execute("SELECT COUNT(*) FROM labels WHERE hit=0").fetchone()[0])
        self.assertAlmostEqual(status["brier"], (1 - 0.85) ** 2, places=6)

    def test_append_only_tables_refuse_update(self):
        rt = self.runtime()
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            rt.db.execute("DELETE FROM equity_marks")

    def test_status_file_and_nightly_review(self):
        rt = self.runtime(GOOD, horizon_candles=5, explore_fraction=0.001)
        self.advance(rt, 30, drift=0.002)
        status = json.loads((self.dir / "status.json").read_text())
        self.assertEqual("paper", status["mode"])
        self.assertFalse(status["live"])
        self.assertFalse(status["reflex_calibrated"])
        self.assertIn("AAVE-USD", status["instruments"])
        report = nightly_review(self.dir / "runtime.sqlite3", self.dir / "reviews", now_us=self.clock.us, day=_day(self.clock.us))
        self.assertFalse(report["applied"])
        self.assertGreater(report["metrics"]["fills"], 0)
        self.assertIsNotNone(report["metrics"]["brier"])
        self.assertTrue(list((self.dir / "reviews").glob("schema-proposal-*.md")))

    def test_offline_reflex_is_typed_and_flags_crisis(self):
        base = Snapshot("AAVE-USD", 0, 0, 0, 0, 100.0, 2.0, 0.1, 5.0, 300.0, 3.0, 30, 0.0, 0.0, 0.0)
        judgment = OfflineReflex().judge(base)
        self.assertEqual((Regime.TRENDING, Direction.LONG, 3, AdvisoryRisk.SAFE, False),
                         (judgment.regime, judgment.direction, judgment.setup_quality, judgment.risk_state, judgment.calibrated))
        self.assertGreater(judgment.direction_confidence, 0.80)
        self.assertEqual(Regime.CRISIS, OfflineReflex().judge(replace(base, drawdown=0.12)).regime)
        self.assertEqual(Direction.NEUTRAL, OfflineReflex().judge(replace(base, trend_t=0.2)).direction)

    def test_once_smoke_run_with_fake_source(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        books = FakeBooks(self.clock, {"BTC-USD": 60000.0, "ETH-USD": 3000.0})
        rules = FakeRules()
        with mock.patch("bot.autonomous.runtime._public_book_source", return_value=books), \
                mock.patch("bot.autonomous.runtime._public_rules_source", return_value=rules), \
                mock.patch("bot.autonomous.runtime.time.time_ns", return_value=self.clock.us * 1000):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(0, main(["--paper", "--once", "--data-dir", str(self.dir)]))
        status = json.loads(out.getvalue())
        self.assertEqual({"BTC-USD", "ETH-USD"}, set(status["instruments"]))
        self.assertEqual(("paper", False, False, 0), (status["mode"], status["live"], status["armed_paper"], status["fills"]))
        self.assertEqual(status, json.loads((self.dir / "status.json").read_text()))
        self.assertEqual({"warmup"}, {r["status"] for r in status["instruments"].values()})
        self.assertEqual(2, books.calls)  # one public book read per instrument
        self.assertEqual(2, rules.calls)  # one public product-details read per instrument
        db = open_db(self.dir / "runtime.sqlite3")
        self.addCleanup(db.close)
        rows = db.execute("SELECT instrument, candle_close_us, event_us, token_estimate FROM snapshots").fetchall()
        self.assertEqual({"BTC-USD", "ETH-USD"}, {r["instrument"] for r in rows})
        self.assertTrue(all(r["candle_close_us"] % 60_000_000 == 0 and r["event_us"] >= r["candle_close_us"]
                            and r["token_estimate"] < 400 for r in rows))
        self.assertEqual("60", dict(db.execute("SELECT key, value FROM meta").fetchall())["candle_seconds"])
        with mock.patch("bot.autonomous.runtime._public_book_source", return_value=books), \
                mock.patch("bot.autonomous.runtime._public_rules_source", return_value=rules), \
                self.assertRaisesRegex(ValueError, "config mismatch"):
            main(["--paper", "--once", "--data-dir", str(self.dir), "--candle-seconds", "300"])


def seed_labels(rt, hits, misses, magnitude=0.015, gate_open=1, instrument="AAVE-USD", reflex_id="fixed-test",
                regime="trending", mid_magnitude=None, prefix="seed"):
    """Resolved long labels behind gate-open decisions, far in the past so they never collide with live candles.

    ``magnitude`` is the executable (cost-aware) return; ``mid_magnitude`` the mid return, defaulting to the same."""
    for k in range(hits + misses):
        decision_id = "%s-%d-%d" % (prefix, gate_open, k)
        hit = k < hits
        rt.db.execute("INSERT INTO decisions(decision_id,instrument,candle_close_us,reflex_id,calibrated,regime,direction,confidence,"
                      "toxic,quality,risk_state,gate_open,action,reasons,mid,created_us) "
                      "VALUES (?,?,?,?,0,?,'long',0.85,0,2,'safe',?,'hold','[]',100,0)",
                      (decision_id, instrument, k * 60_000_000, reflex_id, regime, gate_open))
        mid = magnitude if mid_magnitude is None else mid_magnitude
        rt.db.execute("INSERT INTO labels(decision_id,instrument,direction,confidence,entry_mid,exit_mid,net_return,hit,labeled_us,"
                      "status,mid_return,exec_return,regime,label_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'exec-v1')",
                      (decision_id, instrument, "long", 0.85, 100.0, 100.0, magnitude if hit else -magnitude, int(hit), 0,
                       "resolved", mid if hit else -mid, magnitude if hit else -magnitude, regime))


class ReviewFindingTests(RuntimeCase):
    def test_calibrated_kelly_caps_at_position_limit_instead_of_refusing(self):
        rt = self.runtime(GOOD)
        seed_labels(rt, 72, 48)
        result = self.advance(rt, 21)["instruments"]["AAVE-USD"]
        self.assertEqual("buy", result["status"], result["reasons"])
        self.assertIn("SIZE_CAPPED", result["reasons"])
        fill = rt.db.execute("SELECT * FROM fills").fetchone()
        self.assertEqual(0, fill["exploration"])
        self.assertTrue(D("499") < D(fill["notional_usd"]) <= D("500"), fill["notional_usd"])

    def test_negative_calibrated_edge_holds_and_never_explores(self):
        rt = self.runtime(GOOD, explore_fraction=0.001)
        seed_labels(rt, 48, 72)
        result = self.advance(rt, 21)["instruments"]["AAVE-USD"]
        self.assertEqual("hold", result["status"])
        self.assertIn("NEGATIVE_EDGE", result["reasons"])
        self.assertNotIn("EXPLORATION", result["reasons"])
        self.assertEqual(0, rt.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0])

    def test_cohort_counts_only_gate_open_decisions(self):
        rt = self.runtime(GOOD)
        seed_labels(rt, 72, 48, gate_open=0)
        self.assertIn("CALIBRATION_INSUFFICIENT", self.advance(rt, 21)["instruments"]["AAVE-USD"]["reasons"])

    def test_low_confidence_escalation_does_not_suppress_time_stop(self):
        rt = self.runtime(GOOD, explore_fraction=0.001, horizon_candles=3)
        self.assertEqual("buy", self.advance(rt, 21)["instruments"]["AAVE-USD"]["status"])
        rt.reflex = Fixed(replace(GOOD, direction_confidence=0.55))
        self.advance(rt, 2)
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
        self.assertEqual("sell", result["status"], result["reasons"])
        self.assertIn("LOW_CONFIDENCE", result["reasons"])
        self.assertEqual({}, rt.positions())

    def test_crisis_exits_an_open_position(self):
        rt = self.runtime(GOOD, explore_fraction=0.001)
        self.assertEqual("buy", self.advance(rt, 21)["instruments"]["AAVE-USD"]["status"])
        rt.reflex = Fixed(replace(GOOD, regime=Regime.CRISIS))
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
        self.assertEqual("sell", result["status"], result["reasons"])
        self.assertIn("BRAIN_CRISIS", result["reasons"])

    def test_drawdown_latch_exits_an_open_position(self):
        rt = self.runtime(GOOD, explore_fraction=0.001)
        self.assertEqual("buy", self.advance(rt, 21)["instruments"]["AAVE-USD"]["status"])
        rt.db.execute("INSERT INTO latches(kind,action,reason,at_us) VALUES ('DRAWDOWN_LATCH','set','test',0)")
        self.assertEqual("sell", self.advance(rt, 1)["instruments"]["AAVE-USD"]["status"])

    def test_reflex_exception_is_contained_and_atomic(self):
        class Boom:
            reflex_id = "boom"
            def judge(self, snapshot):
                raise RuntimeError("provider exploded")
        rt = self.runtime(GOOD)
        self.advance(rt, 20)
        rt.reflex = Boom()
        self.clock.us += 60_000_000
        before = rt.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        result = rt.cycle()["instruments"]["AAVE-USD"]
        self.assertEqual(["DECISION_ERROR:RuntimeError"], result["reasons"])
        self.assertEqual(before, rt.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])
        self.assertFalse(rt.db.in_transaction)
        rt.reflex = Fixed(GOOD)
        retried = rt.cycle()["instruments"]["AAVE-USD"]  # same candle: no DUPLICATE_CANDLE lock-out
        self.assertNotIn("DUPLICATE_CANDLE", retried["reasons"])
        self.assertIn("decision_id", retried)

    def test_serve_survives_a_cycle_exception(self):
        rt = self.runtime(GOOD)
        calls = {"n": 0}
        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return {}
        rt.cycle = flaky
        self.assertEqual(2, rt.serve(lambda: calls["n"] >= 3, sleep=lambda s: None))
        self.assertEqual(3, calls["n"])

    def test_label_window_is_bounded_and_gaps_are_unresolvable(self):
        rt = self.runtime(GOOD, horizon_candles=3)
        self.advance(rt, 21)
        self.clock.us += 10 * 60_000_000  # outage: no snapshot inside [close+h, close+h+step)
        self.advance(rt, 1)
        rows = rt.db.execute("SELECT status, hit FROM labels").fetchall()
        self.assertEqual([("unresolvable", None)], [tuple(r) for r in rows])
        self.assertIsNone(rt.brier()[0])

    def test_features_require_contiguous_candles(self):
        rt = self.runtime(GOOD)
        self.advance(rt, 25)
        self.clock.us += 5 * 60_000_000
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
        self.assertEqual("warmup", result["status"])
        self.assertEqual(0, result["history"])

    def test_observation_must_sit_at_a_true_candle_close(self):
        rt = self.runtime()
        self.clock.us += 30_000_000
        result = rt.cycle()["instruments"]["AAVE-USD"]
        self.assertEqual(["AWAY_FROM_CLOSE"], result["reasons"])
        self.clock.us += 31_000_000
        rt.cycle()
        close = rt.db.execute("SELECT candle_close_us, event_us FROM snapshots").fetchone()
        self.assertEqual(0, close["candle_close_us"] % 60_000_000)
        self.assertTrue(0 <= close["event_us"] - close["candle_close_us"] <= rt.config.close_tolerance_us)

    def test_local_clock_ahead_of_exchange_still_snapshots_the_close(self):
        # Observed in WSL: local clock ~4 s ahead of Coinbase. A wake at local close+1 s sees an exchange
        # event ~3 s before the boundary; it is the pre-close book and belongs to that close, not 57 s late.
        rt = PaperRuntime(RuntimeConfig(data_dir=self.dir, instruments=("AAVE-USD",)),
            book_source=FakeBooks(self.clock, {"AAVE-USD": lambda: 100.0}, lag_us=4_000_000), clock_us=self.clock)
        self.addCleanup(rt.close)
        result = rt.cycle()["instruments"]["AAVE-USD"]
        self.assertNotIn("AWAY_FROM_CLOSE", result["reasons"])
        close = rt.db.execute("SELECT candle_close_us, event_us FROM snapshots").fetchone()
        self.assertEqual(START_US, close["candle_close_us"])
        self.assertLessEqual(abs(close["event_us"] - close["candle_close_us"]), rt.config.close_tolerance_us)

    def test_brier_split_and_unresolved_escalations(self):
        rt = self.runtime(replace(GOOD, direction_confidence=0.5), horizon_candles=3)
        self.advance(rt, 25)
        status = json.loads((self.dir / "status.json").read_text())
        self.assertIn("brier_directional", status)
        self.assertIn("brier_neutral", status)
        self.assertEqual(status["brier_directional"], 0.25)
        self.assertIsNone(status["brier_neutral"])
        pending = status["pending_escalations"]
        self.assertGreater(pending, 0)
        first = rt.db.execute("SELECT escalation_id FROM escalations LIMIT 1").fetchone()[0]
        rt.resolve_escalation(first, "brain reviewed")
        self.assertEqual(pending - 1, rt.write_status(self.clock.us, {}, 0)["pending_escalations"])

    def test_restart_with_mismatched_config_is_refused(self):
        self.runtime().close()
        with self.assertRaisesRegex(ValueError, "config mismatch"):
            PaperRuntime(RuntimeConfig(data_dir=self.dir, instruments=("AAVE-USD",), starting_cash_usd=D("5000")),
                         book_source=lambda p: {}, clock_us=self.clock)
        with self.assertRaisesRegex(ValueError, "config mismatch"):
            PaperRuntime(RuntimeConfig(data_dir=self.dir, instruments=("AAVE-USD",), candle_seconds=300),
                         book_source=lambda p: {}, clock_us=self.clock)

    def test_every_table_refuses_update_and_delete(self):
        import sqlite3
        from bot.autonomous.runtime import _APPEND_ONLY
        rt = self.runtime(GOOD, explore_fraction=0.001, horizon_candles=3)
        self.advance(rt, 26)
        rt.db.execute("INSERT INTO latches(kind,action,reason,at_us) VALUES ('X','set','t',0)")
        rt.db.execute("INSERT INTO escalations VALUES ('e','d','LOW_CONFIDENCE','{}','pending_brain',0)")
        rt.resolve_escalation("e", "reviewed")
        rt.db.execute("INSERT INTO reflex_calls(at_us,reflex_id,outcome) VALUES (0,'jev:test','ok')")
        for table in _APPEND_ONLY:
            self.assertGreater(rt.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
            for sql in (f"UPDATE {table} SET rowid=rowid", f"DELETE FROM {table}"):
                with self.subTest(sql), self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    rt.db.execute(sql)

    def test_snapshot_wire_is_exact_and_budget_enforced(self):
        from unittest import mock
        rt = self.runtime()
        self.advance(rt, 1)
        snap = rt.snapshot("AAVE-USD", {"bids": [[99.99, 1]], "asks": [[100.01, 1]]}, self.clock.us - 500_000,
                           self.clock.us - 100_000, self.clock.us, rt.mark({}, self.clock.us))
        wire = json.loads(snap.wire())
        self.assertEqual({"v", "i", "c", "mid", "spr", "imb", "rv", "mom", "t", "n", "inv", "dd", "dl"}, set(wire))
        self.assertEqual(-(-len(snap.wire().encode()) // 3), snap.token_estimate())
        with mock.patch.object(Snapshot, "token_estimate", return_value=400), self.assertRaises(ValueError):
            rt.snapshot("AAVE-USD", {"bids": [[99.99, 1]], "asks": [[100.01, 1]]}, self.clock.us - 500_000,
                        self.clock.us - 100_000, self.clock.us, rt.mark({}, self.clock.us))

    def test_nightly_review_is_bounded_to_its_day(self):
        rt = self.runtime(GOOD, explore_fraction=0.001, horizon_candles=3)
        seed_labels(rt, 1, 1)  # epoch-day rows must not enter today's review
        self.advance(rt, 30, drift=0.004)
        report = nightly_review(self.dir / "runtime.sqlite3", self.dir / "reviews", now_us=self.clock.us, day=_day(self.clock.us), fill_cap=1)
        self.assertNotIn("seed-1-0", [m["decision_id"] for m in report["misses"]])
        self.assertTrue(all(not d.startswith("seed") for d in report["metrics"]["decision_ids_sample"]))
        self.assertEqual(1, len(report["fills"]))
        self.assertTrue(report["metrics"]["fills_truncated"])

    def test_hot_queries_are_indexed(self):
        rt = self.runtime()
        names = {r[0] for r in rt.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for index in ("decisions_close", "labels_labeled", "fills_instrument", "escalations_created"):
            self.assertIn(index, names)


class SecondReviewTests(RuntimeCase):
    def open_position(self, **config):
        rt = self.runtime(GOOD, explore_fraction=0.001, **config)
        self.assertEqual("buy", self.advance(rt, 21)["instruments"]["AAVE-USD"]["status"])
        good_source = rt.book_source
        def failing(product):
            raise ConnectionError("public book outage")
        rt.book_source = failing
        self.assertEqual(["ConnectionError"], self.advance(rt, 1)["instruments"]["AAVE-USD"]["reasons"])
        rt.book_source = good_source
        return rt

    def test_reduce_exits_during_warmup_after_one_missed_candle(self):
        rt = self.open_position()
        rt.reflex = Fixed(replace(GOOD, risk_state=AdvisoryRisk.REDUCE))
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
        self.assertEqual("sell", result["status"], result["reasons"])
        self.assertIn("WARMUP", result["reasons"])
        self.assertEqual({}, rt.positions())

    def test_crisis_exits_during_warmup_and_latches(self):
        rt = self.open_position()
        rt.reflex = Fixed(replace(GOOD, regime=Regime.CRISIS))
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
        self.assertEqual("sell", result["status"], result["reasons"])
        self.assertIn("CRISIS_HALT", rt.latched())

    def test_time_stop_exits_during_warmup(self):
        rt = self.open_position(horizon_candles=3)
        self.advance(rt, 1)
        self.assertEqual("sell", self.advance(rt, 1)["instruments"]["AAVE-USD"]["status"])

    def test_warmup_never_enters_and_still_latches_crisis(self):
        rt = self.runtime(replace(GOOD, regime=Regime.CRISIS), explore_fraction=0.001)
        result = self.advance(rt, 3)["instruments"]["AAVE-USD"]
        self.assertEqual("warmup", result["status"])
        self.assertIn("CRISIS_HALT", rt.latched())
        self.assertEqual(0, rt.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0])
        rt.reflex = Fixed(GOOD)
        self.advance(rt, 3)
        self.assertEqual(0, rt.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0])
        # Warmup decisions never become calibration labels.
        self.advance(rt, 70)
        warm = rt.db.execute("SELECT COUNT(*) FROM labels l JOIN decisions d USING(decision_id) WHERE d.phase='warmup'").fetchone()[0]
        self.assertEqual(0, warm)

    def test_nightly_review_defaults_to_previous_complete_day(self):
        rt = self.runtime(GOOD, explore_fraction=0.001, horizon_candles=3)
        self.advance(rt, 30, drift=0.004)
        day = _day(self.clock.us)
        start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) * 1_000_000
        run_at = start + 86_400_000_000 + (6 * 60 + 10) * 60_000_000
        report = nightly_review(self.dir / "runtime.sqlite3", self.dir / "reviews", now_us=run_at)
        self.assertEqual(day, report["metrics"]["day"])
        self.assertGreater(report["metrics"]["decisions"], 0)
        self.assertGreater(report["metrics"]["fills"], 0)
        self.assertTrue((self.dir / "reviews" / f"nightly-{day}.json").exists())
        explicit = nightly_review(self.dir / "runtime.sqlite3", self.dir / "reviews", now_us=run_at, day=_day(run_at))
        self.assertEqual(0, explicit["metrics"]["decisions"])

    def test_first_open_meta_is_atomic(self):
        import sqlite3
        db = open_db(self.dir / "runtime.sqlite3")
        db.execute("CREATE TRIGGER boom BEFORE INSERT ON meta WHEN NEW.key='candle_seconds' "
                   "BEGIN SELECT RAISE(ABORT, 'injected'); END")
        db.close()
        with self.assertRaises(sqlite3.DatabaseError):
            PaperRuntime(RuntimeConfig(data_dir=self.dir, instruments=("AAVE-USD",)), book_source=lambda p: {}, clock_us=self.clock)
        db = sqlite3.connect(self.dir / "runtime.sqlite3")
        self.addCleanup(db.close)
        self.assertEqual(0, db.execute("SELECT COUNT(*) FROM meta").fetchone()[0])

    def test_calibration_ignores_labels_not_yet_known(self):
        rt = self.runtime(GOOD)
        seed_labels(rt, 72, 48)
        rt.db.execute("DROP TRIGGER labels_no_update")  # test-only: move seeded labels into the future
        rt.db.execute("UPDATE labels SET labeled_us=?", (self.clock.us + 10**12,))
        self.assertIn("CALIBRATION_INSUFFICIENT", self.advance(rt, 21)["instruments"]["AAVE-USD"]["reasons"])

    def test_low_confidence_escalations_collapse_per_instrument_hour(self):
        rt = self.runtime(replace(GOOD, direction_confidence=0.5))
        self.advance(rt, 40)
        self.assertEqual(1, rt.db.execute("SELECT COUNT(*) FROM escalations WHERE code='LOW_CONFIDENCE'").fetchone()[0])
        self.advance(rt, 61)
        self.assertEqual(2, rt.db.execute("SELECT COUNT(*) FROM escalations WHERE code='LOW_CONFIDENCE'").fetchone()[0])

    def test_status_counts_use_indexes(self):
        rt = self.runtime()
        for sql in ("SELECT COUNT(*) FROM labels WHERE status='resolved'",):
            plan = " ".join(r[3] for r in rt.db.execute("EXPLAIN QUERY PLAN " + sql))
            self.assertIn("INDEX", plan.upper(), plan)
        plan = " ".join(r[3] for r in rt.db.execute("EXPLAIN QUERY PLAN " + rt.PENDING_ESCALATIONS_SQL, (0,)))
        self.assertIn("escalations_created", plan)


class MarketRulesAndCostLabelTests(RuntimeCase):
    def test_public_product_details_fetch_parses_rules(self):
        from data.public_metrics import fetch_coinbase_product
        class Response:
            status_code, content = 200, b"{}"
            def raise_for_status(self): pass
            def json(self): return online_rules(base_increment="0.001", quote_increment="0.01", status="online")
        seen = {}
        class Log:
            def record(self, **kw): seen.update(kw)
        def getter(url, **kw):
            seen["url"] = url
            return Response()
        rules = fetch_coinbase_product(Log(), "AAVE-USD", getter=getter)
        self.assertEqual("https://api.exchange.coinbase.com/products/AAVE-USD", seen["url"])
        self.assertEqual(("0.001", "0.01", "online", False), (rules["base_increment"], rules["quote_increment"],
                                                              rules["status"], rules["trading_disabled"]))
        self.assertFalse(rules["dispatched"])
        with self.assertRaises(ValueError):
            fetch_coinbase_product(Log(), "AAVE/USD", getter=getter)

    def test_rules_are_cached_with_ttl(self):
        rt = self.runtime(market_rules_ttl_us=5 * 60_000_000)
        self.advance(rt, 4)
        self.assertEqual(1, self.rules.calls)
        self.advance(rt, 2)
        self.assertEqual(2, self.rules.calls)

    def test_unavailable_markets_hold(self):
        for changes in (dict(status="delisted"), dict(trading_disabled=True), dict(cancel_only=True), dict(post_only=True)):
            with self.subTest(changes):
                self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.dir = Path(self.tmp.name)
                rt = self.runtime(GOOD, rules=FakeRules(**changes), explore_fraction=0.001)
                result = self.advance(rt, 22)["instruments"]["AAVE-USD"]
                self.assertEqual(["MARKET_UNAVAILABLE"], result["reasons"])
                self.assertEqual(0, rt.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0])

    def test_rules_outage_without_fresh_cache_holds(self):
        rules = FakeRules()
        rules.fail = True
        rt = self.runtime(GOOD, rules=rules, explore_fraction=0.001)
        self.assertEqual(["MARKET_RULES_UNAVAILABLE"], self.advance(rt, 22)["instruments"]["AAVE-USD"]["reasons"])

    def test_fill_rounds_to_base_increment_tick_and_min_funds(self):
        from bot.autonomous.runtime import MarketRules
        rules = MarketRules.parse(online_rules(base_increment="0.001", quote_increment="0.05", min_market_funds="10"), 0, "AAVE-USD")
        asks = [(100.0, 0.3337), (100.05, 10.0)]
        qty, avg, notional, fee = paper_fill(asks, "buy", D("50.30"), None, D("100.07"), D("0.006"), rules)
        self.assertEqual(qty, qty.quantize(D("0.001")))
        self.assertEqual(0, fee % D("0.05"))  # fee rounds up to a multiple of the quote increment
        self.assertLessEqual(notional + fee, D("50.30"))
        self.assertEqual(D("100.05"), rules.round_limit(D("100.07"), "buy"))  # buy limit rounds down
        self.assertEqual(D("100.10"), rules.round_limit(D("100.07"), "sell"))  # sell limit rounds up
        self.assertIsNone(paper_fill(asks, "buy", D("9"), None, D("100.05"), D("0.006"), rules))  # below min funds

    def test_entry_size_floors_to_quote_increment(self):
        self.price["v"] = 1000.0  # a whole-dollar tick must not round the buy limit below the ask
        rt = self.runtime(GOOD, rules=FakeRules(quote_increment="1"))
        seed_labels(rt, 72, 48)
        self.advance(rt, 21)
        fill = rt.db.execute("SELECT * FROM fills").fetchone()
        self.assertTrue(D("499") <= D(fill["notional_usd"]) <= D("500"))
        self.assertEqual(D(fill["fee_usd"]), D(fill["fee_usd"]).quantize(D("1")))

    def test_labels_use_executable_prices_and_store_both_returns(self):
        rt = self.runtime(GOOD, horizon_candles=3)
        rt.book_source.spread = 0.0018  # 18 bp spread: the mid is flat but a round trip loses the spread
        self.advance(rt, 25)
        row = rt.db.execute("SELECT * FROM labels WHERE status='resolved' LIMIT 1").fetchone()
        self.assertAlmostEqual(0.0, row["mid_return"], places=9)
        self.assertAlmostEqual(0.9991 / 1.0009 - 1 - 0.012, row["exec_return"], places=9)
        self.assertEqual(0, row["hit"])
        self.assertEqual("trending", row["regime"])

    def test_calibration_uses_executable_not_mid_returns(self):
        rt = self.runtime(GOOD, explore_fraction=0.001)
        # Mid returns look like a 60% winner; after spread and fees every label lost.
        seed_labels(rt, 0, 120, magnitude=0.003, mid_magnitude=0.015)
        result = self.advance(rt, 21)["instruments"]["AAVE-USD"]
        self.assertIn("NEGATIVE_EDGE", result["reasons"])

    def test_calibration_cohort_is_per_regime(self):
        rt = self.runtime(GOOD)
        seed_labels(rt, 72, 48, regime="mean_reverting")
        self.assertIn("CALIBRATION_INSUFFICIENT", self.advance(rt, 21)["instruments"]["AAVE-USD"]["reasons"])

    def test_legacy_database_migrates_cleanly(self):
        import sqlite3
        db = sqlite3.connect(self.dir / "runtime.sqlite3")
        db.executescript("""
            CREATE TABLE labels(decision_id TEXT PRIMARY KEY, instrument TEXT, direction TEXT, confidence REAL,
              entry_mid REAL, exit_mid REAL, net_return REAL, hit INTEGER, labeled_us INTEGER);
            CREATE TABLE decisions(decision_id TEXT PRIMARY KEY, instrument TEXT, candle_close_us INTEGER, reflex_id TEXT,
              calibrated INTEGER, regime TEXT, direction TEXT, confidence REAL, toxic INTEGER, quality INTEGER, risk_state TEXT,
              gate_open INTEGER, action TEXT, reasons TEXT, mid REAL, created_us INTEGER);
            INSERT INTO labels VALUES ('old','AAVE-USD','long',0.85,100,101,0.01,1,0);
        """)
        db.close()
        rt = self.runtime(GOOD)
        columns = {r["name"] for r in rt.db.execute("PRAGMA table_info(labels)")}
        self.assertTrue({"status", "mid_return", "exec_return", "regime"} <= columns)
        self.assertIn("phase", {r["name"] for r in rt.db.execute("PRAGMA table_info(decisions)")})
        old = rt.db.execute("SELECT * FROM labels WHERE decision_id='old'").fetchone()
        self.assertIsNone(old["exec_return"])  # legacy mid-only labels never feed executable calibration


class RulesOutageAndDustTests(RuntimeCase):
    def held(self, **config):
        config.setdefault("explore_fraction", 0.001)
        rt = self.runtime(GOOD, **config)
        self.assertEqual("buy", self.advance(rt, 21)["instruments"]["AAVE-USD"]["status"])
        return rt

    def test_rules_outage_never_blocks_crisis_or_reduce_exit(self):
        for judgment, latched in ((replace(GOOD, regime=Regime.CRISIS), True), (replace(GOOD, risk_state=AdvisoryRisk.REDUCE), False)):
            with self.subTest(judgment.regime):
                self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.dir = Path(self.tmp.name)
                rt = self.held(market_rules_ttl_us=60_000_000)
                self.rules.fail = True
                rt.reflex = Fixed(judgment)
                before = rt.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
                result = self.advance(rt, 2)["instruments"]["AAVE-USD"]
                self.assertEqual({}, rt.positions(), result["reasons"])
                self.assertEqual("sell", rt.db.execute("SELECT side FROM fills ORDER BY created_us DESC LIMIT 1").fetchone()[0])
                self.assertEqual(latched, "CRISIS_HALT" in rt.latched())
                self.assertGreater(rt.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], before)

    def test_rules_outage_with_no_cache_exits_with_default_rounding(self):
        rt = self.held()
        rt._rules.clear()
        self.rules.fail = True
        rt.reflex = Fixed(replace(GOOD, risk_state=AdvisoryRisk.REDUCE))
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
        self.assertEqual("sell", result["status"], result["reasons"])
        self.assertIn("MARKET_RULES_UNAVAILABLE", result["reasons"])

    def test_untradable_flags_block_entries_but_only_real_halts_block_exits(self):
        rt = self.held()
        rt._rules.clear()
        self.rules.changes = dict(status="delisted")
        rt.reflex = Fixed(replace(GOOD, risk_state=AdvisoryRisk.REDUCE))
        self.assertEqual("sell", self.advance(rt, 1)["instruments"]["AAVE-USD"]["status"])  # status alone does not trap
        rt.reflex = Fixed(GOOD)
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
        self.assertIn("MARKET_UNAVAILABLE", result["reasons"])
        self.assertIn("decision_id", result)  # the decision is still recorded
        for flag in ("trading_disabled", "cancel_only"):
            with self.subTest(flag):
                self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.dir = Path(self.tmp.name)
                rt = self.held()
                rt._rules.clear()
                self.rules.changes = {flag: True}
                rt.reflex = Fixed(replace(GOOD, risk_state=AdvisoryRisk.REDUCE))
                result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
                self.assertEqual("hold", result["status"])
                self.assertIn("EXIT_REFUSED_MARKET_HALT", result["reasons"])

    def test_dust_below_increment_does_not_pin_the_entry_slot(self):
        rt = self.held()
        rt._rules.clear()
        self.rules.changes = dict(base_increment="0.01")
        rt.reflex = Fixed(replace(GOOD, risk_state=AdvisoryRisk.REDUCE))
        self.assertEqual("sell", self.advance(rt, 1)["instruments"]["AAVE-USD"]["status"])
        remainder = rt.positions()["AAVE-USD"]
        self.assertTrue(D(0) < remainder < D("0.01"))
        rt.reflex = Fixed(GOOD)
        status = self.advance(rt, 1)
        self.assertEqual("buy", status["instruments"]["AAVE-USD"]["status"], status["instruments"]["AAVE-USD"]["reasons"])
        self.assertIn("AAVE-USD", status["dust_usd"])

    def test_sub_minimum_value_is_flat_for_entries_and_still_sellable(self):
        rt = self.held(rules=FakeRules(min_market_funds="5"))
        self.price["v"] *= 0.45
        status = self.advance(rt, 1)
        self.assertNotIn("ENTRY_SLOT_OCCUPIED", status["instruments"]["AAVE-USD"]["reasons"])
        self.assertIn("AAVE-USD", status["dust_usd"])
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.dir = Path(self.tmp.name)
        self.price["v"] = 100.0
        rt = self.held(rules=FakeRules(min_market_funds="5"))
        self.price["v"] *= 0.45
        rt.reflex = Fixed(replace(GOOD, risk_state=AdvisoryRisk.REDUCE))
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
        self.assertEqual("sell", result["status"], result["reasons"])  # reduce-only sells ignore min funds
        self.assertEqual({}, rt.positions())

    def test_short_label_is_relative_to_entry_proceeds(self):
        rt = self.runtime(replace(GOOD, direction=Direction.SHORT), horizon_candles=3)
        rt.book_source.spread = 0.0018
        self.advance(rt, 25)
        row = rt.db.execute("SELECT * FROM labels WHERE status='resolved' LIMIT 1").fetchone()
        self.assertAlmostEqual(1 - 1.0009 / 0.9991 - 0.012, row["exec_return"], places=9)

    def test_entry_refreshes_rules_older_than_five_minutes(self):
        rt = self.runtime(GOOD, explore_fraction=0.001)
        self.advance(rt, 20)
        calls = self.rules.calls
        self.assertEqual(1, calls)
        self.assertEqual("buy", self.advance(rt, 1)["instruments"]["AAVE-USD"]["status"])
        self.assertEqual(2, self.rules.calls)

    def test_rules_identity_and_flags_are_validated(self):
        from bot.autonomous.runtime import MarketRules
        with self.assertRaises(Unavailable):
            MarketRules.parse(online_rules(id="ETH-USD"), 0, "AAVE-USD")
        missing = online_rules()
        del missing["cancel_only"]
        with self.assertRaises(Unavailable):
            MarketRules.parse(missing, 0, "AAVE-USD")

    def test_exploration_below_min_funds_holds(self):
        rt = self.runtime(GOOD, explore_fraction=0.001, rules=FakeRules(min_market_funds="20"))
        result = self.advance(rt, 21)["instruments"]["AAVE-USD"]
        self.assertIn("BELOW_MIN_FUNDS", result["reasons"])
        self.assertEqual(0, rt.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0])

    def test_legacy_mid_labels_never_enter_brier(self):
        rt = self.runtime()
        rt.db.execute("INSERT INTO labels(decision_id,instrument,direction,confidence,hit,labeled_us,status) "
                      "VALUES ('legacy','AAVE-USD','long',0.9,0,0,'resolved')")
        self.assertEqual((None, 0), rt.brier())
        report = nightly_review(self.dir / "runtime.sqlite3", self.dir / "reviews", now_us=86_400_000_000, day="1970-01-01")
        self.assertEqual(0, report["metrics"]["resolved_labels"])

    def test_schema_upgrade_from_v2_is_accepted_and_unknown_refused(self):
        import sqlite3
        db = open_db(self.dir / "runtime.sqlite3")
        for k, v in (("schema", "runtime-v2"), ("starting_cash_usd", "10000"), ("candle_seconds", "60")):
            db.execute("INSERT INTO meta VALUES (?,?)", (k, v))
        db.close()
        rt = self.runtime()
        self.assertEqual("runtime-v3", rt.schema_version())
        rt.close()
        other = self.dir / "other"
        db = open_db(other / "runtime.sqlite3")
        db.execute("INSERT INTO meta VALUES ('schema','runtime-v9')")
        db.close()
        with self.assertRaisesRegex(ValueError, "schema"):
            PaperRuntime(RuntimeConfig(data_dir=other, instruments=("AAVE-USD",)), book_source=lambda p: {},
                         rules_source=FakeRules(), clock_us=self.clock)

    def test_failed_product_fetch_is_logged(self):
        from data.public_metrics import fetch_coinbase_product
        seen = []
        class Log:
            def record(self, **kw): seen.append(kw)
        def getter(url, **kw):
            raise ConnectionError("down")
        with self.assertRaises(ConnectionError):
            fetch_coinbase_product(Log(), "AAVE-USD", getter=getter)
        self.assertEqual([False], [r["ok"] for r in seen])
        from bot.autonomous.runtime import MarketRules
        self.assertFalse(hasattr(MarketRules, "ceil_fee"))


if __name__ == "__main__":
    unittest.main()
