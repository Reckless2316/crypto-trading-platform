import unittest

from strategies.strategy_suite import (
    STRATEGY_CATALOG,
    MarketData,
    bollinger_mean_reversion,
    donchian_breakout,
    elliott_wave_heuristic,
    generate_signal,
    macd_crossover,
    markov_regime,
    rsi_reversal,
)


class StrategySuiteTests(unittest.TestCase):
    def test_catalog_and_common_signal_contract(self):
        names = {item["name"] for item in STRATEGY_CATALOG}
        self.assertTrue({"ema_12_26_crossover", "markov_regime", "elliott_wave_heuristic"} <= names)
        signal = generate_signal("ema_12_26_crossover", "BTC-USD", [100.0] * 30)
        self.assertEqual(signal["direction"], "neutral")
        self.assertEqual(signal["symbol"], "BTC-USD")
        for key in ("strength", "reference_price", "reason", "generated_at"):
            self.assertIn(key, signal)

    def test_bollinger_and_rsi_signals(self):
        bb = bollinger_mean_reversion(MarketData("BTC-USD", [100.0] * 19 + [70.0]))
        self.assertEqual(bb["direction"], "long")
        self.assertLess(bb["lower_band"], bb["middle_band"])
        rsi = rsi_reversal(MarketData("BTC-USD", list(range(110, 95, -1))))
        self.assertEqual(rsi["direction"], "long")
        self.assertEqual(rsi["rsi"], 0)

    def test_donchian_breakout_uses_prior_highs(self):
        signal = donchian_breakout(MarketData(
            "ETH-USD", [10.0, 11.0, 12.0], highs=[10.5, 11.5, 12.2], lows=[9.5, 10.5, 11.5]),
            period=2)
        self.assertEqual(signal["direction"], "long")
        self.assertEqual(signal["channel_high"], 11.5)

    def test_macd_signal_contract_and_markov_explanation(self):
        prices = [100 + i * 0.2 for i in range(50)] + [110, 107, 105, 103, 100]
        macd = macd_crossover(MarketData("BTC-USD", prices))
        self.assertIn(macd["direction"], {"long", "exit", "neutral"})
        markov = markov_regime(MarketData("BTC-USD", prices))
        self.assertIn(markov["direction"], {"long", "exit", "neutral"})
        self.assertIn("next_state_probabilities", markov)
        self.assertIn("not calibrated", markov["caveat"])

    def test_elliott_output_is_explicitly_heuristic(self):
        signal = elliott_wave_heuristic(MarketData("BTC-USD", [100, 101, 100.5, 101.5, 100.8, 102, 101]))
        self.assertTrue(signal["heuristic"])
        self.assertIn("subjective", signal["caveat"])
        self.assertLessEqual(signal["strength"], 0.2)

    def test_invalid_input_and_unknown_strategy_fail_closed(self):
        with self.assertRaises(ValueError):
            generate_signal("ema_12_26_crossover", "BTC-USD", [100, float("nan")])
        with self.assertRaises(ValueError):
            generate_signal("does_not_exist", "BTC-USD", [100])


if __name__ == "__main__":
    unittest.main()
