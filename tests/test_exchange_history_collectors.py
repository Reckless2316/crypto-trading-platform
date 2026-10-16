"""Offline tests for public exchange candle adapters and history boundaries."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from data.collectors.binance_collector import BinanceCollector
from data.collectors.coinbase_collector import CoinbaseCollector
from data.collectors.kraken_collector import KrakenCollector
from data.history import CandleHistoryStore, ExchangeHistory


class CollectorRequestTests(unittest.TestCase):
    @patch("data.collectors.binance_collector.requests.get")
    def test_binance_preserves_millisecond_timestamps_and_interval_close(self, get):
        response = Mock()
        response.json.return_value = [
            [1_700_000_000_000, "10", "12", "9", "11", "4"],
            [1_700_000_300_000, "11", "13", "10", "12", "5"],
        ]
        get.return_value = response

        bars = BinanceCollector().fetch_candles("BTC-USDT", "5m", 1_700_000_000_000,
                                                1_700_000_300_000)

        self.assertEqual([bar.ts_open for bar in bars], [1_700_000_000_000])
        self.assertEqual(bars[0].ts_close, 1_700_000_300_000)
        self.assertEqual(get.call_args.kwargs["params"]["endTime"], 1_700_000_299_999)

    @patch("data.collectors.coinbase_collector.requests.get")
    def test_coinbase_uses_second_inputs_filters_range_and_parses_utc(self, get):
        response = Mock()
        response.json.return_value = [
            [120, "1", "3", "2", "2.5", "7"],
            [60, "1", "2", "1", "1.5", "6"],
            [180, "2", "4", "1", "3", "8"],
        ]
        get.return_value = response

        bars = CoinbaseCollector().fetch_candles("BTC-USD", "1m", 60, 180)

        self.assertEqual([bar.ts_open for bar in bars], [60_000, 120_000])
        self.assertEqual(bars[0].ts_close, 120_000)
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["start"], "1970-01-01T00:01:00Z")
        self.assertEqual(params["end"], "1970-01-01T00:03:00Z")

    @patch("data.collectors.kraken_collector.requests.get")
    def test_kraken_uses_since_seconds_and_excludes_end(self, get):
        response = Mock()
        response.json.return_value = {"error": [], "result": {
            "XXBTZUSD": [[60, "1", "2", "1", "2", "1.5", "3", 2],
                         [120, "2", "3", "2", "3", "2.5", "4", 2]],
            "last": 120,
        }}
        get.return_value = response

        bars = KrakenCollector().fetch_candles("BTCUSDT", "1m", 60, 120)

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].ts_open, 60_000)
        self.assertEqual(bars[0].ts_close, 120_000)
        self.assertEqual(get.call_args.kwargs["params"],
                         {"pair": "XXBTZUSD", "interval": 1, "since": 60})

    def test_binance_and_kraken_reject_unknown_intervals(self):
        with self.assertRaises(ValueError):
            BinanceCollector().fetch_candles("BTCUSDT", "2m", 0)
        with self.assertRaises(ValueError):
            KrakenCollector().fetch_candles("BTCUSDT", "2m", 0)

    @patch("data.collectors.kraken_collector.requests.get")
    def test_kraken_api_errors_are_not_returned_as_empty_data(self, get):
        response = Mock()
        response.json.return_value = {"error": ["EQuery:Unknown asset pair"], "result": {}}
        get.return_value = response
        with self.assertRaisesRegex(RuntimeError, "Unknown asset pair"):
            KrakenCollector().fetch_candles("NOTAPAIR", "1m", 0, 60)


class HistoryIntegrationTests(unittest.TestCase):
    def test_second_based_provider_pagination_and_exchange_scoped_storage(self):
        class FakeKraken:
            def __init__(self):
                self.calls = []

            def fetch_candles(self, symbol, timeframe, start, end=None):
                self.calls.append((start, end))
                # History pages 700 one-minute bars and passes Unix seconds.
                return []

        fake = FakeKraken()
        service = ExchangeHistory({"kraken": fake})
        with tempfile.TemporaryDirectory() as directory:
            store = CandleHistoryStore(Path(directory) / "history.sqlite")
            # Save same key and timestamp under distinct exchanges.
            from data.collectors.collector_base import Candle
            store.save("kraken", "BTCUSDT", "1m", [Candle(1, 1, 1, 1, 1, 0, 60_000)])
            store.save("binance", "BTCUSDT", "1m", [Candle(2, 2, 2, 2, 1, 0, 60_000)])
            self.assertEqual(store.load("kraken", "BTCUSDT", "1m")[0].close, 1)
            self.assertEqual(store.load("binance", "BTCUSDT", "1m")[0].close, 2)
            service.fetch("kraken", "BTCUSDT", "1m", 0, 701 * 60_000)
        self.assertEqual(fake.calls, [(0, 42_000), (42_000, 42_060)])


if __name__ == "__main__":
    unittest.main()
