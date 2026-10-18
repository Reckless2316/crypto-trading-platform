import unittest
from types import SimpleNamespace

from bot.autonomous.paper_cycle import CycleOutcome
from bot.autonomous.worker import PaperWorker, foreign_secret_names, role_secret_names


def outcome(dispatched=False, reasons=(), escalations=(), halts=()):
    policy = SimpleNamespace(reason_codes=reasons, escalation_codes=escalations, halt_transitions=halts, proposal=None)
    return CycleOutcome(dispatched, reasons, policy if reasons or escalations or halts else None)


class WorkerTests(unittest.TestCase):
    def test_reflex_has_no_venue_key(self):
        self.assertEqual(role_secret_names("reflex"), frozenset({"TYPESAFE_API_KEY"}))
        self.assertNotIn("TYPESAFE_API_KEY", role_secret_names("authority"))
        self.assertEqual(role_secret_names("dashboard"), frozenset())

    def test_foreign_secret_names_do_not_include_values(self):
        env = {"TYPESAFE_API_KEY": "secret-value", "SURGE_COINBASE_API_KEY": "another-secret", "PATH": "/usr/bin"}
        foreign = foreign_secret_names("reflex", env)
        self.assertEqual(foreign, frozenset({"SURGE_COINBASE_API_KEY"}))
        self.assertNotIn("secret-value", foreign)
        self.assertEqual(foreign_secret_names("authority", {"SURGE_COINBASE_API_KEY": "x", "SURGE_COINBASE_API_SECRET": "y"}), frozenset())

    def test_live_venue_is_refused(self):
        with self.assertRaises(ValueError):
            PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="coinbase")))

    def test_each_close_is_considered_once_and_history_is_not_replayed(self):
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        calls = []

        def consider(cycle):
            calls.append(cycle)
            return outcome()

        first = worker.on_close(1_000, consider)
        second = worker.on_close(1_000, consider)
        older = worker.on_close(900, consider)
        self.assertEqual(first["status"], "processed")
        self.assertEqual(second["status"], "duplicate_or_historical")
        self.assertEqual(older["status"], "duplicate_or_historical")
        self.assertEqual(len(calls), 1)

    def test_crisis_halts_later_candles(self):
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        calls = []

        def consider(cycle):
            calls.append(1)
            return outcome(escalations=("BRAIN_CRISIS",), halts=("crisis_halt",))

        worker.on_close(1_000, consider)
        later = worker.on_close(2_000, consider)
        self.assertEqual(len(calls), 1)
        self.assertEqual(later["status"], "halted")
        self.assertFalse(later["dispatched"])

    def test_kill_halts_later_candles(self):
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        calls = []

        def consider(cycle):
            calls.append(1)
            return outcome(reasons=("HARD_KILL",))

        worker.on_close(1_000, consider)
        worker.on_close(2_000, consider)
        self.assertEqual(len(calls), 1)

    def test_review_fires_once_at_0015_utc_and_cannot_approve(self):
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        fired = []
        day = 3 * 86_400 * 1_000_000
        slot = day + 15 * 60 * 1_000_000

        def review(when):
            fired.append(when)
            return False

        before = worker.on_close(slot - 1, lambda cycle: outcome(), review)
        at_slot = worker.on_close(slot, lambda cycle: outcome(), review)
        again = worker.on_close(slot + 1, lambda cycle: outcome(), review)
        self.assertEqual(before["status"], "processed")
        self.assertEqual(fired, [slot])
        self.assertEqual(at_slot["status"], "processed")
        self.assertEqual(again["status"], "processed")

        def approve(when):
            return True

        with self.assertRaises(RuntimeError):
            worker.on_close(slot + 86_400 * 1_000_000, lambda cycle: outcome(), approve)

    def test_jev_crisis_and_low_confidence_do_not_submit(self):
        from bot.autonomous.contracts import (
            AdvisoryRisk, CandidateDecision, DecisionBatch, Direction, DirectionDistribution, Regime,
        )
        def batch(decision):
            return DecisionBatch("request", "a" * 64, "b" * 64, "model", 20, 7, 1, (decision,), None)
        def decision(regime, confidence, quality=2):
            return CandidateDecision(
                "aave", regime, Direction.LONG, quality, AdvisoryRisk.SAFE,
                DirectionDistribution(confidence, (1 - confidence) / 2, (1 - confidence) / 2),
                confidence, 0.9, 0.9, 0.9, 0.1)
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        crisis = worker.apply_jev_batch(batch(decision(Regime.CRISIS, 0.9)))
        self.assertEqual(crisis["status"], "escalated")
        self.assertFalse(crisis["dispatched"])
        self.assertTrue(worker.halted)
        low = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        paused = low.apply_jev_batch(batch(decision(Regime.TRENDING, 0.59)))
        self.assertEqual(paused["status"], "escalated")
        self.assertFalse(paused["dispatched"])
        opened = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        held = opened.apply_jev_batch(batch(decision(Regime.TRENDING, 0.85)))
        self.assertEqual(held["status"], "gate_open_unsubmitted")
        self.assertFalse(held["dispatched"])
        self.assertEqual(opened.considered, 0)

    def test_candle_reads_the_equity_mark_before_considering(self):
        import sqlite3
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE equity_marks(mark_id TEXT, account_id TEXT, nav TEXT, high_water_nav TEXT, "
            "day_open_nav TEXT, marked_at_us INTEGER)")
        connection.execute(
            "INSERT INTO equity_marks VALUES ('mark-1','paper','85','100','100',10)")
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        worker.attach_equity(connection, "paper")

        def consider(_cycle):
            raise AssertionError("the candle must read the mark before considering")

        result = worker.on_close(3_000, consider)
        self.assertEqual(result["status"], "halted")
        self.assertFalse(result["dispatched"])
        self.assertEqual(worker.considered, 0)

    def test_equity_mark_row_halts_the_next_candle(self):
        import sqlite3
        from decimal import Decimal
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE equity_marks(mark_id TEXT, account_id TEXT, nav TEXT, high_water_nav TEXT, "
            "day_open_nav TEXT, marked_at_us INTEGER)")
        connection.execute(
            "INSERT INTO equity_marks VALUES ('mark-1','paper','85','100','100',10)")
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        verdict = worker.block_from_equity_mark(connection, "paper")
        self.assertEqual(verdict["status"], "risk_blocked")
        self.assertTrue(verdict["drawdown_hit"])
        self.assertFalse(verdict["dispatched"])

        def consider(_cycle):
            raise AssertionError("a mark-driven halt must not consider the candle")

        result = worker.on_close(2_000, consider)
        self.assertEqual(result["status"], "halted")
        self.assertFalse(result["dispatched"])
        self.assertEqual(worker.considered, 0)
        clear = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        connection.execute(
            "INSERT INTO equity_marks VALUES ('mark-2','paper','99','100','100',11)")
        later = clear.block_from_equity_mark(connection, "paper")
        self.assertEqual(later["status"], "risk_clear")
        self.assertFalse(later["dispatched"])

    def test_loss_halt_skips_the_next_candle(self):
        from decimal import Decimal
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        worker.loss_block(Decimal("85"), Decimal("100"), Decimal("100"))

        def consider(_cycle):
            raise AssertionError("a loss halt must not consider the candle")

        result = worker.on_close(1_000, consider)
        self.assertEqual(result["status"], "halted")
        self.assertFalse(result["dispatched"])
        self.assertEqual(worker.considered, 0)

    def test_fifteen_percent_drawdown_blocks_before_a_snapshot(self):
        from decimal import Decimal
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        clear = worker.loss_block(Decimal("99"), Decimal("100"), Decimal("100"))
        self.assertEqual(clear["status"], "risk_clear")
        self.assertFalse(clear["dispatched"])
        self.assertFalse(worker.halted)

        def encode(*_args):
            raise AssertionError("a blocked account must not encode a snapshot")

        blocked = worker.hold_accounted_book(
            ((99.99, 2.0),), ((100.01, 1.0),), (100.0,) * 61, 2.0,
            Decimal("85"), Decimal("100"), encode, day_open=Decimal("100"))
        self.assertEqual(blocked["status"], "risk_blocked")
        self.assertTrue(blocked["drawdown_hit"])
        self.assertFalse(blocked["dispatched"])
        self.assertTrue(worker.halted)
        self.assertEqual(worker.considered, 0)

    def test_volatility_inventory_and_drawdown_are_inside_the_held_snapshot(self):
        from bot.autonomous.contracts import AccountRevision, Feature, NumericSnapshot, SharedContext
        from tests.autonomous.test_state import StateEncodingHelpers
        helper = StateEncodingHelpers()
        helper.setUp()
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        mids = (100.0,) * 30 + (101.0,) * 31

        def encode(mid, spread, imbalance, account):
            bundle = helper.ranked_bundle(("aave",))
            wanted = {
                "mid": (mid, True), "spread_bps": (spread, True), "imbalance": (imbalance, True),
                "rv_60m": (account["rv"], True), "rv_samples": (float(account["rv_samples"]), True),
                "base_inventory": (account["base_inventory"], True),
                "inventory_usd": (account["inventory_usd"], True), "eligible": (1.0, True),
            }
            features = tuple(Feature(field.name, wanted.get(field.name, (0.0, False))[0],
                wanted.get(field.name, (0.0, False))[1]) for field in helper.dictionary.candidate_fields)
            mask = sum(1 << index for index, feature in enumerate(features) if feature.valid)
            snapshot = NumericSnapshot(
                "aave", "AAVE-USD", "a" * 64, helper.cutoffs, features, mask,
                "b" * 64, AccountRevision(), release_hash=bundle.bundle_hash)
            shared = []
            for field in helper.dictionary.shared_fields:
                if field.name == "drawdown":
                    shared.append(Feature("drawdown", account["drawdown"], True))
                else:
                    shared.append(Feature(field.name, 0.0, False))
            shared_mask = sum(1 << index for index, feature in enumerate(shared) if feature.valid)
            return helper.encode(("aave",), snapshots=(snapshot,),
                context=SharedContext(tuple(shared), shared_mask))

        held = worker.hold_accounted_book(
            ((99.99, 2.0), (99.9, 4.0), (99.0, 1000.0)),
            ((100.01, 1.0), (100.1, 1.0), (101.0, 1000.0)),
            mids, 2.0, 90.0, 100.0, encode)
        self.assertEqual(held["status"], "hold_no_judgment")
        self.assertEqual(held["inventory_usd"], 200.0)
        self.assertEqual(held["drawdown"], 0.1)
        self.assertGreater(held["rv"], 0.0)
        self.assertFalse(held["dispatched"])
        self.assertEqual(worker.considered, 0)

    def test_quoted_book_is_inside_the_held_snapshot(self):
        from bot.autonomous.contracts import AccountRevision, Feature, NumericSnapshot
        from tests.autonomous.test_state import StateEncodingHelpers
        helper = StateEncodingHelpers()
        helper.setUp()
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))

        def encode(mid, spread, imbalance):
            bundle = helper.ranked_bundle(("aave",))
            features = []
            for field in helper.dictionary.candidate_fields:
                if field.name == "mid":
                    features.append(Feature("mid", mid, True))
                elif field.name == "spread_bps":
                    features.append(Feature("spread_bps", spread, True))
                elif field.name == "imbalance":
                    features.append(Feature("imbalance", imbalance, True))
                elif field.name == "eligible":
                    features.append(Feature("eligible", 1.0, True))
                else:
                    features.append(Feature(field.name, 0.0, False))
            mask = 0
            for index, feature in enumerate(features):
                if feature.valid:
                    mask |= 1 << index
            snapshot = NumericSnapshot(
                "aave", "AAVE-USD", "a" * 64, helper.cutoffs, tuple(features), mask,
                "b" * 64, AccountRevision(), release_hash=bundle.bundle_hash)
            return helper.encode(("aave",), snapshots=(snapshot,))

        held = worker.hold_quoted_book(
            ((99.99, 2.0), (99.9, 4.0), (99.0, 1000.0)),
            ((100.01, 1.0), (100.1, 1.0), (101.0, 1000.0)),
            encode)
        self.assertEqual(held["status"], "hold_no_judgment")
        self.assertEqual(held["mid"], 100.0)
        self.assertAlmostEqual(held["spread"], 2.0)
        self.assertEqual(held["imbalance"], 0.5)
        self.assertEqual(held["tokens"], 399)
        self.assertFalse(held["dispatched"])
        self.assertEqual(worker.considered, 0)

    def test_engine_snapshot_holds_after_the_book_is_quoted_in_code(self):
        from tests.autonomous.test_state import StateEncodingHelpers
        helper = StateEncodingHelpers()
        helper.setUp()
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        quoted = worker.quote_book(
            ((99.99, 2.), (99.9, 4.), (99., 1000.)),
            ((100.01, 1.), (100.1, 1.), (101., 1000.)))
        encoded = helper.encode(("aave",))
        held = worker.observe_state(encoded)
        self.assertEqual(quoted["mid"], 100.)
        self.assertFalse(quoted["dispatched"])
        self.assertEqual(held["status"], "hold_no_judgment")
        self.assertEqual(held["tokens"], 399)
        self.assertFalse(held["dispatched"])
        self.assertEqual(worker.considered, 0)

    def test_snapshot_at_the_token_cap_holds_and_over_cap_is_rejected(self):
        from bot.autonomous.contracts import TokenCount
        from tests.autonomous.support import encoded_state
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        held = worker.observe_state(encoded_state())
        self.assertEqual(held["status"], "hold_no_judgment")
        self.assertEqual(held["tokens"], 399)
        self.assertFalse(held["dispatched"])
        oversized = encoded_state()
        object.__setattr__(oversized, "token_count", TokenCount(400, "tok", "provider", "pinned_provider_tokenizer", "c" * 64))
        rejected = worker.observe_state(oversized)
        self.assertEqual(rejected["status"], "state_rejected")
        self.assertFalse(rejected["dispatched"])
        self.assertEqual(worker.considered, 0)

    def test_cli_lists_a_role_without_reading_the_process_environment(self):
        from bot.autonomous.worker import main
        self.assertEqual(main(["--role", "dashboard"]), 0)


