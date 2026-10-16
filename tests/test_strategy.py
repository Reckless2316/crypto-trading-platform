import unittest

from prediction.markov_prototype import CryptoRegimeHMM
from strategies.signal_to_bot import generate_signal


class StrategyTests(unittest.TestCase):
    def test_ema_crossover_detects_upward_cross(self):
        closes=[100-i*.5 for i in range(40)]+[80,88,96,104,112]
        signal=generate_signal("ema_12_26_crossover","BTC-USD",closes)
        self.assertEqual(signal["direction"],"long")

    def test_unsupported_strategy_fails_closed(self):
        with self.assertRaises(ValueError):
            generate_signal("bb_mean_revert","BTC-USD",[100]*30)

    def test_viterbi_returns_one_state_per_observation(self):
        observations=[.05,.07,.04,.06,-.04,-.08]
        path=CryptoRegimeHMM().viterbi(observations)
        self.assertEqual(len(path),len(observations))
        self.assertTrue(set(path).issubset({"L","HU","HD"}))


if __name__ == "__main__":
    unittest.main()
