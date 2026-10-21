import tempfile
import unittest
from pathlib import Path

from data.backtesting import BacktestEngine, markov_momentum_signal
from data.collectors.collector_base import Candle, Collector
from data.history import CandleHistoryStore, ExchangeHistory


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


if __name__ == "__main__":
    unittest.main()
