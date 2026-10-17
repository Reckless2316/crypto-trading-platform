"""Fail-closed, versioned edge calibration for resolved outcome labels.

The calibration boundary accepts explicit probability and outcome fields. It
never treats a model's confidence score as a probability and never fills gaps
between unsupported bins.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, asdict, replace
from decimal import Decimal, localcontext, ROUND_FLOOR, Inexact, Rounded
from fractions import Fraction
from typing import Any

from .contracts import (
    CalibrationArtifact, CalibrationBin, EdgeEstimate,
    Regime, UnavailableEdge, content_hash, DecisionBatch, CandidateDecision,
    NumericSnapshot, Direction,
    decimal_text,
)

HORIZON_CANDLES = 4
HOUR_US = 3_600_000_000
MIN_BIN_OUTCOMES = 100
MAX_CALIBRATION_BINS = 4
_WILSON_Z_ONE_SIDED_95 = 1.6448536269514722


@dataclass(frozen=True)
class OutcomeVerificationRequest:
    label_id: str
    forecast_id: str
    decision_id: str
    candidate_id: int
    side: str
    model_id: str
    schema_hash: str
    dictionary_hash: str
    execution_policy_hash: str
    decision_at_us: int
    forecast_at_us: int
    as_of_us: int
    previous_event_id: str
    source_request_json: str

    @property
    def request_hash(self) -> str:
        return content_hash(asdict(self))


class OutcomeEvidenceVerifier(ABC):
    """Authority-injected source verifier; never deserialize this dependency.

    A production implementation must prove the first eligible quote, executable
    quantity/depth, actual fee/slippage convention, contiguous candle evidence,
    revisions and source receipts from immutable persisted records. There is no
    permissive default or production implementation in this module. The hook
    is trusted process wiring, like the authority risk hook; Python code already
    executing inside authority is outside the JSON/role threat boundary.
    """
    @abstractmethod
    def verify(self, request: OutcomeVerificationRequest) -> VerifiedOutcomeEvidence | None:
        """Return issuer-bound evidence, or None if any source proof is missing."""
        raise NotImplementedError


@dataclass(frozen=True)
class VerifiedOutcomeEvidence:
    issuer: OutcomeEvidenceVerifier
    request_hash: str
    entry_event_us: int
    entry_received_us: int
    entry_known_us: int
    hourly_closes_us: tuple[int, ...]
    exit_event_us: int
    exit_received_us: int
    exit_known_us: int
    verified_at_us: int
    base_quantity: Decimal
    entry_price_usd: Decimal
    exit_price_usd: Decimal
    entry_fee_usd: Decimal
    exit_fee_usd: Decimal
    slippage_cost_usd: Decimal
    execution_manifest_hash: str
    source_record_hashes: tuple[str, ...]
    short_borrow_record_hash: str | None


def verified_outcome_payload(request: OutcomeVerificationRequest,
        evidence: VerifiedOutcomeEvidence, verifier: OutcomeEvidenceVerifier) -> dict:
    """Validate trusted source evidence and compute a fixed-horizon binary label.

    Entry/exit prices are before the separately charged slippage amount; a
    production verifier must prevent double-counting execution impact. Shorts
    require borrow proof, with all borrow costs included in explicit fees.
    Earlier protective exits need a later policy-specific verifier extension;
    this initial interface resolves terminal four-candle outcomes only.
    """
    if (type(request) is not OutcomeVerificationRequest or type(evidence) is not VerifiedOutcomeEvidence
            or not isinstance(verifier, OutcomeEvidenceVerifier) or evidence.issuer is not verifier
            or evidence.request_hash != request.request_hash):
        raise ValueError("outcome evidence is not bound to the configured verifier/request")
    if request.side not in ("buy", "sell"):
        raise ValueError("unsupported outcome side")
    times=(request.decision_at_us,request.forecast_at_us,request.as_of_us,
        evidence.entry_event_us,evidence.entry_received_us,evidence.entry_known_us,
        evidence.exit_event_us,evidence.exit_received_us,evidence.exit_known_us,evidence.verified_at_us)
    if (any(type(value) is not int or value < 0 for value in times)
            or not request.decision_at_us <= request.forecast_at_us < evidence.entry_event_us
            <= evidence.entry_received_us <= evidence.entry_known_us <= evidence.exit_event_us
            <= evidence.exit_received_us <= evidence.exit_known_us <= evidence.verified_at_us <= request.as_of_us):
        raise ValueError("outcome evidence violates causal timestamp cutoffs")
    start=evidence.entry_event_us//HOUR_US
    expected_closes=tuple((start+i)*HOUR_US for i in range(1,HORIZON_CANDLES+1))
    if (type(evidence.hourly_closes_us) is not tuple or evidence.hourly_closes_us != expected_closes
            or evidence.exit_event_us != expected_closes[-1]):
        raise ValueError("complete exact four-candle evidence required")
    refs=(evidence.execution_manifest_hash,)+evidence.source_record_hashes
    if evidence.short_borrow_record_hash is not None:
        refs+= (evidence.short_borrow_record_hash,)
    if (type(evidence.source_record_hashes) is not tuple or not evidence.source_record_hashes
            or any(type(value) is not str or len(value)!=64
                or any(ch not in "0123456789abcdef" for ch in value) for value in refs)
            or request.side=="sell" and evidence.short_borrow_record_hash is None):
        raise ValueError("verified execution source and short-borrow identities required")
    positive=(evidence.base_quantity,evidence.entry_price_usd,evidence.exit_price_usd)
    costs=(evidence.entry_fee_usd,evidence.exit_fee_usd,evidence.slippage_cost_usd)
    for value in positive+costs:
        decimal_text(value)
    if any(value<=0 for value in positive) or any(value<0 for value in costs):
        raise ValueError("invalid executable quantities/prices/costs")
    quantity=Fraction(evidence.base_quantity)
    entry=Fraction(evidence.entry_price_usd)
    exit=Fraction(evidence.exit_price_usd)
    gross=quantity*(exit-entry)*(1 if request.side=="buy" else -1)
    profit=gross-sum((Fraction(value) for value in costs),Fraction(0))
    ratio=profit/(quantity*entry)
    with localcontext() as context:
        context.prec=256
        context.rounding=ROUND_FLOOR
        # Decimal contexts are inherited from the caller. Projection must be
        # deterministic even if a caller enables traps for intentional
        # precision loss or rounding; the exact Fraction remains authoritative.
        context.traps[Inexact]=False
        context.traps[Rounded]=False
        projection=(Decimal(ratio.numerator)/Decimal(ratio.denominator)).quantize(Decimal("1e-38"),rounding=ROUND_FLOOR)
    projected_text=decimal_text(projection)
    if int(projection>0)!=int(ratio>0):
        raise ValueError("positive exact outcome is below supported Decimal projection precision")
    return {"net_positive_outcome":int(ratio>0),"terminal_direction_outcome":None,
        "net_return":projected_text,"net_return_exact":(str(ratio.numerator),str(ratio.denominator)),
        "net_return_projection":"decimal_scale38_floor_v1",
        "resolved_at_us":evidence.verified_at_us,"observed_through_us":evidence.exit_event_us,
        "first_executable_quote_us":evidence.entry_event_us,"horizon_close_us":evidence.exit_event_us,
        "verified_execution":{"base_quantity":decimal_text(evidence.base_quantity),
            "entry_price_usd":decimal_text(evidence.entry_price_usd),
            "exit_price_usd":decimal_text(evidence.exit_price_usd),
            "entry_fee_usd":decimal_text(evidence.entry_fee_usd),
            "exit_fee_usd":decimal_text(evidence.exit_fee_usd),
            "slippage_cost_usd":decimal_text(evidence.slippage_cost_usd)},
        "pnl_method":"quantity_times_price_change_minus_explicit_costs_v1",
        "execution_manifest_hash":evidence.execution_manifest_hash,
        "source_record_hashes":evidence.source_record_hashes,"verification_request_hash":request.request_hash,
        "costs_included":True,"observations":{"quote":True,"depth":True,"fees":True,
            "slippage":True,"matured_data":True,"short_borrow":evidence.short_borrow_record_hash is not None}}


def binary_brier(probabilities: tuple[float, ...], outcomes: tuple[int, ...]) -> float:
    """Return mean squared error for resolved binary outcomes only."""
    if (type(probabilities) is not tuple or type(outcomes) is not tuple
            or not probabilities or len(probabilities) != len(outcomes)):
        raise ValueError("incomplete outcomes")
    if any(type(y) is not int or y not in (0, 1) for y in outcomes):
        raise ValueError("invalid outcome")
    if any(type(p) not in (int, float) or not math.isfinite(p)
           or not 0 <= p <= 1 for p in probabilities):
        raise ValueError("invalid probability")
    return math.fsum((p - y) ** 2 for p, y in zip(probabilities, outcomes)) / len(outcomes)


def direction_brier(probabilities: Sequence[tuple[float, float, float]],
                    outcomes: Sequence[str | None]) -> tuple[float | None, int, int]:
    """Three-class Brier score (long/short/neutral); pending labels are excluded."""
    if len(probabilities) != len(outcomes):
        raise ValueError("incomplete outcomes")
    scores = []
    missing = 0
    for probs, outcome in zip(probabilities, outcomes):
        if (type(probs) is not tuple or len(probs) != 3 or
                any(type(p) not in (float, int) or not math.isfinite(p) or not 0 <= p <= 1 for p in probs) or
                abs(math.fsum(probs) - 1.0) > 1e-6):
            raise ValueError("invalid direction distribution")
        if outcome is None:
            missing += 1
            continue
        if outcome not in ("long", "short", "neutral"):
            raise ValueError("invalid terminal direction label")
        target = tuple(float(name == outcome) for name in ("long", "short", "neutral"))
        scores.append(math.fsum((p-y)**2 for p, y in zip(probs, target)))
    return (math.fsum(scores)/len(scores) if scores else None, len(scores), missing)


def classify_terminal_direction(long_net_return: Decimal,
                                short_net_return: Decimal) -> str:
    """Map cost-adjusted long/short outcomes to the separate 3-class label."""
    if any(type(value) is not Decimal or not value.is_finite()
           for value in (long_net_return, short_net_return)):
        raise ValueError("finite cost-adjusted Decimal returns required")
    long_wins, short_wins = long_net_return > 0, short_net_return > 0
    if long_wins and short_wins:
        raise ValueError("both directional cost hurdles cannot be positive")
    if long_wins:
        return "long"
    if short_wins:
        return "short"
    return "neutral"


def label_mature(maturity_us: int, cutoff_us: int) -> bool:
    if any(type(t) is not int or t < 0 for t in (maturity_us, cutoff_us)):
        raise ValueError("invalid label timestamp")
    return maturity_us <= cutoff_us


def fixed_horizon_maturity(candle_close_us: int, horizon_candles: int) -> int:
    """Earliest pending-label gate; not a resolved executable-label horizon.

    Executable labels must freeze ``fourth_completed_hour_close_after`` from
    their verified first postdecision quote instead.
    """
    if type(candle_close_us) is not int or candle_close_us < 0:
        raise ValueError("invalid forecast candle close")
    if type(horizon_candles) is not int or horizon_candles != HORIZON_CANDLES:
        raise ValueError("unsupported outcome horizon")
    return fourth_completed_hour_close_after(candle_close_us)


def fourth_completed_hour_close_after(first_quote_us: int) -> int:
    """UTC close of the fourth complete hourly candle after an eligible quote."""
    if type(first_quote_us) is not int or first_quote_us < 0:
        raise ValueError("invalid first executable quote timestamp")
    return (first_quote_us // HOUR_US + HORIZON_CANDLES) * HOUR_US


def wilson_lower(successes: int, samples: int, *, z: float = _WILSON_Z_ONE_SIDED_95) -> float:
    """One-sided Wilson lower confidence bound for a Bernoulli proportion."""
    if (type(successes) is not int or type(samples) is not int or samples <= 0
            or not 0 <= successes <= samples or type(z) not in (int, float)
            or not math.isfinite(z) or z <= 0):
        raise ValueError("invalid Wilson inputs")
    p = successes / samples
    z2 = z * z
    denominator = 1 + z2 / samples
    center = p + z2 / (2 * samples)
    radius = z * math.sqrt((p * (1 - p) + z2 / (4 * samples)) / samples)
    return max(0.0, (center - radius) / denominator)


def _unavailable(reason: str, *refs: str) -> UnavailableEdge:
    return UnavailableEdge(reason, tuple(refs))


def estimate_edge(batch: DecisionBatch, decision: CandidateDecision,
                  snapshot: NumericSnapshot, artifact: CalibrationArtifact,
                  *, ledger: Any = None, now_us: int | None = None) -> EdgeEstimate | UnavailableEdge:
    """Estimate edge only for an exact, in-window supported artifact cohort.

    Requires a ledger-verified persisted decision and snapshot, and an artifact
    whose attestation is durably recorded against the decision's release. The
    proposed-direction probability selects a bin; its one-sided Wilson lower
    bound is the conservative p. `confidence` is never used as probability.
    """
    if type(batch) is not DecisionBatch or type(decision) is not CandidateDecision or type(snapshot) is not NumericSnapshot:
        return _unavailable("typed_persisted_forecast_required")
    from .ledger import Ledger
    if type(ledger) is not Ledger:
        return _unavailable("persisted_decision_verification_required")
    if type(now_us) is not int:
        return _unavailable("evaluation_time_required")
    if type(artifact) is not CalibrationArtifact:
        return _unavailable("invalid_calibration_artifact")
    if not artifact.uncertainty_passed or not artifact.tail_checks_passed:
        return _unavailable("artifact_evidence_incomplete", artifact.artifact_hash)
    persisted = ledger.verified_decision_snapshot(batch, snapshot)
    if persisted is None:
        return _unavailable("persisted_decision_verification_failed", batch.request_id)
    if decision not in batch.decisions or decision.candidate_id != snapshot.candidate_id:
        return _unavailable("decision_not_in_persisted_batch", batch.request_id)
    if decision.direction is Direction.NEUTRAL:
        return _unavailable("neutral_direction_has_no_entry_edge")
    if ("buy" if decision.direction is Direction.LONG else "sell") not in artifact.sides:
        return _unavailable("unsupported_side", artifact.artifact_hash)
    if ledger.attested_calibration(artifact.artifact_hash, batch.bundle_hash) != artifact:
        return _unavailable("calibration_attestation_unrecorded", artifact.artifact_hash)
    if (batch.model_id != artifact.model_id or batch.schema_hash != artifact.schema_hash
            or persisted["field_hash"] != artifact.dictionary_hash):
        return _unavailable("artifact_version_mismatch", artifact.artifact_hash)
    if snapshot.instrument not in artifact.instruments or decision.regime not in artifact.regimes:
        return _unavailable("unsupported_cohort", artifact.artifact_hash)
    close = snapshot.cutoffs.candle_close_us
    valid_until = min(artifact.expires_at_us, close + HOUR_US)
    if not artifact.valid_from_us <= close < artifact.expires_at_us:
        return _unavailable("artifact_outside_validity_window", artifact.artifact_hash)
    if not close <= now_us < valid_until:
        return _unavailable("edge_evaluated_outside_validity", artifact.artifact_hash)
    probability = (decision.direction_probabilities.long if decision.direction is Direction.LONG
        else decision.direction_probabilities.short)
    last = len(artifact.mapping) - 1
    selected = next((b for i, b in enumerate(artifact.mapping)
        if b.lower <= probability < b.upper or (i == last and probability == b.upper == 1.0)), None)
    if selected is None:
        return _unavailable("unsupported_probability_bin", artifact.artifact_hash)
    return EdgeEstimate(selected.conservative_probability, artifact.win_per_dollar,
        artifact.loss_per_dollar, artifact.cost_convention,
        valid_until, artifact.artifact_hash,
        content_hash(("edge-input-v1", batch.request_id, decision, snapshot.snapshot_id,
            probability, selected)))


MIN_TAIL_OUTCOMES = 30
MIN_HALF_OUTCOMES = 50
ATTESTATION_METHOD = "calibration-attestation-v2"
LABEL_DEFINITION = ("fixed-four-hourly-candle-net-positive-v1",
    "quantity_times_price_change_minus_explicit_costs_v1", "decimal_scale38_floor_v1")


def label_definition_hash(payload: Mapping[str, Any]) -> str | None:
    """Identity of the label convention a verified payload was produced under."""
    if payload.get("pnl_method") != LABEL_DEFINITION[1] or payload.get("net_return_projection") != LABEL_DEFINITION[2]:
        return None
    return content_hash(LABEL_DEFINITION)


def _row_order(row: Mapping[str, Any]) -> tuple:
    return (row["decision_at_us"], row["forecast_id"], row["label_event_id"])


def attest_calibration_artifact(artifact: CalibrationArtifact, *, training: Sequence[Mapping[str, Any]],
        calibration: Sequence[Mapping[str, Any]], **build_args: Any) -> tuple[CalibrationArtifact, dict]:
    """Deterministic uncertainty and tail checks over the exact rows that built `artifact`.

    Uncertainty: each admitted bin's success rate in both chronological halves of
    the calibration block is at least its conservative probability. Tail: training
    has at least 30 wins and 30 losses, and calibration never shows a smaller win
    or a larger loss than the training-derived W/L. Pure; recording is the ledger's job.
    """
    rebuilt = build_calibration_artifact(training=training, calibration=calibration, **build_args)
    if type(artifact) is not CalibrationArtifact or rebuilt != artifact:
        raise ValueError("artifact does not rebuild from the supplied rows")
    ordered = sorted(calibration, key=_row_order)
    halves = (ordered[:len(ordered)//2], ordered[len(ordered)//2:])
    last = len(artifact.mapping) - 1
    bin_rates = []
    for index, item in enumerate(artifact.mapping):
        rates = []
        for half in halves:
            members = [row for row in half if item.lower <= row["direction_probability"] < item.upper
                       or (index == last and row["direction_probability"] == item.upper == 1.0)]
            if len(members) < MIN_HALF_OUTCOMES:
                raise ValueError("uncertainty check: too few outcomes in a calibration half")
            rates.append(sum(row["net_positive_outcome"] for row in members) / len(members))
        if min(rates) < item.conservative_probability:
            raise ValueError("uncertainty check: bin rate unstable across calibration halves")
        bin_rates.append(rates)
    wins = [row["net_return"] for row in training if row["net_return"] > 0]
    losses = [row["net_return"] for row in training if row["net_return"] < 0]
    if len(wins) < MIN_TAIL_OUTCOMES or len(losses) < MIN_TAIL_OUTCOMES:
        raise ValueError("tail check: too few training wins or losses")
    out_wins = [row["net_return"] for row in calibration if row["net_return"] > 0]
    out_losses = [-row["net_return"] for row in calibration if row["net_return"] < 0]
    if (out_wins and min(out_wins) < artifact.win_per_dollar) or (out_losses and max(out_losses) > artifact.loss_per_dollar):
        raise ValueError("tail check: calibration returns exceed training payoff tails")
    metrics = {"method": ATTESTATION_METHOD, "base_artifact_hash": artifact.artifact_hash,
        "bin_half_rates": bin_rates, "training_wins": len(wins), "training_losses": len(losses),
        "training_rows_hash": content_hash(tuple(sorted(training, key=_row_order))),
        "calibration_rows_hash": content_hash(tuple(sorted(calibration, key=_row_order)))}
    attested = replace(artifact, uncertainty_passed=True, tail_checks_passed=True,
        artifact_hash=content_hash((ATTESTATION_METHOD, artifact.artifact_hash, metrics)))
    return attested, metrics


def _validate_observation(row: Mapping[str, Any], *, before_us: int, after_us: int | None = None) -> None:
    if not isinstance(row, Mapping):
        raise ValueError("invalid calibration observation")
    for field in ("forecast_id", "label_event_id"):
        identity = row.get(field)
        if (type(identity) is not str or not identity or len(identity) > 256
                or not identity.isascii()):
            raise ValueError("immutable forecast and label-event identities required")
    observed_at = row.get("resolved_at_us")
    if type(observed_at) is not int or observed_at > before_us or (after_us is not None and observed_at <= after_us):
        raise ValueError("calibration observation outside chronological block")
    decision_at = row.get("decision_at_us")
    forecast_at = row.get("forecast_at_us")
    quote_at = row.get("first_executable_quote_us")
    horizon_at = row.get("horizon_close_us")
    if any(type(t) is not int for t in (decision_at, forecast_at, quote_at, horizon_at)):
        raise ValueError("causal decision, forecast, quote and horizon timestamps required")
    if (not decision_at <= forecast_at < quote_at < horizon_at
            or horizon_at != fourth_completed_hour_close_after(quote_at)):
        raise ValueError("forecast must precede first quote and its fourth completed hourly close")
    if decision_at > before_us:
        raise ValueError("forecast decision is outside the historical block")
    if observed_at < horizon_at or observed_at > before_us or (after_us is not None and decision_at <= after_us):
        raise ValueError("immature or chronologically misplaced observation")
    p = row.get("direction_probability")
    if type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
        raise ValueError("invalid direction probability")
    ret = row.get("net_return")
    if type(ret) is not Decimal or not ret.is_finite():
        raise ValueError("net-of-costs Decimal return required")
    y = row.get("net_positive_outcome")
    if type(y) is not int or y not in (0, 1):
        raise ValueError("invalid resolved net-positive label")
    if y != int(ret > 0):
        raise ValueError("net-positive label disagrees with net return")
    manifest = row.get("execution_manifest_hash")
    if (row.get("costs_included") is not True or type(manifest) is not str
            or len(manifest) != 64 or any(ch not in "0123456789abcdef" for ch in manifest)):
        raise ValueError("verified net-cost execution evidence required")
    direction_outcome = row.get("terminal_direction_outcome")
    if direction_outcome is not None and direction_outcome not in ("long", "short", "neutral"):
        raise ValueError("invalid terminal direction label")
    observations = row.get("observations")
    required_observations = ("quote", "depth", "fees", "slippage", "matured_data")
    if (row.get("side") not in ("buy", "sell") or type(observations) is not dict
            or any(observations.get(key) is not True for key in required_observations)
            or row.get("side") == "sell" and observations.get("short_borrow") is not True):
        raise ValueError("incomplete executable outcome observations")


def build_calibration_artifact(*, training: Sequence[Mapping[str, Any]],
        calibration: Sequence[Mapping[str, Any]], model_id: str, schema_hash: str,
        dictionary_hash: str, label_hash: str, cost_hash: str, instrument: str,
        regime: Regime, training_cutoff_us: int, calibration_cutoff_us: int,
        valid_from_us: int, expires_at_us: int, side: str = "buy"
        ) -> CalibrationArtifact:
    """Build a single-cohort artifact; quantile boundaries use training only.

    Training returns supply conservative payoff tails; calibration outcomes
    supply Wilson lower bounds. `attest_calibration_artifact` runs the
    uncertainty/tail checks; only the ledger records an attested artifact.
    """
    hashes = (schema_hash,dictionary_hash,label_hash,cost_hash)
    if (not training or not calibration or type(model_id) is not str or not model_id
            or type(instrument) is not str or not instrument or type(regime) is not Regime
            or any(type(value) is not int or value < 0 for value in
                   (training_cutoff_us,calibration_cutoff_us,valid_from_us,expires_at_us))
            or any(type(value) is not str or len(value) != 64
                   or any(ch not in "0123456789abcdef" for ch in value) for value in hashes)):
        raise ValueError("training, calibration, and one supported cohort required")
    if not (training_cutoff_us < calibration_cutoff_us < valid_from_us < expires_at_us):
        raise ValueError("calibration blocks and validity window must be chronological")
    for row in training:
        _validate_observation(row, before_us=training_cutoff_us)
    for row in calibration:
        _validate_observation(row, before_us=calibration_cutoff_us, after_us=training_cutoff_us)
    for identity in ("forecast_id", "label_event_id"):
        values = [row[identity] for row in (*training, *calibration)]
        if len(values) != len(set(values)):
            raise ValueError("forecast or label-event identity reused across calibration rows")
    for rows in (training, calibration):
        for identity in ("forecast_id", "label_event_id"):
            values = [row.get(identity) for row in rows]
            if any(type(value) is not str or not value for value in values) or len(set(values)) != len(values):
                raise ValueError("missing or reused forecast/label identity")
        ordered_times = sorted((r["first_executable_quote_us"], r["horizon_close_us"]) for r in rows)
        if any(ordered_times[i][0] <= ordered_times[i-1][1] for i in range(1, len(ordered_times))):
            raise ValueError("overlapping four-hour outcome windows are not independent")
    if max(r["horizon_close_us"] for r in training) > training_cutoff_us:
        raise ValueError("training horizon crosses training cutoff")
    if min(r["decision_at_us"] for r in calibration) <= training_cutoff_us:
        raise ValueError("calibration forecast originates in training block")
    for identity in ("forecast_id", "label_event_id"):
        if {row[identity] for row in training} & {row[identity] for row in calibration}:
            raise ValueError("training and calibration evidence identities overlap")
    if side not in ("buy", "sell"):
        raise ValueError("calibration side must be buy or sell")
    cohort = lambda row: (row.get("instrument"), row.get("regime"), row.get("side"))
    if any(cohort(row) != (instrument, regime.value, side) for row in (*training, *calibration)):
        raise ValueError("mixed or unsupported calibration cohort")
    exact = {"model_id":model_id,"schema_hash":schema_hash,"dictionary_hash":dictionary_hash,
        "label_hash":label_hash,"cost_hash":cost_hash}
    if any(any(row.get(key) != value for key, value in exact.items())
           for row in (*training, *calibration)):
        raise ValueError("mixed calibration artifact versions")

    sorted_probabilities = sorted(row["direction_probability"] for row in training)
    requested = min(MAX_CALIBRATION_BINS, len(sorted_probabilities))
    boundaries = [0.0]
    for index in range(1, requested):
        left = sorted_probabilities[(index * len(sorted_probabilities)) // requested - 1]
        right = sorted_probabilities[(index * len(sorted_probabilities)) // requested]
        if left < right:
            boundaries.append((left + right) / 2)
    boundaries.append(1.0)
    if len(boundaries) - 1 > MAX_CALIBRATION_BINS:
        raise ValueError("too many quantile bins")
    admitted: list[CalibrationBin] = []
    for index, (lower, upper) in enumerate(zip(boundaries, boundaries[1:])):
        members = [row for row in calibration if lower <= row["direction_probability"] < upper
                   or (index == len(boundaries) - 2 and row["direction_probability"] == 1.0)]
        if len(members) < MIN_BIN_OUTCOMES:
            continue
        admitted.append(CalibrationBin(lower, upper,
            wilson_lower(sum(row["net_positive_outcome"] for row in members), len(members)), len(members)))
    if not admitted:
        raise ValueError("no calibration bin has 100 independent outcomes")

    winning_returns = sorted(row["net_return"] for row in training if row["net_return"] > 0)
    losing_returns = sorted(-row["net_return"] for row in training if row["net_return"] < 0)
    if not winning_returns or not losing_returns:
        raise ValueError("training block lacks positive and negative net returns")
    # Conservative empirical tails: worst observed positive return and worst
    # observed loss. No extrapolation beyond observed returns is permitted.
    win_per_dollar = winning_returns[0]
    loss_per_dollar = losing_returns[-1]
    if win_per_dollar <= 0 or loss_per_dollar <= 0:
        raise ValueError("invalid training payoff tails")
    core = {
        "model_id": model_id, "schema_hash": schema_hash,
        "dictionary_hash": dictionary_hash, "label_hash": label_hash,
        "cost_hash": cost_hash, "instrument": instrument, "regime": regime, "side": side,
        "training_cutoff_us": training_cutoff_us,
        "calibration_cutoff_us": calibration_cutoff_us,
        "valid_from_us": valid_from_us, "expires_at_us": expires_at_us,
        "training_evidence_hash": content_hash(tuple(sorted(training, key=_row_order))),
        "calibration_evidence_hash": content_hash(tuple(sorted(calibration, key=_row_order))),
        "mapping": tuple(admitted), "independent_samples": len(calibration),
        "win_per_dollar": win_per_dollar, "loss_per_dollar": loss_per_dollar,
        "tail_checks_passed": False,
        "method": "training_quantile_bins_v1_one_sided_95_wilson",
        "payoff_method": "training_minimum_positive_maximum_loss_v1",
        "cost_convention": "net_of_all_costs",
        "training_rows_hash": content_hash(tuple(sorted(training,
            key=lambda row: (row["decision_at_us"], row["forecast_id"], row["label_event_id"])))),
        "calibration_rows_hash": content_hash(tuple(sorted(calibration,
            key=lambda row: (row["decision_at_us"], row["forecast_id"], row["label_event_id"])))),
    }
    return CalibrationArtifact(content_hash(core), model_id, schema_hash,
        dictionary_hash, label_hash, cost_hash, (instrument,), (regime,),
        training_cutoff_us, valid_from_us, expires_at_us, tuple(admitted),
        len(calibration), win_per_dollar, loss_per_dollar, False,
        False, "net_of_all_costs", (side,))


def resolved_brier(probabilities: Sequence[float | None], outcomes: Sequence[int | None]) -> tuple[float | None, int, int]:
    """Return Brier score, resolved denominator, and unresolved count.

    Pending labels are omitted from the denominator and reported separately;
    they are never coerced to losses.
    """
    if type(probabilities) not in (tuple, list) or type(outcomes) not in (tuple, list):
        raise ValueError("probability and outcome sequences required")
    if not probabilities or len(probabilities) != len(outcomes):
        raise ValueError("incomplete forecast cohort")
    if any(p is not None and (type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1)
           for p in probabilities):
        raise ValueError("invalid probability")
    resolved_p, resolved_y = [], []
    missing = 0
    for p, outcome in zip(probabilities, outcomes):
        if outcome is None or p is None:
            missing += 1
        elif type(outcome) is int and outcome in (0, 1):
            resolved_p.append(float(p))
            resolved_y.append(outcome)
        else:
            raise ValueError("invalid outcome")
    score = binary_brier(tuple(resolved_p), tuple(resolved_y)) if resolved_y else None
    return score, len(resolved_y), missing
