"""Paper decision step. Refuses live venues before any order."""
import unittest
from dataclasses import replace
from types import SimpleNamespace

from bot.autonomous.contracts import ControlState, IntentReceipt, Mode, Permit, ReceiptStatus, Regime, RiskLimits
from bot.autonomous.paper_cycle import PaperCycle
from tests.autonomous.test_policy_boundaries import EntryCompositionTests


class SpyAuthority:
    venue = SimpleNamespace(venue="paper")

    def __init__(self):
        self.calls = []

    def submit(self, proposal, *, decision, request_id):
        self.calls.append(request_id)
        return IntentReceipt(request_id, ReceiptStatus.PENDING, "intent-test")


class PaperCycleTests(EntryCompositionTests):
    def step_kwargs(self, **kwargs):
        policy = self.evaluate()
        proposal = policy.proposal
        control = kwargs.pop("control", None) or ControlState(mode=Mode.PAPER, permit=Permit.ENTRY,
            control_epoch=proposal.control_epoch, boot_id="boot-test", deadman_deadline_mono_ns=9_000_000_000_000,
            active_release_hash=proposal.release_hash, reconciliation_ready=True, armed=True)
        account = replace(self.account, reconciled_at_us=self.s.now, private_heartbeat_us=self.s.now,
                          marked_at_us=self.s.now)
        args = dict(decision=self.s.decision, snapshot=self.s.snapshot, bundle=self.fixture_bundle,
                    edge=self.s.edge, account=account, control=control,
                    limits=RiskLimits(mode=Mode.PAPER, armed=True, candidate_eligible=True,
                        tokenizer_config_hash="1" * 64, calibration_config_hash="2" * 64),
                    market=self.s.market, now_us=self.s.now, context=self.context, now_mono_ns=1_000_000_000,
                    current_boot_id="boot-test")
        args.update(kwargs)
        return args

    def cycle(self, **kwargs):
        authority = kwargs.pop("authority", SpyAuthority())
        step = PaperCycle(authority)
        args = self.step_kwargs(**kwargs)
        args["request_id"] = "paper-step-1"
        return step, authority, step.consider(**args)

    def test_live_venue_cannot_be_constructed(self):
        with self.assertRaises(ValueError):
            PaperCycle(SimpleNamespace(venue=SimpleNamespace(venue="coinbase")))

    def test_crisis_and_low_confidence_do_not_reach_the_venue(self):
        for name, decision in {
            "crisis": replace(self.s.decision, regime=Regime.CRISIS),
            "low": replace(self.s.decision, direction_confidence=0.59),
        }.items():
            with self.subTest(name):
                step, authority, outcome = self.cycle(decision=decision)
                self.assertFalse(outcome.dispatched)
                self.assertEqual(authority.calls, [])
                self.assertIsNone(outcome.risk)

    def test_kill_switch_blocks_a_proposal_the_policy_would_allow(self):
        _, authority, outcome = self.cycle(control=ControlState(
            mode=Mode.PAPER, permit=Permit.NONE, control_epoch=3, boot_id="boot-test",
            deadman_deadline_mono_ns=9_000_000_000_000, active_release_hash=self.fixture_bundle.bundle_hash,
            reconciliation_ready=True, armed=False, hard_killed=True))
        self.assertFalse(outcome.dispatched)
        self.assertEqual(authority.calls, [])
        self.assertIn("HARD_KILL", outcome.reason_codes)

    def test_allowed_risk_is_the_only_path_that_submits(self):
        _, authority, outcome = self.cycle()
        self.assertTrue(outcome.risk.allowed, outcome.reason_codes)
        self.assertEqual(authority.calls, ["paper-step-1"])
        self.assertTrue(outcome.dispatched)
        self.assertEqual(outcome.receipt.status, ReceiptStatus.PENDING)

    def test_open_gate_without_entry_context_does_not_submit(self):
        from bot.autonomous.worker import PaperWorker
        authority = SpyAuthority()
        worker = PaperWorker(authority)
        kwargs = self.step_kwargs()
        kwargs.pop("context")
        held = worker.consider_gated(kwargs["now_us"], request_id="paper-step-1", **kwargs)
        self.assertEqual(held["status"], "policy_hold")
        self.assertEqual(held["reason"], "NO_ENTRY_CONTEXT")
        self.assertFalse(held["dispatched"])
        self.assertEqual(authority.calls, [])

    def test_open_gate_enters_the_cycle_and_low_confidence_does_not(self):
        from bot.autonomous.worker import PaperWorker
        authority = SpyAuthority()
        worker = PaperWorker(authority)
        kwargs = self.step_kwargs()
        opened = worker.consider_gated(kwargs["now_us"], request_id="paper-step-1", **kwargs)
        self.assertTrue(opened["dispatched"])
        self.assertEqual(authority.calls, ["paper-step-1"])
        low_authority = SpyAuthority()
        low_worker = PaperWorker(low_authority)
        low = replace(kwargs["decision"], direction_confidence=0.59)
        refused = low_worker.consider_gated(kwargs["now_us"], **{**kwargs, "decision": low})
        self.assertEqual(refused["status"], "escalated")
        self.assertFalse(refused["dispatched"])
        self.assertEqual(low_authority.calls, [])

    def test_worker_runs_the_real_cycle_once_per_close(self):
        from bot.autonomous.worker import PaperWorker
        authority = SpyAuthority()
        worker = PaperWorker(authority)
        kwargs = self.step_kwargs()
        close_us = kwargs["now_us"]
        first = worker.consider_close(close_us, **kwargs)
        second = worker.consider_close(close_us, **kwargs)
        crisis = replace(kwargs["decision"], regime=Regime.CRISIS)
        third = worker.consider_close(close_us + 1, **{**kwargs, "decision": crisis})
        self.assertTrue(first["dispatched"])
        self.assertEqual(second["status"], "duplicate_or_historical")
        self.assertFalse(third["dispatched"])
        self.assertTrue(third["halted"])
        self.assertEqual(authority.calls, [f"close-{close_us}"])


if __name__ == "__main__":
    unittest.main()
