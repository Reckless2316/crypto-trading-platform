"""Conservative terminal proof requirements for releasing reserved capacity."""
import unittest
from decimal import Decimal

from bot.autonomous import execution
from tests.autonomous import test_execution_recovery
from bot.autonomous.contracts import ContractViolation


class ReservationReleaseTests(unittest.TestCase):
    def test_only_final_fully_accounted_terminal_orders_can_release(self):
        release = getattr(execution, "reservation_releasable", None)
        self.assertTrue(callable(release), "terminal reconciliation proof gate is missing")
        for terminal, filled, accounted, fees, expected in (
            (True, Decimal("2"), Decimal("1"), True, False),
            (True, Decimal("2"), Decimal("2"), False, False),
            (False, Decimal("0"), Decimal("0"), True, False),
            (True, Decimal("2"), Decimal("2"), True, True),
            (True, Decimal("0"), Decimal("0"), True, True),
        ):
            with self.subTest(terminal=terminal, filled=filled, accounted=accounted, fees=fees):
                self.assertIs(expected, release(terminal, filled, accounted, fees))

    def test_invalid_or_ambiguous_proofs_keep_capacity(self):
        release = getattr(execution, "reservation_releasable", None)
        self.assertTrue(callable(release), "terminal reconciliation proof gate is missing")
        for terminal, filled, accounted, fees in (
            (1, Decimal("0"), Decimal("0"), True),
            (True, Decimal("0"), Decimal("0"), 1),
            (True, Decimal("NaN"), Decimal("0"), True),
            (True, Decimal("Infinity"), Decimal("Infinity"), True),
            (True, Decimal("-1"), Decimal("-1"), True),
            (True, 0, Decimal("0"), True),
            (True, Decimal("0"), None, True),
        ):
            with self.subTest(filled=filled, accounted=accounted):
                self.assertFalse(release(terminal, filled, accounted, fees))


class TerminalSettlementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_execution_recovery.AuthorityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def settle(self, evidence, intent):
        method = getattr(self.fixture.ledger, "settle_reconciled_intent", None)
        self.assertTrue(callable(method), "durable terminal settlement is missing")
        return method(evidence, intent)

    def test_terminal_status_retains_capacity_until_fresh_account_reconciliation(self):
        x = self.fixture
        x.prepare(asks=((Decimal("100"), Decimal("5")),))
        receipt = x.submit()
        self.assertGreater(x.ledger.account_view("paper-test").reservations[0].cash_usd_u, 0)
        with self.assertRaises(ContractViolation):
            self.settle("opening", receipt.intent_id)
        x.mark("settlement", books={"AAVE": {"at_us": x.now, "quote_asset": "USD",
            "bids": [["99.95", "100"]]}})
        self.assertTrue(self.settle("settlement", receipt.intent_id))
        self.assertFalse(self.settle("settlement", receipt.intent_id))
        self.assertEqual((), x.ledger.account_view("paper-test").reservations)
        x.ledger.close()
        x.ledger = test_execution_recovery.test_ledger.load_ledger().open(x.path)
        self.assertEqual((0, 0, 0, "0", "0"), tuple(x.ledger.connection.execute(
            "SELECT cash_usd_u,gross_usd_u,loss_budget_usd_u,base_qty,remaining_qty FROM reservations").fetchone()))

    def test_canceled_unfilled_order_can_release_after_reconciliation(self):
        x = self.fixture
        x.prepare(asks=((Decimal("100.5"), Decimal("10")),))
        receipt = x.submit()
        x.mark("nonfill-settlement")
        self.assertTrue(self.settle("nonfill-settlement", receipt.intent_id))
        self.assertEqual((), x.ledger.account_view("paper-test").reservations)

    def test_unknown_order_never_releases_even_after_matching_balance_mark(self):
        x = self.fixture
        x.prepare(error=TimeoutError("acceptance unknown"))
        receipt = x.submit()
        x.mark("unknown-settlement")
        with self.assertRaises(ContractViolation):
            self.settle("unknown-settlement", receipt.intent_id)
        self.assertGreater(x.ledger.connection.execute("SELECT cash_usd_u FROM reservations").fetchone()[0], 0)

    def test_authority_reconciles_through_its_own_source_and_mutex(self):
        from bot.autonomous.contracts import ReconcileReason
        x = self.fixture
        x.prepare(asks=((Decimal("100.5"), Decimal("10")),))
        x.submit()
        def source(account):
            self.assertTrue(x.authority._lock(account).locked())
            return x.evidence("authority-reconcile", "mark", {
                "balances": x.balances(), "books": {}, "heartbeat_at_us": x.now,
                "open_intent_ids": [], "liabilities_usd_u": 0, "exit_cost_usd_u": 0})
        x.authority.reconciliation_source = source
        method = getattr(x.authority, "reconcile", None)
        self.assertTrue(callable(method), "authority reconciliation is missing")
        report = method("paper-test", ReconcileReason.OPERATOR)
        self.assertTrue(report.ready, report.differences)
        self.assertEqual((), x.ledger.account_view("paper-test").reservations)

    def test_missing_reconciliation_source_disables_readiness(self):
        x = self.fixture
        x.prepare()
        method = getattr(x.authority, "reconcile", None)
        self.assertTrue(callable(method), "authority reconciliation is missing")
        report = method("paper-test")
        self.assertFalse(report.ready)
        self.assertFalse(x.ledger.account_view("paper-test").ready)
