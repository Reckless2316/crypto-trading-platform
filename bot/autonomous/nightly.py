"""Nightly session review. Metrics are deterministic; proposals are not approvals.

A review may describe a schema change. It cannot arm trading, approve a release,
or send an order. Pending labels never enter a Brier denominator.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

from .brain import ALLOWED_ACTIONS, BrainProposal
from .calibration import binary_brier
from .contracts import ProviderUnavailable, content_hash

_DAY_US = 86_400 * 1_000_000
_REVIEW_OFFSET_US = 15 * 60 * 1_000_000
_KINDS = frozenset({"fill", "partial", "nonfill", "rejection", "veto", "miss", "error", "hold"})
_MAX_ATTEMPTS = 3


def events_from_fill_rows(rows, *, cutoff_us: int) -> tuple[SessionEvent, ...]:
    """Turn ledger fills into review events. A fill is not a resolved outcome."""
    if type(cutoff_us) is not int or cutoff_us < 0:
        raise ValueError("invalid label timestamp")
    events = []
    for row in rows:
        fill_id = row["fill_id"] if hasattr(row, "keys") else row[0]
        event_at = row["event_at_us"] if hasattr(row, "keys") else row[1]
        if type(fill_id) is not str or not fill_id or type(event_at) is not int or event_at < 0:
            raise ValueError("invalid fill row")
        maturity = event_at if event_at <= cutoff_us else None
        events.append(SessionEvent(fill_id, "fill", maturity, None, None, 0, 0))
    return tuple(events)


def _stored_probability(calibrated_p, probabilities_json):
    """Use the forecast's stored probability. A missing value stays missing."""
    import json
    import math
    if type(calibrated_p) is str:
        try:
            value = float(calibrated_p)
        except ValueError:
            value = None
        else:
            if math.isfinite(value) and 0 <= value <= 1:
                return value
    if type(probabilities_json) is str:
        try:
            parsed = json.loads(probabilities_json)
        except json.JSONDecodeError:
            return None
        value = parsed.get("direction_probability") if isinstance(parsed, dict) else None
        if type(value) in (int, float) and math.isfinite(float(value)) and 0 <= float(value) <= 1:
            return float(value)
    return None


def label_mature(maturity_us: int, cutoff_us: int) -> bool:
    if any(type(t) is not int or t < 0 for t in (maturity_us, cutoff_us)):
        raise ValueError("invalid label timestamp")
    return maturity_us <= cutoff_us


@dataclass(frozen=True)
class SessionEvent:
    event_id: str
    kind: str
    maturity_us: int | None
    probability: float | None = None
    outcome: int | None = None
    realized_pnl_u: int = 0
    hypothetical_pnl_u: int = 0

    def __post_init__(self) -> None:
        if not self.event_id or self.kind not in _KINDS:
            raise ValueError("invalid session event")
        if self.maturity_us is not None and (type(self.maturity_us) is not int or self.maturity_us < 0):
            raise ValueError("invalid label timestamp")
        if type(self.realized_pnl_u) is not int or type(self.hypothetical_pnl_u) is not int:
            raise ValueError("pnl must be integer microdollars")


@dataclass(frozen=True)
class SessionView:
    account_id: str
    cutoff_us: int
    archive_hash: str
    events: tuple[SessionEvent, ...]

    def __post_init__(self) -> None:
        if not self.account_id or type(self.cutoff_us) is not int or self.cutoff_us < 0:
            raise ValueError("invalid session view")
        if not self.archive_hash or len(self.archive_hash) != 64:
            raise ValueError("invalid archive hash")
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError("duplicate session event")


@dataclass(frozen=True)
class NightlyReport:
    account_id: str
    cutoff_us: int
    archive_hash: str
    counts: tuple[tuple[str, int], ...]
    binary_brier: float | None
    resolved: int
    pending: int
    realized_pnl_u: int
    hypothetical_pnl_u: int
    included_ids: tuple[str, ...]
    excluded_ids: tuple[str, ...]
    brain_action: str | None
    brain_model_id: str
    approved: bool
    status: str
    reason_codes: tuple[str, ...]
    schema_diff: str | None
    report_hash: str


def next_review_us(now_us: int) -> int:
    """Next 00:15 UTC slot strictly after now, in Unix microseconds."""
    if type(now_us) is not int or now_us < 0:
        raise ValueError("invalid clock")
    midnight = now_us - (now_us % _DAY_US)
    slot = midnight + _REVIEW_OFFSET_US
    if now_us < slot:
        return slot
    return slot + _DAY_US


