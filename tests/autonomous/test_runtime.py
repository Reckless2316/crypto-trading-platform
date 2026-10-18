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


class Fixed:
    reflex_id = "fixed-test"

    def __init__(self, judgment):
        self.judgment = judgment

    def judge(self, snapshot):
        return self.judgment


GOOD = Judgment(Regime.TRENDING, Direction.LONG, 0.85, False, 2, AdvisoryRisk.SAFE, "fixed-test")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.clock = Clock(START_US + 1_000_000)
        self.price = {"v": 100.0}

    def runtime(self, judgment=None, **config):
        config.setdefault("instruments", ("AAVE-USD",))
        config.setdefault("paper_armed", True)
        books = FakeBooks(self.clock, {"AAVE-USD": lambda: self.price["v"]})
        rt = PaperRuntime(RuntimeConfig(data_dir=self.dir, **config), book_source=books, clock_us=self.clock,
                          reflex=Fixed(judgment) if judgment else None)
        self.addCleanup(rt.close)
        return rt

    def advance(self, rt, n, drift=0.0):
        out = None
        for _ in range(n):
            self.clock.us += 60_000_000
            self.price["v"] *= 1 + drift
            out = rt.cycle()
        return out

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
        report = nightly_review(self.dir / "runtime.sqlite3", self.dir / "reviews", now_us=self.clock.us)
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
        with mock.patch("bot.autonomous.runtime._public_book_source", return_value=books), \
                mock.patch("bot.autonomous.runtime.time.time_ns", return_value=self.clock.us * 1000):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(0, main(["--paper", "--once", "--data-dir", str(self.dir)]))
        status = json.loads(out.getvalue())
        self.assertEqual({"BTC-USD", "ETH-USD"}, set(status["instruments"]))
        self.assertEqual(0, status["fills"])
        db = open_db(self.dir / "runtime.sqlite3")
        self.addCleanup(db.close)
        self.assertEqual(2, db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
