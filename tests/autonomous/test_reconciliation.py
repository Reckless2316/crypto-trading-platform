"""Conservative terminal proof requirements for releasing reserved capacity."""
import unittest
from decimal import Decimal

from bot.autonomous import execution


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
