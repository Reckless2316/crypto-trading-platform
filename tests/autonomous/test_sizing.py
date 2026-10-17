import unittest
from decimal import Decimal as D

from dataclasses import replace

from bot.autonomous.contracts import (
    BookPayload, EdgeEstimate, ExecutableBook, MarketRules, RiskLimits, SizedProposal, SizeRejection,
)
from bot.autonomous.sizing import kelly_loss_budget, size_entry
from tests.autonomous.support import account_view


class SizingTests(unittest.TestCase):
    def test_loss_budget_is_capped(self):
        self.assertEqual(kelly_loss_budget(D("10000"), D(".6"), D(".02"), D(".01")), D("25"))
        self.assertEqual(kelly_loss_budget(D("10000"), D(".2"), D(".02"), D(".01")), D("0"))

    def test_loss_budget_uses_quarter_kelly_below_the_per_entry_cap(self):
        self.assertEqual(kelly_loss_budget(D("10000"), D(".5001"), D(".01"), D(".01")), D(".5"))

    def test_loss_budget_handles_small_positive_payoffs_with_decimal_precision(self):
        self.assertEqual(
            kelly_loss_budget(D("10000"), D(".5001"), D("1e-30"), D("1e-30")),
            D(".5"),
        )

    def test_loss_budget_rejects_nonfinite_or_invalid_inputs(self):
        for values in (
            (D("NaN"), D(".6"), D(".02"), D(".01")),
            (D("10000"), D("Infinity"), D(".02"), D(".01")),
            (D("0"), D(".6"), D(".02"), D(".01")),
            (D("10000"), D("1.1"), D(".02"), D(".01")),
            (D("10000"), D(".6"), D("0"), D(".01")),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                kelly_loss_budget(*values)

    def test_size_entry_fails_closed_without_verified_edge_and_executable_book(self):
        edge = EdgeEstimate(.6, D(".02"), D(".01"), "net_of_all_costs", 9_000_000, "a" * 64, "b" * 64)
        market = MarketRules(
            venue="venue", instrument="AAVE-USD", base_asset="AAVE", quote_asset="USD",
            tick_size=D(".01"), lot_size=D(".001"), minimum_quantity=D(".001"),
            minimum_notional=D("1"), fee_bound=D(".001"), max_price_precision=2,
            capability_hash="c" * 64, expires_at_us=9_000_000,
            spot_allowed=True, limit_ioc_verified=True, book_at_us=1_000_000,
            available_buy_notional_usd_u=1_000_000_000,
        )
        result = size_entry(edge, account_view(), market, RiskLimits())
        self.assertIsInstance(result, SizeRejection)
        self.assertTrue(result.reason_code)

    def test_size_entry_does_not_treat_constructible_edge_as_verified(self):
        # EdgeEstimate is a shape-only record today; constructing it does not
        # authenticate a persisted forecast, release, calibration, or tail review.
        forged = EdgeEstimate(.6, D("1"), D("1"), "net_of_all_costs", 9_000_000, "a" * 64, "b" * 64)
        market = MarketRules(
            venue="venue", instrument="AAVE-USD", base_asset="AAVE", quote_asset="USD",
            tick_size=D(".01"), lot_size=D(".001"), minimum_quantity=D(".001"),
            minimum_notional=D("1"), fee_bound=D(".001"), max_price_precision=2,
            capability_hash="c" * 64, expires_at_us=9_000_000,
            spot_allowed=True, limit_ioc_verified=True,
        )
        result = size_entry(forged, account_view(), market, RiskLimits())
        self.assertIsInstance(result, SizeRejection)
        self.assertEqual(result.reason_code, "verified_edge_and_executable_book_evidence_unavailable")

    def test_size_entry_rejects_unreconciled_account(self):
        edge = EdgeEstimate(.6, D(".02"), D(".01"), "net_of_all_costs", 9_000_000, "a" * 64, "b" * 64)
        market = MarketRules(
            venue="venue", instrument="AAVE-USD", base_asset="AAVE", quote_asset="USD",
            tick_size=D(".01"), lot_size=D(".001"), minimum_quantity=D(".001"),
            minimum_notional=D("1"), fee_bound=D(".001"), max_price_precision=2,
            capability_hash="c" * 64, expires_at_us=9_000_000,
        )
        result = size_entry(edge, account_view(), market, RiskLimits())
        self.assertIsInstance(result, SizeRejection)



class VerifiedSizingTests(unittest.TestCase):
    """size_entry against a real ledger-verified edge (Task 8 gate) and an executable book."""

    def setUp(self):
        import tempfile
        from tests.autonomous.test_calibration import EdgeGateTests
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        fixture = EdgeGateTests("test_each_gate_failure_has_its_own_reason")
        self.fixture = fixture
        self.ledger, self.artifact, self.batch, self.decision, _, self.snapshot = fixture.ledger_fixture(self.tmp.name)
        self.addCleanup(self.ledger.close)
        from bot.autonomous.calibration import estimate_edge
        self.now = self.snapshot.cutoffs.candle_close_us + 1000
        self.edge = estimate_edge(self.batch, self.decision, self.snapshot, self.artifact, ledger=self.ledger, now_us=self.now)
        self.assertIsInstance(self.edge, EdgeEstimate)
        self.market = self.market_for(self.now)
        self.book = self.book_for(self.now)

    @staticmethod
    def market_for(now):
        return MarketRules(venue="venue", instrument="AAVE-USD", base_asset="AAVE", quote_asset="USD",
            tick_size=D(".01"), lot_size=D(".001"), minimum_quantity=D(".001"), minimum_notional=D("1"),
            fee_bound=D(".001"), max_price_precision=2, capability_hash="c"*64, expires_at_us=now+10**9,
            spot_allowed=True, limit_ioc_verified=True, book_at_us=now-500,
            available_buy_notional_usd_u=1_000_000_000)

    @staticmethod
    def book_for(now):
        return ExecutableBook("venue", "AAVE-USD", now-500,
            BookPayload(((D("99.95"), D("1000")),), ((D("100"), D("1000")), (D("101"), D("1000")))), "e"*64)

    def size(self, *, edge=None, account=None, market=None, book=None, now=None, limits=None):
        return size_entry(edge or self.edge, account or account_view("paper"), market or self.market,
            limits or RiskLimits(), book or self.book, now_us=self.now if now is None else now,
            edge_inputs=(self.batch, self.decision, self.snapshot, self.artifact), ledger=self.ledger)

    def test_exact_quarter_kelly_capped_by_asset_room_and_rounded_down(self):
        result = self.size()
        self.assertIsInstance(result, SizedProposal, getattr(result, "reason_code", None))
        self.assertEqual(D("100.10"), result.limit_price)
        self.assertEqual(D("4.995"), result.base_quantity)
        self.assertEqual(499_999_500, result.notional_usd_u)
        self.assertEqual(25_000_000, result.loss_budget_usd_u)
        self.assertEqual(500_000, result.max_fee_usd_u)
        self.assertEqual(self.edge.artifact_hash, result.artifact_hash)
        # Rounding never expands authorization.
        self.assertLessEqual(result.base_quantity * result.limit_price, D("500"))
        self.assertLessEqual(result.base_quantity * result.limit_price * self.edge.loss_per_dollar, D("25"))

    def test_constructed_edge_that_does_not_reproduce_is_rejected(self):
        forged = replace(self.edge, calibrated_probability=.99)
        self.assertEqual("edge_not_reproducible", self.size(edge=forged).reason_code)

    def test_guardrails_reject(self):
        wide = ExecutableBook("venue", "AAVE-USD", self.now-500,
            BookPayload(((D("99.5"), D("1000")),), ((D("100"), D("1000")),)), "e"*64)
        thin = ExecutableBook("venue", "AAVE-USD", self.now-500,
            BookPayload(((D("99.95"), D("1000")),), ((D("100"), D("0.05")),)), "e"*64)
        stale = replace(self.book, observed_at_us=self.now-RiskLimits().max_book_age_ms*1000-1)
        cases = {
            "spread_exceeds_guardrail": dict(book=wide),
            "below_venue_minimum": dict(book=thin),
            "book_stale_or_future": dict(book=stale),
            "edge_expired": dict(now=self.edge.valid_until_us),
            "venue_buy_capacity_unknown": dict(market=replace(self.market, available_buy_notional_usd_u=0)),
            "book_identity_mismatch": dict(book=replace(self.book, instrument="ETH-USD")),
            "reconciled_account_evidence_required": dict(account=replace(account_view("paper"), ready=False, exact_nav=None)),
        }
        for reason, kwargs in cases.items():
            with self.subTest(reason):
                result = self.size(**kwargs)
                self.assertIsInstance(result, SizeRejection)
                self.assertEqual(reason, result.reason_code)

    def test_reserved_pending_loss_and_existing_holding_consume_capacity(self):
        from bot.autonomous.contracts import Holding, IntentState, Reservation
        base = account_view("paper")
        reserved = replace(base, reservations=(Reservation("intent-1", 0, D("0"), 0, 100_000_000, D("0"), IntentState.RESERVED),))
        self.assertEqual("pending_loss_capacity_exhausted", self.size(account=reserved).reason_code)
        held = replace(base, holdings=(Holding("AAVE-USD", "AAVE", D("4"), 400_000_000, 400_000_000, self.now-1, "f"*64),))
        result = self.size(account=held)
        self.assertIsInstance(result, SizedProposal, getattr(result, "reason_code", None))
        self.assertLessEqual(result.notional_usd_u, 100_000_000)



class SizingCapTests(VerifiedSizingTests):
    """Each cap binds alone in its own case, so removing it changes the asserted size."""

    def notional(self, **kwargs):
        result = self.size(**kwargs)
        self.assertIsInstance(result, SizedProposal, getattr(result, "reason_code", None))
        return result.notional_usd_u

    def test_gross_cap_binds(self):
        from bot.autonomous.contracts import Holding
        held = replace(account_view("paper"), holdings=(Holding("ETH-USD", "ETH", D("1"), 1_700_000_000, 1_700_000_000, self.now-1, "f"*64),))
        # Gross room $300 < asset room $500.
        self.assertEqual(299_999_700, self.notional(account=held))  # floor(300/100.10, .001)=2.997

    def test_cash_after_fee_cap_binds(self):
        from bot.autonomous.contracts import Balance
        poor = replace(account_view("paper"), balances=(Balance("USD", D("200"), 200_000_000),))
        result = self.size(account=poor)
        self.assertLessEqual(result.notional_usd_u + result.max_fee_usd_u, 200_000_000)
        self.assertEqual(199_799_600, result.notional_usd_u)  # floor(200/1.001/100.10, .001)=1.996

    def test_venue_cap_binds(self):
        self.assertEqual(99_999_900, self.notional(market=replace(self.market, available_buy_notional_usd_u=100_000_000)))

    def test_market_expiry_tick_bound_minimum_and_fee_refuse(self):
        off_tick = ExecutableBook("venue", "AAVE-USD", self.now-500,
            BookPayload(((D("100.4"), D("1000")),), ((D("100.5"), D("1000")),)), "e"*64)
        cases = {
            "market_capability_expired": dict(market=replace(self.market, expires_at_us=self.now)),
            "tick_exceeds_impact_bound": dict(market=replace(self.market, tick_size=D("1")), book=off_tick),
            "below_venue_minimum": dict(market=replace(self.market, minimum_notional=D("1000"))),
            "fee_bound_unknown": dict(market=replace(self.market, fee_bound=D("0"))),
        }
        for reason, kwargs in cases.items():
            with self.subTest(reason):
                self.assertEqual(reason, self.size(**kwargs).reason_code)

    def test_edge_snapshot_must_match_market_and_account(self):
        hype = replace(self.market, instrument="HYPE-USD", base_asset="HYPE")
        hype_book = replace(self.book, instrument="HYPE-USD")
        self.assertEqual("book_identity_mismatch", self.size(market=hype, book=hype_book).reason_code)
        self.assertEqual("book_identity_mismatch", self.size(account=account_view("other")).reason_code)

    def test_capacity_exhausted_by_existing_asset(self):
        from bot.autonomous.contracts import Holding
        held = replace(account_view("paper"), holdings=(Holding("AAVE-USD", "AAVE", D("5"), 500_000_000, 500_000_000, self.now-1, "f"*64),))
        self.assertEqual("notional_capacity_exhausted", self.size(account=held).reason_code)

    def test_reserved_loss_covers_modeled_loss(self):
        result = self.size()
        self.assertGreaterEqual(result.loss_budget_usd_u * 1, int(D(result.notional_usd_u) * self.edge.loss_per_dollar))


class DirectionAndEdgeSizingTests(unittest.TestCase):
    def test_short_judgment_never_sized_as_buy(self):
        import tempfile
        from bot.autonomous.contracts import Direction
        from tests.autonomous.test_calibration import EdgeGateTests
        with tempfile.TemporaryDirectory() as directory:
            fixture = EdgeGateTests("test_each_gate_failure_has_its_own_reason")
            ledger, artifact, batch, decision, _, snapshot = fixture.ledger_fixture(directory, live_direction=Direction.SHORT)
            try:
                forged = EdgeEstimate(.62, D(".04"), D(".02"), "net_of_all_costs", fixture.now+10, artifact.artifact_hash, "b"*64)
                result = size_entry(forged, account_view("paper"), VerifiedSizingTests.market_for(fixture.now), RiskLimits(),
                    VerifiedSizingTests.book_for(fixture.now), now_us=fixture.now, edge_inputs=(batch, decision, snapshot, artifact), ledger=ledger)
                self.assertEqual("spot_entry_requires_long", result.reason_code)
            finally:ledger.close()

    def test_no_positive_kelly_edge(self):
        import tempfile
        from bot.autonomous.calibration import estimate_edge
        from tests.autonomous.test_calibration import EdgeGateTests
        with tempfile.TemporaryDirectory() as directory:
            fixture = EdgeGateTests("test_each_gate_failure_has_its_own_reason")
            ledger, artifact, batch, decision, _, snapshot = fixture.ledger_fixture(directory, win_exit=D("101"))
            try:
                edge = estimate_edge(batch, decision, snapshot, artifact, ledger=ledger, now_us=fixture.now)
                result = size_entry(edge, account_view("paper"), VerifiedSizingTests.market_for(fixture.now), RiskLimits(),
                    VerifiedSizingTests.book_for(fixture.now), now_us=fixture.now, edge_inputs=(batch, decision, snapshot, artifact), ledger=ledger)
                self.assertEqual("no_positive_kelly_edge", result.reason_code)
            finally:ledger.close()


if __name__ == "__main__":
    unittest.main()