def entries_blocked(*, evidence_expires_us: int, review_completed: bool, now_us: int) -> bool:
    """Late review expires previously approved evidence and blocks new entries."""
    if any(type(value) is not int or value < 0 for value in (evidence_expires_us, now_us)):
        raise ValueError("invalid evidence window")
    if type(review_completed) is not bool:
        raise ValueError("invalid review flag")
    return now_us >= evidence_expires_us and not review_completed


def _probability_ok(value: float | None) -> bool:
    return type(value) in (int, float) and value == value and 0 <= value <= 1  # NaN fails equality


class ReviewHistory:
    """Append-only reports. A corrected label adds a report; it does not rewrite the old one."""

    def __init__(self) -> None:
        self.reports: list[NightlyReport] = []

    def add(self, report: NightlyReport) -> NightlyReport:
        if any(existing.report_hash == report.report_hash for existing in self.reports):
            return next(existing for existing in self.reports if existing.report_hash == report.report_hash)
        self.reports.append(report)
        return report

    def for_cutoff(self, account_id: str, cutoff_us: int) -> tuple[NightlyReport, ...]:
        return tuple(report for report in self.reports
                     if report.account_id == account_id and report.cutoff_us == cutoff_us)


class ReviewScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def enqueue(self, account_id: str, cutoff_us: int) -> dict:
        key = f"{account_id}:{cutoff_us}"
        existing = self.jobs.get(key)
        if existing is not None:
            return existing
        job = {"id": key, "attempts": 0, "status": "pending", "approved": False}
        self.jobs[key] = job
        return job

    def retry(self, account_id: str, cutoff_us: int) -> dict:
        job = self.enqueue(account_id, cutoff_us)
        if job["attempts"] >= _MAX_ATTEMPTS:
            job["status"] = "pending"
            job["approved"] = False
            return job
        job["attempts"] += 1
        job["status"] = "pending"
        job["approved"] = False
        return job


def review_session(view: SessionView, brain, *, job_id: str) -> NightlyReport:
    if type(view) is not SessionView or not job_id:
        raise ValueError("typed session and job id required")
    counts = {kind: 0 for kind in sorted(_KINDS)}
    included: list[str] = []
    excluded: list[str] = []
    probabilities: list[float] = []
    outcomes: list[int] = []
    realized = hypothetical = 0
    reasons: list[str] = []
    for event in view.events:
        counts[event.kind] += 1
        mature = event.maturity_us is not None and label_mature(event.maturity_us, view.cutoff_us)
        if not mature or event.outcome is None:
            excluded.append(event.event_id)
            continue
        if not _probability_ok(event.probability):
            excluded.append(event.event_id)
            if "INVALID_PROBABILITY" not in reasons:
                reasons.append("INVALID_PROBABILITY")
            continue
        included.append(event.event_id)
        probabilities.append(float(event.probability))
        outcomes.append(event.outcome)
        realized += event.realized_pnl_u
        hypothetical += event.hypothetical_pnl_u
    score = binary_brier(tuple(probabilities), tuple(outcomes)) if probabilities else None
    metrics = {
        "binary_brier": score,
        "resolved": len(included),
        "pending": len(excluded),
        "counts": counts,
    }
    status = "metrics_ready"
    action = None
    model_id = getattr(brain, "model_id", "unconfigured")
    schema_diff = None
    try:
        proposal = brain.review(job_id=job_id, archive_hash=view.archive_hash, metrics=metrics)
    except ProviderUnavailable:
        proposal = None
        status = "pending_provider"
        reasons.append("PROVIDER_UNAVAILABLE")
    if proposal is not None:
        if type(proposal) is not BrainProposal or proposal.action not in ALLOWED_ACTIONS:
            status = "rejected_action"
            action = getattr(proposal, "action", None)
            reasons.append("MALICIOUS_ACTION")
            schema_diff = None
        else:
            action = proposal.action
            model_id = proposal.model_id
            schema_diff = proposal.schema_diff
            if action == "propose_schema":
                status = "pending_agenkit"
                reasons.append("SCHEMA_REQUIRES_APPROVAL")
    body = {
        "account_id": view.account_id,
        "cutoff_us": view.cutoff_us,
        "archive_hash": view.archive_hash,
        "counts": tuple(sorted(counts.items())),
        "binary_brier": score,
        "resolved": len(included),
        "pending": len(excluded),
        "realized_pnl_u": realized,
        "hypothetical_pnl_u": hypothetical,
        "included_ids": tuple(included),
        "excluded_ids": tuple(excluded),
        "brain_action": action,
        "brain_model_id": model_id,
        "approved": False,
        "status": status,
        "reason_codes": tuple(reasons),
        "schema_diff": schema_diff,
    }
    return NightlyReport(**body, report_hash=content_hash(body))


