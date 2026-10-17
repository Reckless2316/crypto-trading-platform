"""Conservative candidate policy primitives.

This module can refuse, request durable escalation, or propose a bounded entry.
It never grants order authority: the hard risk layer and the ledger/authority
reservation boundary still decide whether a proposal may be dispatched.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .contracts import (
    AccountRevision, AccountView, ApprovedBundle, CalibrationArtifact, CandidateDecision,
    DecisionBatch, DecisionReference, EdgeEstimate, ExecutableBook, MarketRules, NumericSnapshot,
    OrderProposal, PolicyResult, Regime, RiskLimits, Side, SizeRejection, UnavailableEdge,
    content_hash,
)


@dataclass(frozen=True)
class EntryContext:
    """Evidence the policy needs to turn an approved judgment into a bounded entry proposal."""
    batch: DecisionBatch
    artifact: CalibrationArtifact
    market: MarketRules
    book: ExecutableBook
    limits: RiskLimits
    ledger: Any
    now_us: int
    control_epoch: int
    deadline_mono_ns: int


def entry_gate(quality, confidence, risk_state, toxic, direction) -> bool:
    """Primitive entry filter; a true result is never an authorization."""
    quality_value = getattr(quality, "value", quality)
    risk_value = getattr(risk_state, "value", risk_state)
    direction_value = getattr(direction, "value", direction)
    return (
        type(quality) is int and 2 <= quality <= 3
        and type(confidence) in (int, float) and math.isfinite(confidence)
        and 0.80 < confidence <= 1
        and risk_value == "safe"
        and toxic is False
        and direction_value == "long"
    )


def _hold(account, snapshot, *reason_codes: str,
          halt_transitions: tuple[str, ...] = (),
          escalation_codes: tuple[str, ...] = ()) -> PolicyResult:
    revision = account.revision if type(account) is AccountView else AccountRevision()
    snapshot_hash = snapshot.snapshot_hash if type(snapshot) is NumericSnapshot else content_hash({"snapshot": "unavailable"})
    return PolicyResult(False, tuple(reason_codes), revision, snapshot_hash,
        halt_transitions=halt_transitions, escalation_codes=escalation_codes)


def evaluate_candidate(decision, snapshot, bundle, edge, account, *, context: EntryContext | None = None) -> PolicyResult:
    """Evaluate one candidate; propose only a capped long entry backed by verified evidence.

    Without an `EntryContext` the result is always HOLD. With one, the edge must
    reproduce from ledger evidence, the release must name its calibrator and be
    unexpired, and `size_entry` must produce a capped size.
    """
    if (type(decision) is not CandidateDecision or type(snapshot) is not NumericSnapshot
            or type(bundle) is not ApprovedBundle or type(account) is not AccountView):
        return _hold(account, snapshot, "POLICY_INPUT_INVALID")
    # Escalations act before any other refusal: a crisis halts the account and low
    # confidence pauses the candidate even when the judgment is unusable for entry.
    if decision.regime is Regime.CRISIS:
        return _hold(account, snapshot, "ACCOUNT_CRISIS_HALT_REQUIRED",
            halt_transitions=("crisis_halt",), escalation_codes=("BRAIN_CRISIS",))
    critical_confidences = (decision.direction_confidence, decision.regime_confidence,
        decision.quality_confidence, decision.risk_confidence)
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0.60
           for value in critical_confidences):
        return _hold(account, snapshot, "LOW_CRITICAL_CHOICE_CONFIDENCE",
            escalation_codes=("PAUSE_CANDIDATE_AND_REVIEW",))
    if decision.candidate_id != snapshot.candidate_id:
        return _hold(account, snapshot, "CANDIDATE_SNAPSHOT_MISMATCH")
    if snapshot.account_id != account.account_id or snapshot.account_revision != account.revision:
        return _hold(account, snapshot, "STALE_ACCOUNT_SNAPSHOT")
    if snapshot.release_hash != bundle.bundle_hash:
        return _hold(account, snapshot, "RELEASE_SNAPSHOT_MISMATCH")
    candidate = next((item for item in bundle.bundle.candidates
                      if item.candidate_id == decision.candidate_id), None)
    if candidate is None or not candidate.eligible:
        return _hold(account, snapshot, "CANDIDATE_NOT_ELIGIBLE")
    if not account.ready:
        return _hold(account, snapshot, "ACCOUNT_NOT_READY")
    if not entry_gate(decision.setup_quality, decision.direction_confidence,
                      decision.risk_state, decision.toxic_flow, decision.direction):
        return _hold(account, snapshot, "ENTRY_GATE_REFUSED")
    if type(edge) is not EdgeEstimate:
        reason = edge.reason_code if type(edge) is UnavailableEdge else "EDGE_UNAVAILABLE"
        return _hold(account, snapshot, reason)
    if type(context) is not EntryContext:
        return _hold(account, snapshot, "AUTHORITY_PROVENANCE_OR_SIZING_UNAVAILABLE")
    from .sizing import size_entry
    if bundle.bundle.calibrator_hash != edge.artifact_hash:
        return _hold(account, snapshot, "CALIBRATOR_NOT_IN_RELEASE")
    if type(context.now_us) is not int or context.now_us >= bundle.bundle.expires_at_us:
        return _hold(account, snapshot, "RELEASE_EXPIRED")
    sized = size_entry(edge, account, context.market, context.limits, context.book, now_us=context.now_us,
        edge_inputs=(context.batch, decision, snapshot, context.artifact), ledger=context.ledger)
    if type(sized) is SizeRejection:
        return _hold(account, snapshot, sized.reason_code)
    persisted = context.ledger.verified_decision_snapshot(context.batch, snapshot)
    if persisted is None or context.batch.bundle_hash != bundle.bundle_hash:
        return _hold(account, snapshot, "DECISION_PROVENANCE_UNAVAILABLE")
    reference = DecisionReference(persisted["decision_id"], context.batch.request_id,
        snapshot.snapshot_hash, bundle.bundle_hash)
    proposal = OrderProposal(content_hash(("entry-v1", persisted["decision_id"], decision.candidate_id)),
        account.account_id, decision.candidate_id, sized.instrument, Side.BUY, sized.base_quantity,
        sized.limit_price, sized.max_fee_usd_u, bundle.bundle_hash, reference,
        snapshot.cutoffs.candle_close_us, "entry", False, context.control_epoch,
        context.deadline_mono_ns, sized.loss_budget_usd_u)
    return PolicyResult(True, ("ENTRY_PROPOSED",), account.revision, snapshot.snapshot_hash, proposal)
