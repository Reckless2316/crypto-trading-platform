import unittest

from research.autonomous.status_cards import candidate_cards


class ResearchCardTests(unittest.TestCase):
    def test_six_candidates_stay_ineligible(self):
        payload = candidate_cards()
        self.assertEqual(payload["qualified_finalists"], 0)
        self.assertFalse(payload["regime_valid"])
        self.assertEqual(len(payload["candidates"]), 6)
        self.assertTrue(all(card["eligible"] is False for card in payload["candidates"]))


if __name__ == "__main__":
    unittest.main()
