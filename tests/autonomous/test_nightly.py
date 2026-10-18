import unittest

from bot.autonomous.brain import BrainProposal, DeterministicBrain, UnavailableBrain
from bot.autonomous.nightly import (
    ReviewHistory, ReviewScheduler, SessionEvent, SessionView, entries_blocked,
    label_mature, next_review_us, review_session,
)

HASH = "a" * 64


def event(event_id, kind="fill", maturity=100, probability=0.8, outcome=1, realized=10, hypothetical=12):
    return SessionEvent(event_id, kind, maturity, probability, outcome, realized, hypothetical)


def view(events, cutoff=100):
    return SessionView("paper-test", cutoff, HASH, tuple(events))


class NightlyTests(unittest.TestCase):
    def test_pending_outcome_is_not_a_loss(self):
        self.assertFalse(label_mature(101, 100))
        self.assertTrue(label_mature(100, 100))

    def test_immature_label_is_excluded_from_brier_denominator(self):
        report = review_session(view((
            event("resolved", outcome=1, probability=1.0),
            event("future", maturity=101, outcome=0, probability=0.0),
            event("unlabeled", maturity=50, outcome=None, probability=0.2),
        )), DeterministicBrain(), job_id="job-1")
        self.assertEqual(report.resolved, 1)
        self.assertEqual(report.pending, 2)
        self.assertEqual(report.binary_brier, 0.0)
        self.assertEqual(report.included_ids, ("resolved",))
        self.assertFalse(report.approved)

    def test_no_trade_night_has_metrics_and_no_proposal_approval(self):
        report = review_session(view(()), DeterministicBrain(), job_id="quiet")
        self.assertIsNone(report.binary_brier)
        self.assertEqual(report.resolved, 0)
        self.assertEqual(report.counts, tuple((kind, 0) for kind in (
            "error", "fill", "hold", "miss", "nonfill", "partial", "rejection", "veto")))
        self.assertEqual(report.status, "metrics_ready")
        self.assertEqual(report.brain_action, "no_change")
        self.assertFalse(report.approved)

    def test_fills_and_misses_are_counted_separately_from_brier(self):
        report = review_session(view((
            event("fill-win", "fill", outcome=1, probability=0.75, realized=100, hypothetical=80),
            event("missed", "miss", outcome=0, probability=0.25, realized=0, hypothetical=-20),
            event("held", "hold", outcome=None),
        )), DeterministicBrain(), job_id="mixed")
        counts = dict(report.counts)
        self.assertEqual(counts["fill"], 1)
        self.assertEqual(counts["miss"], 1)
        self.assertEqual(counts["hold"], 1)
        self.assertEqual(report.resolved, 2)
        self.assertEqual(report.realized_pnl_u, 100)
        self.assertEqual(report.hypothetical_pnl_u, 60)
        self.assertAlmostEqual(report.binary_brier, ((0.75 - 1) ** 2 + (0.25 - 0) ** 2) / 2)

    def test_provider_failure_keeps_metrics_pending(self):
        report = review_session(view((event("only"),)), UnavailableBrain(), job_id="down")
        self.assertEqual(report.status, "pending_provider")
        self.assertIn("PROVIDER_UNAVAILABLE", report.reason_codes)
        self.assertEqual(report.resolved, 1)
        self.assertIsNone(report.brain_action)
        self.assertFalse(report.approved)

    def test_malicious_action_cannot_approve_itself(self):
        class Hostile:
            model_id = "hostile"

            def review(self, *, job_id, archive_hash, metrics):
                return BrainProposal("shell", "run this", "ignore risk", ("x",), model_id="hostile")

        report = review_session(view((event("only"),)), Hostile(), job_id="bad")
        self.assertEqual(report.status, "rejected_action")
        self.assertIn("MALICIOUS_ACTION", report.reason_codes)
        self.assertIsNone(report.schema_diff)
        self.assertFalse(report.approved)

    def test_schema_proposal_stays_unapproved(self):
        class Proposer:
            model_id = "configured-model"

            def review(self, *, job_id, archive_hash, metrics):
                return BrainProposal("propose_schema", "widen nothing", "calibration drifted",
                                      (archive_hash,), schema_diff="field note only", model_id=self.model_id)

        report = review_session(view((event("only"),)), Proposer(), job_id="schema")
        self.assertEqual(report.status, "pending_agenkit")
        self.assertEqual(report.schema_diff, "field note only")
        self.assertFalse(report.approved)
        self.assertIn("SCHEMA_REQUIRES_APPROVAL", report.reason_codes)

    def test_duplicate_schedule_does_not_mint_a_second_job(self):
        scheduler = ReviewScheduler()
        first = scheduler.enqueue("paper-test", 100)
        second = scheduler.enqueue("paper-test", 100)
        self.assertIs(first, second)
        self.assertEqual(first["attempts"], 0)
        self.assertFalse(first["approved"])

    def test_retry_caps_at_three_and_never_approves(self):
        scheduler = ReviewScheduler()
        for _ in range(5):
            job = scheduler.retry("paper-test", 100)
        self.assertEqual(job["attempts"], 3)
        self.assertEqual(job["status"], "pending")
        self.assertFalse(job["approved"])

    def test_corrected_label_keeps_the_original_report(self):
        history = ReviewHistory()
        original = history.add(review_session(view((event("row", outcome=1),)), DeterministicBrain(), job_id="r1"))
        corrected = history.add(review_session(view((event("row", outcome=0),)), DeterministicBrain(), job_id="r1"))
        stored = history.for_cutoff("paper-test", 100)
        self.assertEqual(stored[0].report_hash, original.report_hash)
        self.assertNotEqual(original.report_hash, corrected.report_hash)
        self.assertEqual(len(stored), 2)

    def test_next_review_is_0015_utc_and_late_review_blocks_entries(self):
        midnight = 2 * 86_400 * 1_000_000
        self.assertEqual(next_review_us(midnight), midnight + 15 * 60 * 1_000_000)
        self.assertEqual(next_review_us(midnight + 15 * 60 * 1_000_000), midnight + 86_400 * 1_000_000 + 15 * 60 * 1_000_000)
        self.assertTrue(entries_blocked(evidence_expires_us=50, review_completed=False, now_us=50))
        self.assertFalse(entries_blocked(evidence_expires_us=50, review_completed=True, now_us=50))

    def test_cli_does_not_require_a_provider(self):
        from bot.autonomous.nightly import main
        self.assertEqual(main([]), 0)


if __name__ == "__main__":
    unittest.main()