def review_account_activity(connection, account_id: str, cutoff_us: int, archive_hash: str, brain=None) -> NightlyReport:
    """Score one account's fills and unfilled terminal intents. Nothing is approved."""
    from .brain import DeterministicBrain
    if not account_id or len(archive_hash) != 64:
        raise ValueError("invalid review account")
    brain = DeterministicBrain() if brain is None else brain
    fills = list(connection.execute(
        "SELECT fill_id, event_at_us FROM fills WHERE account_id=? AND event_at_us<=?",
        (account_id, cutoff_us)))
    misses = list(connection.execute(
        """SELECT intent_id, recorded_at_us FROM order_intents AS i
           WHERE account_id=? AND state IN ('canceled','rejected') AND recorded_at_us<=?
             AND NOT EXISTS (SELECT 1 FROM fills AS f WHERE f.intent_id=i.intent_id)""",
        (account_id, cutoff_us)))
    events = list(events_from_fill_rows(fills, cutoff_us=cutoff_us))
    resolved_rows = connection.execute(
        """SELECT f.fill_id, l.maturity_us, l.outcome, fc.calibrated_p, fc.probabilities_json, fc.forecast_id
           FROM fills AS f
           JOIN order_intents AS i ON i.intent_id=f.intent_id
           JOIN forecasts AS fc ON fc.decision_id=i.decision_id
            AND fc.candidate_id=i.candidate_id AND fc.side=i.side
           JOIN outcome_labels AS l ON l.forecast_id=fc.forecast_id
           WHERE f.account_id=? AND l.status IN ('resolved','corrected')
             AND l.outcome IN (0,1) AND l.maturity_us<=?""",
        (account_id, cutoff_us)).fetchall()
    by_fill = {}
    used_forecasts = set()
    for fill_id, maturity, outcome, calibrated_p, probabilities_json, forecast_id in resolved_rows:
        probability = _stored_probability(calibrated_p, probabilities_json)
        if probability is None or not label_mature(maturity, cutoff_us):
            continue
        by_fill[fill_id] = (maturity, probability, outcome)
        used_forecasts.add(forecast_id)
    events = [
        SessionEvent(event.event_id, event.kind, by_fill[event.event_id][0], by_fill[event.event_id][1],
                     by_fill[event.event_id][2], 0, 0) if event.event_id in by_fill else event
        for event in events
    ]
    for label_id, maturity, outcome, calibrated_p, probabilities_json, forecast_id in connection.execute(
            """SELECT l.label_id, l.maturity_us, l.outcome, fc.calibrated_p, fc.probabilities_json, fc.forecast_id
               FROM outcome_labels AS l
               JOIN forecasts AS fc ON fc.forecast_id=l.forecast_id
               JOIN decisions AS d ON d.decision_id=fc.decision_id
               JOIN snapshots AS s ON s.snapshot_id=d.snapshot_id
               WHERE s.account_id=? AND l.status IN ('resolved','corrected')
                 AND l.outcome IN (0,1) AND l.maturity_us<=?""",
            (account_id, cutoff_us)):
        if forecast_id in used_forecasts:
            continue
        probability = _stored_probability(calibrated_p, probabilities_json)
        if probability is None or not label_mature(maturity, cutoff_us):
            continue
        if any(event.event_id == label_id for event in events):
            continue
        events.append(SessionEvent(label_id, "hold", maturity, probability, int(outcome), 0, 0))
    for intent_id, recorded_at in misses:
        if type(intent_id) is not str or type(recorded_at) is not int:
            raise ValueError("invalid miss row")
        events.append(SessionEvent(intent_id, "miss", recorded_at, None, None, 0, 0))
    report = review_session(SessionView(account_id, cutoff_us, archive_hash, tuple(events)),
                            brain, job_id=f"review-{account_id}-{cutoff_us}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect nightly review scheduling. Does not load keys or call a venue.")
    parser.add_argument("--next-review-us", type=int, help="Print the next 00:15 UTC slot after this Unix microsecond clock")
    args = parser.parse_args(argv)
    if args.next_review_us is not None:
        print(next_review_us(args.next_review_us))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
