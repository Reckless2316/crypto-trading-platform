import random
import unittest

from data.binance_archive import Series
from research.fade_flush.strategy import (DAY, H1, M15, FundingIndex, Params, build_bars, detect,
                                          simulate, wilder_atr)

T0 = 1_640_995_200_000  # 2022-01-01 00:00 UTC
DAYS = 20
SWEEP_DAY = DAYS - 1


def series_from_15m(bars15, oi=None, funding=None, symbol="TESTUSDT"):
    """Each 15m bar becomes three 5m bars whose aggregate is exactly the 15m bar."""
    ts, o, h, l, c = [], [], [], [], []
    for t, (bo, bh, bl, bc) in bars15:
        for k, sub in enumerate(((bo, bh, bl, bc), (bc,) * 4, (bc,) * 4)):
            ts.append(t + k * 300_000)
            o.append(sub[0]); h.append(sub[1]); l.append(sub[2]); c.append(sub[3])
    oi = oi or []
    funding = funding or []
    return Series(symbol, ts, o, h, l, c, [x[0] for x in oi], [x[1] for x in oi],
                  [x[0] for x in funding], [x[1] for x in funding])


def scenario(pierce_low=94.5, reclaim_close=96.0, after=None, oi_drop=0.0, funding_spike=0.0):
    """Flat 100 +/- 1 market (ATR 2). The day before the sweep breaks down to 95.5 at 06:00 and up
    to 104.5 at 10:00, both more than 1.5 ATR beyond the range so neither is a sweep itself. The
    sweep day then pierces 95.5 at 20:00 and closes back above it in the same bar."""
    bars = []
    pierce_ts = T0 + SWEEP_DAY * DAY + 20 * H1
    after = after or [(96.0, 105.0, 95.9, 104.8)]
    for t in range(T0, pierce_ts + (1 + len(after)) * M15 + 4 * H1, M15):
        bar = (100.0, 101.0, 99.0, 100.0)
        if t == T0 + (SWEEP_DAY - 1) * DAY + 6 * H1:
            bar = (99.0, 99.5, 95.5, 96.0)
        if t == T0 + (SWEEP_DAY - 1) * DAY + 10 * H1:
            bar = (101.0, 104.5, 100.5, 104.0)
        if t == pierce_ts:
            bar = (100.0, 101.0, pierce_low, reclaim_close)
        k = (t - pierce_ts) // M15 - 1
        if 0 <= k < len(after):
            bar = after[k]
        bars.append((t, bar))
    oi, value = [], 1000.0
    for n, t in enumerate(range(T0, pierce_ts + 30 * DAY // 30, 300_000)):
        value *= 1.001 if n % 2 else 0.999
        if t == pierce_ts + M15:
            value *= 1 - oi_drop
        oi.append((t, value))
    funding = []
    for t in range(T0, pierce_ts + DAY, 8 * H1):
        rate = 0.0001 + (funding_spike if t >= pierce_ts - DAY else 0.0) + (t // (8 * H1) % 3) * 1e-6
        funding.append((t, rate))
    return series_from_15m(bars, oi, funding), pierce_ts


class SweepDetectionTests(unittest.TestCase):
    def test_pierce_and_reclaim_fires_one_long(self):
        s, pierce_ts = scenario()
        signals, _ = detect(s)
        self.assertEqual([(x.side, x.level, x.signal_ts) for x in signals], [("long", 95.5, pierce_ts + M15)])
        sig = signals[0]
        self.assertAlmostEqual(sig.sweep_extreme, 94.5)
        self.assertAlmostEqual(sig.atr, 2.0, places=3)
        self.assertAlmostEqual(sig.pen_atr, 0.5, places=3)
        self.assertIn(104.5, sig.targets)

    def test_too_deep_is_a_breakdown_not_a_sweep(self):
        s, _ = scenario(pierce_low=91.5)   # 2 ATR
        self.assertEqual(detect(s)[0], [])

    def test_too_shallow_is_ignored(self):
        s, _ = scenario(pierce_low=95.3)   # 0.1 ATR
        self.assertEqual(detect(s)[0], [])

    def test_no_reclaim_within_three_bars(self):
        s, _ = scenario(reclaim_close=95.0, after=[(95.0, 95.4, 94.8, 95.1)] * 3)
        self.assertEqual(detect(s)[0], [])

    def test_reclaim_on_third_bar_counts(self):
        s, pierce_ts = scenario(reclaim_close=95.0,
                                after=[(95.0, 95.4, 94.8, 95.1), (95.1, 96.1, 94.9, 96.0), (96.0, 105, 95.9, 104.8)])
        signals, _ = detect(s)
        self.assertEqual([(x.signal_ts, x.sweep_bars) for x in signals], [(pierce_ts + 3 * M15, 3)])

    def test_flush_and_crowding_flags(self):
        quiet, _ = scenario()
        sig = detect(quiet)[0][0]
        self.assertFalse(sig.flush)
        self.assertFalse(sig.crowd)
        loud, _ = scenario(oi_drop=0.03, funding_spike=0.0004)
        sig = detect(loud)[0][0]
        self.assertTrue(sig.flush)
        self.assertLess(sig.oi_z, -2)
        self.assertTrue(sig.crowd)
        self.assertGreater(sig.f24, 0)

    def test_detection_is_causal(self):
        """Signals found on a prefix of the data are exactly the full run's signals up to that point."""
        rng = random.Random(7)
        bars, price = [], 100.0
        for n in range(20 * 96):
            o = price
            price *= 1 + rng.gauss(0, 0.004)
            bars.append((T0 + n * M15, (o, max(o, price) * (1 + abs(rng.gauss(0, 0.003))),
                                        min(o, price) * (1 - abs(rng.gauss(0, 0.003))), price)))
        full = series_from_15m(bars)
        full_signals, _ = detect(full)
        self.assertGreater(len(full_signals), 3)
        for cut in range(2 * 96, len(bars), 97):
            prefix = series_from_15m(bars[:cut])
            got = [(x.side, x.signal_ts, x.level, x.targets) for x in detect(prefix)[0]]
            want = [(x.side, x.signal_ts, x.level, x.targets) for x in full_signals
                    if x.bar_index < cut]
            self.assertEqual(got, want, cut)


class SimulationTests(unittest.TestCase):
    def run_trade(self, s):
        signals, b15 = detect(s)
        return simulate(signals[0], b15, FundingIndex(s, Params()), Params())

    def test_target_hit(self):
        s, _ = scenario()
        trade = self.run_trade(s)
        self.assertEqual(trade.exit_reason, "target")
        entry = 96.0 * 1.0005
        risk = entry - (94.5 - 0.3)
        self.assertAlmostEqual(trade.entry, entry)
        risk = trade.entry - trade.stop   # ATR is 2.0 only to ~1e-5 after the breakdown bars
        self.assertAlmostEqual(trade.target, 104.5)
        self.assertAlmostEqual(trade.gross_r, (104.5 - entry) / risk)
        self.assertAlmostEqual(trade.net_r, (104.5 - entry - 0.0005 * entry - 0.0002 * 104.5) / risk)

    def test_stop_assumed_first_when_bar_spans_both(self):
        s, _ = scenario(after=[(96.0, 105.0, 93.0, 100.0)])
        trade = self.run_trade(s)
        self.assertEqual(trade.exit_reason, "stop")
        self.assertLess(trade.net_r, -1)

    def test_time_stop_when_one_r_never_touched(self):
        s, _ = scenario(after=[(96.0, 96.5, 95.8, 96.1)] * 14)
        trade = self.run_trade(s)
        self.assertEqual(trade.exit_reason, "time_stop")
        self.assertEqual(trade.exit_ts - trade.entry_ts, 12 * M15)

    def test_target_below_2r_is_skipped(self):
        # Reclaim far above the level: entry 101.05, risk 6.85, target 104.5 only 0.5R away.
        s, _ = scenario(reclaim_close=101.0, after=[(101.0, 101.5, 100.5, 101.0)])
        signals, b15 = detect(s)
        self.assertEqual(simulate(signals[0], b15, FundingIndex(s, Params()), Params()), "target_below_2r")


class AtrTests(unittest.TestCase):
    def test_wilder_seed_and_smoothing(self):
        s = series_from_15m([(T0 + n * M15, (100.0, 101.0, 99.0, 100.0)) for n in range(20)])
        b15 = build_bars(s)[0]
        atr = wilder_atr(b15, 14)
        self.assertTrue(all(x != x for x in atr[:13]))
        self.assertAlmostEqual(atr[13], 2.0)
        self.assertAlmostEqual(atr[19], 2.0)


if __name__ == "__main__":
    unittest.main()
