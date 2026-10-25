import tempfile
import math
import random
import time
import unittest
from pathlib import Path

from data.backtesting import BacktestEngine, _ema, ema_crossover_signal, markov_momentum_signal
from data.collectors.collector_base import Candle, Collector
from data.history import CandleHistoryStore, ExchangeHistory
from strategies.strategy_suite import (
    STRATEGY_CATALOG,
    IncrementalBacktestSignal,
    generate_signal,
)


def candle(ts, price):
    return Candle(price, price, price, price, 1.0, ts, ts + 60_000)


class FakeCollector(Collector):
    def __init__(self):
        super().__init__("fake", {})
        self.calls = []

    def fetch_candles(self, symbol, timeframe, start, end=None):
        self.calls.append((start, end))
        # This fake follows the Binance collector's millisecond convention.
        return [candle(ts, 10) for ts in range(start, end, 60_000)]


class HistoryTests(unittest.TestCase):
    def test_sqlite_key_includes_exchange_and_filters_times(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CandleHistoryStore(Path(directory) / "history.sqlite")
            sample = [candle(60_000, 10), candle(120_000, 11)]
            store.save("binance", "BTCUSDT", "1m", sample)
            store.save("coinbase", "BTCUSDT", "1m", [candle(60_000, 12)])
            self.assertEqual([x.close for x in store.load("binance", "BTCUSDT", "1m")], [10, 11])
            self.assertEqual([x.close for x in store.load("coinbase", "BTCUSDT", "1m")], [12])
            self.assertEqual([x.ts_open for x in store.load("binance", "BTCUSDT", "1m", 120_000, 180_000)], [120_000])
            self.assertEqual([x.close for x in store.load("binance", "BTCUSDT", "1m", limit=1)], [11])
            self.assertEqual(store.load("binance", "BTCUSDT", "1m", limit=5), store.load("binance", "BTCUSDT", "1m"))
            self.assertEqual(store.load("binance", "BTCUSDT", "1m", limit=0), [])

    def test_fetch_paginates_and_persists(self):
        fake = FakeCollector()
        with tempfile.TemporaryDirectory() as directory:
            store = CandleHistoryStore(Path(directory) / "history.sqlite")
            service = ExchangeHistory({"binance": fake})
            out = service.fetch("binance", "BTCUSDT", "1m", 0, 120_000, store)
            self.assertEqual(len(out), 2)
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(store.load("binance", "BTCUSDT", "1m"), out)

    def test_unsupported_exchange_symbol_and_timeframe_fail_closed(self):
        service = ExchangeHistory({"binance": FakeCollector()})
        with self.assertRaises(ValueError):
            service.fetch("unknown", "BTCUSDT", "1m", 0, 60_000)
        with self.assertRaises(ValueError):
            service.fetch("binance", "BTCUSDT", "2m", 0, 60_000)
        with self.assertRaises(ValueError):
            service.fetch("binance", "../", "1m", 0, 60_000)


class BacktestTests(unittest.TestCase):
    def test_next_bar_execution_and_round_trip_fees(self):
        bars = [candle(i * 60_000, p) for i, p in enumerate((10, 10, 20, 20))]
        # Decision from first closed candle buys at bar 1 open; next decision sells at bar 2 open.
        outputs = iter((True, False, False))
        result = BacktestEngine(100, fee_rate=0.01).run(bars, lambda history: next(outputs))
        self.assertEqual(result.trade_count, 1)
        trade = result.trades[0]
        self.assertEqual((trade.entry_ts, trade.exit_ts), (60_000, 120_000))
        self.assertEqual((trade.entry_price, trade.exit_price), (10, 20))
        self.assertGreater(trade.fees, 0)
        self.assertAlmostEqual(result.final_equity, 100 * 20 / 10 * (1 - 0.01) / (1 + 0.01))

    def test_empty_or_bad_order_data_rejected_and_markov_signal_defined(self):
        with self.assertRaises(ValueError):
            BacktestEngine().run([], lambda _history: False)
        with self.assertRaises(ValueError):
            BacktestEngine().run([candle(60_000, 1), candle(60_000, 2)], lambda _history: False)
        self.assertFalse(markov_momentum_signal()( [candle(0, 1)] ))

    def test_signals_match_full_history_reference_and_prefix_is_a_view(self):
        prices = [10 + ((i * 7) % 13) + (i % 5) * 0.5 for i in range(150)]
        bars = [candle(i * 60_000, p) for i, p in enumerate(prices)]

        def ref_markov(history, lookback=20, threshold=0.5):
            changes = [history[i].close >= history[i - 1].close for i in range(1, len(history))][-lookback:]
            if len(changes) < 2:
                return False
            current = changes[-1]
            following = [changes[i + 1] for i in range(len(changes) - 1) if changes[i] == current]
            p = sum(following) / len(following) if following else sum(changes) / len(changes)
            return p > threshold

        def ref_ema(history, fast_p=5, slow_p=12):
            if len(history) < slow_p:
                return False
            closes = [c.close for c in history]
            return _ema(closes[-max(slow_p * 3, fast_p):], fast_p) > _ema(closes[-slow_p * 3:], slow_p)

        seen = []
        markov, ema = markov_momentum_signal(20), ema_crossover_signal(5, 12)

        def probe(history):
            prefix = tuple(history)
            seen.append(len(prefix))
            self.assertEqual(history[-3:], prefix[-3:])
            self.assertEqual(history[-1], prefix[-1])
            self.assertEqual(markov(history), ref_markov(prefix))
            self.assertEqual(ema(history), ref_ema(prefix))
            return len(prefix) % 3 != 0

        BacktestEngine().run(bars, probe)
        self.assertEqual(seen, list(range(1, len(bars))))

    def test_incremental_catalog_signals_match_full_prefix_backtests(self):
        smooth = []
        for index in range(3000):
            price = 100 + index * 0.02 + 3 * math.sin(index * 0.09) + 5 * math.sin(index * 0.017)
            smooth.append(Candle(price, price + 1, price - 1, price, 1, index * 60_000, (index + 1) * 60_000))
        # A seeded high-volatility random walk with uneven ranges exercises the Markov and Elliott
        # branches that a smooth series rarely reaches.
        rng, price, walk = random.Random(7), 100.0, []
        for index in range(1200):
            opened, price = price, price * (1 + rng.gauss(0, 0.03))
            high, low = max(opened, price) * (1 + abs(rng.gauss(0, 0.01))), min(opened, price) * (1 - abs(rng.gauss(0, 0.01)))
            walk.append(Candle(opened, high, low, price, 1, index * 60_000, (index + 1) * 60_000))
        self._assert_incremental_matches(smooth)
        self._assert_incremental_matches(walk)

    def _assert_incremental_matches(self, bars):
        for entry in STRATEGY_CATALOG:
            strategy = entry["name"]

            def full_prefix_signal(prefix):
                try:
                    signal = generate_signal(strategy, "BTC-USD",
                        [float(c.close) for c in prefix],
                        highs=[float(c.high) for c in prefix],
                        lows=[float(c.low) for c in prefix])
                    return signal["direction"] == "long"
                except (ValueError, TypeError):
                    return False

            incremental = IncrementalBacktestSignal(strategy, "BTC-USD")
            optimized = BacktestEngine().run(
                bars, lambda prefix: incremental.update(prefix[-1].close, prefix[-1].high, prefix[-1].low))
            reference = BacktestEngine().run(bars, full_prefix_signal)
            self.assertEqual(optimized, reference, strategy)

    def test_twenty_thousand_candle_ema_backtest_is_bounded(self):
        bars = [candle(index * 60_000, 100 + index * 0.001 + math.sin(index / 20))
                for index in range(20_000)]
        signal = IncrementalBacktestSignal("ema_12_26_crossover", "BTC-USD")

        started = time.perf_counter()
        BacktestEngine().run(
            bars, lambda prefix: signal.update(prefix[-1].close, prefix[-1].high, prefix[-1].low))
        self.assertLess(time.perf_counter() - started, 10)


if __name__ == "__main__":
    unittest.main()
