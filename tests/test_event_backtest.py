import random
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from data.binance_archive import Series, candles, import_to_store, ARCHIVE_EXCHANGE
from data.event_backtest import Bracket, CostModel, simulate_bracket
from data.history import CandleHistoryStore
from research.trade_stats import clustered_ci, concentration, mean_r, random_subset_test

BAR = 60_000
FREE = CostModel(0.0, 0.0, 0.0)


@dataclass
class Bars:
    ts: list
    open: list
    high: list
    low: list
    close: list


def bars(rows):
    """rows of (open, high, low, close), one minute apart."""
    return Bars([i * BAR for i in range(len(rows))], *[list(col) for col in zip(*rows)])


class BracketTests(unittest.TestCase):
    def test_long_target_pays_maker_and_reports_r(self):
        b = bars([(100, 100, 100, 100), (100, 101, 99.5, 100.5), (100.5, 103, 100, 102)])
        costs = CostModel(taker_fee=0.001, maker_fee=0.0, slippage=0.0)
        fill = simulate_bracket(b, Bracket("long", 1, stop=99, target=102.5), costs, BAR)
        self.assertEqual(fill.exit_reason, "target")
        self.assertEqual(fill.exit_ts, 3 * BAR)
        self.assertAlmostEqual(fill.gross_r, 2.5)
        self.assertAlmostEqual(fill.net_r, 2.5 - 0.1)

    def test_short_stop_and_gap_through_fills_at_open(self):
        b = bars([(100, 100, 100, 100), (100, 100.5, 99, 99.5), (103, 104, 102, 103)])
        fill = simulate_bracket(b, Bracket("short", 1, stop=101, target=95), FREE, BAR)
        self.assertEqual(fill.exit_reason, "stop")
        self.assertEqual(fill.exit_price, 103)   # gapped past 101, so the stop fills at the open
        self.assertAlmostEqual(fill.net_r, -3.0)

    def test_stop_is_assumed_first_when_a_bar_spans_both(self):
        b = bars([(100, 100, 100, 100), (100, 110, 90, 100)])
        self.assertEqual(simulate_bracket(b, Bracket("long", 1, 95, 105), FREE, BAR).exit_reason, "stop")

    def test_slippage_is_inside_fill_prices(self):
        b = bars([(100, 100, 100, 100), (100, 100, 94, 95)])
        fill = simulate_bracket(b, Bracket("long", 1, 95, 110), CostModel(0, 0, 0.01), BAR)
        self.assertAlmostEqual(fill.entry, 101)
        self.assertAlmostEqual(fill.exit_price, 95 * 0.99)

    def test_time_stop_only_when_one_r_never_touched(self):
        flat = [(100, 100.5, 99.8, 100)] * 6
        fill = simulate_bracket(bars([(100,) * 4] + flat), Bracket("long", 1, 99, 104, time_stop_bars=3), FREE, BAR)
        self.assertEqual((fill.exit_reason, fill.exit_ts), ("time_stop", 4 * BAR))
        touched = [(100, 101.2, 99.8, 100)] + flat
        fill = simulate_bracket(bars([(100,) * 4] + touched), Bracket("long", 1, 99, 104, time_stop_bars=3, max_hold_bars=5),
                                FREE, BAR)
        self.assertEqual(fill.exit_reason, "max_hold")

    def test_data_gap_closes_the_trade(self):
        b = bars([(100,) * 4, (100, 100.5, 99.8, 100), (100, 100.5, 99.8, 100)])
        b.ts[2] += BAR
        self.assertEqual(simulate_bracket(b, Bracket("long", 1, 99, 104), FREE, BAR).exit_reason, "data_gap")

    def test_funding_longs_pay_positive_rates_shorts_receive(self):
        b = bars([(100,) * 4, (100, 100.5, 99.8, 100), (100, 103, 99.8, 102)])
        paid = lambda start, end: 0.001  # noqa: E731
        long = simulate_bracket(b, Bracket("long", 1, 99, 102.5), FREE, BAR, paid)
        short = simulate_bracket(b, Bracket("short", 1, 101, 97), FREE, BAR, paid)
        self.assertAlmostEqual(long.funding_r, -0.1)
        self.assertAlmostEqual(short.funding_r, 0.1)

    def test_rejects_brackets_that_cannot_exist(self):
        b = bars([(100,) * 4, (100,) * 4])
        with self.assertRaises(ValueError):
            simulate_bracket(b, Bracket("long", 1, 101, 105), FREE, BAR)
        with self.assertRaises(ValueError):
            simulate_bracket(b, Bracket("long", 1, 99, 99.5 - 1), FREE, BAR)


@dataclass
class T:
    net_r: float
    entry_ts: int
    symbol: str = "X"


class TradeStatsTests(unittest.TestCase):
    def test_random_subset_test_detects_a_real_filter_and_not_a_random_one(self):
        rng = random.Random(1)
        base = [T(rng.gauss(0, 1), i * 86_400_000) for i in range(400)]
        best = sorted(base, key=lambda t: t.net_r)[-40:]
        self.assertLess(random_subset_test(base, best, rng, reps=2000, ci_reps=200)["perm_p"], 0.01)
        chance = rng.sample(base, 40)
        self.assertGreater(random_subset_test(base, chance, rng, reps=2000, ci_reps=200)["perm_p"], 0.01)

    def test_clustered_ci_brackets_the_mean(self):
        rng = random.Random(2)
        trades = [T(rng.gauss(0.5, 1), (i // 3) * 86_400_000) for i in range(300)]
        lo, hi = clustered_ci(trades, mean_r, rng, reps=500)
        self.assertLess(lo, mean_r(trades))
        self.assertGreater(hi, mean_r(trades))

    def test_concentration_share(self):
        trades = [T(3, 0, "A"), T(1, 0, "B"), T(-1, 0, "B")]
        out = concentration(trades, {"symbol": lambda t: t.symbol})
        self.assertEqual((out["top_symbol"], out["top_symbol_share"]), ("A", 1.0))


class ArchiveImportTests(unittest.TestCase):
    def test_aggregates_complete_buckets_and_stores_them(self):
        ts = [h * 3_600_000 + k * 300_000 for h in range(3) for k in range(12)][:-1]  # last hour incomplete
        px = [100.0 + i for i in range(len(ts))]
        s = Series("BTCUSDT", ts, px, [p + 1 for p in px], [p - 1 for p in px], [p + 0.5 for p in px], [], [], [], [])
        hourly = candles(s, "1h")
        self.assertEqual([c.ts_open for c in hourly], [0, 3_600_000])
        self.assertEqual((hourly[0].open, hourly[0].high, hourly[0].low, hourly[0].close), (100.0, 112.0, 99.0, 111.5))
        with tempfile.TemporaryDirectory() as directory:
            import data.binance_archive as archive
            original = archive.load
            archive.load = lambda symbol, cache: s
            try:
                n = import_to_store("BTCUSDT", "1h", Path(directory) / "h.sqlite3")
            finally:
                archive.load = original
            self.assertEqual(n, 2)
            stored = CandleHistoryStore(Path(directory) / "h.sqlite3").load(ARCHIVE_EXCHANGE, "BTCUSDT", "1h")
            self.assertEqual([c.close for c in stored], [111.5, 123.5])


if __name__ == "__main__":
    unittest.main()
