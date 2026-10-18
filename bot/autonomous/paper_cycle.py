"""One paper decision step. Policy, then hard risk, then the paper venue.

Nothing in this module calls Jev or a live exchange. A refusal never reaches
the venue. Crisis and sub-0.60 confidence stay inside policy and are not orders.
"""
from __future__ import annotations

from dataclasses import dataclass

from .contracts import IntentReceipt, PolicyResult, RiskVerdict
from .policy import evaluate_candidate
from .risk import assess


@dataclass(frozen=True)
class CycleOutcome:
    dispatched: bool
    reason_codes: tuple[str, ...]
    policy: PolicyResult | None = None
    risk: RiskVerdict | None = None
    receipt: IntentReceipt | None = None


class PaperCycle:
    def __init__(self, authority):
        venue = getattr(authority, "venue", None)
        if getattr(venue, "venue", None) != "paper":
            raise ValueError("paper cycle refuses a non-paper venue")
        self.authority = authority

    def consider(self, decision, snapshot, bundle, edge, account, control, limits, market, now_us, *,
                 context=None, now_mono_ns=None, current_boot_id=None, request_id: str) -> CycleOutcome:
        if type(request_id) is not str or not request_id:
            raise ValueError("request id required")
        policy = evaluate_candidate(decision, snapshot, bundle, edge, account, context=context)
        if not policy.propose or policy.proposal is None:
            return CycleOutcome(False, policy.reason_codes, policy)
        verdict = assess(policy.proposal, account, control, limits, market, now_us,
                         now_mono_ns=now_mono_ns, current_boot_id=current_boot_id)
        if not verdict.allowed:
            return CycleOutcome(False, verdict.reason_codes, policy, verdict)
        receipt = self.authority.submit(policy.proposal, decision=policy.proposal.decision, request_id=request_id)
        dispatched = type(receipt) is IntentReceipt and receipt.status.value in ("pending", "completed")
        return CycleOutcome(dispatched, receipt.reason_codes if type(receipt) is IntentReceipt else ("INVALID_RECEIPT",),
                            policy, verdict, receipt if type(receipt) is IntentReceipt else None)