if __name__ == "__main__":
    unittest.main()


class WorkerBookReadersAreReadOnlyTests(unittest.TestCase):
    """Dashboard-called book readers never open the observation database writable."""

    def test_book_and_eth_readers_open_mode_ro(self):
        import os
        import sqlite3
        import tempfile
        from unittest.mock import patch
        from bot.autonomous import worker
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            os.chdir(directory)
            self.addCleanup(os.chdir, previous)
            path = os.path.join("data", "autonomous", "book_observations.sqlite3")
            os.makedirs(os.path.dirname(path))
            with sqlite3.connect(path) as db:
                for table in ("book_tops", "eth_book_tops"):
                    db.execute(f"CREATE TABLE {table}(event_us INTEGER PRIMARY KEY, sequence INTEGER, exchange_time TEXT, "
                               "bid_price TEXT, bid_size TEXT, ask_price TEXT, ask_size TEXT, eligible INTEGER, dispatched INTEGER)")
                    db.execute(f"INSERT INTO {table} VALUES (1, 7, 't', '1', '2', '3', '4', 0, 0)")
            seen = []
            real = sqlite3.connect
            def spy(target, *args, **kwargs):
                seen.append((str(target), kwargs.get("uri", False)))
                return real(target, *args, **kwargs)
            with patch("sqlite3.connect", spy):
                self.assertEqual(1, len(worker.load_book_tops(5)))
                self.assertEqual(1, len(worker.load_eth_tops(5)))
            self.assertEqual(2, len(seen))
            for target, uri in seen:
                self.assertTrue(uri, target)
                self.assertIn("mode=ro", target)
