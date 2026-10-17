"""Fail-closed evidence requirements and thesis assessment."""
from __future__ import annotations

import hashlib
import math
import json
from pathlib import Path
from decimal import Decimal
from dataclasses import dataclass
from enum import Enum
from typing import Any

from bot.autonomous.contracts import CandidateDefinition, EvidenceManifest, EvidenceObservation, ThesisAssessment, content_hash
from research.autonomous.evidence import EvidenceRegistry, is_source_issued_observation


REQUIRED_EVIDENCE = (
    "price", "market_cap", "fdv", "circulating_supply", "total_supply",
    "forward_supply", "revenue", "fees", "incentives", "token_accrual",
    "tvl_or_aum", "active_users", "concentration", "execution_costs",
)

# The six-bit contract allows at most six rubric clauses. These names are a
# stable, lossless spelling of the exact candidate clauses in the approved
# design; they are not generic evidence-field presence bits.
CONDITION_NAMES = {
    "aave": ("executed_purchase_evidence_current", "verified_net_surplus_supports_purchases",
             "no_unresolved_budget_conflict", "book_confirms_approved_long_timing"),
    "hype": ("validated_net_fee_absorption_vs_forward_issuance", "retained_fee_paying_activity",
             "venue_health", "approved_long_timing"),
    "pendle": ("realized_nonpoints_fee_retention", "current_spendle_mechanics_verified",
                "current_emissions_verified", "liquid_pendle_timing_without_staking_income"),
    "aero": ("organic_fees_isolated_from_bribes_subsidies", "emissions_checked",
             "migration_checked", "liquid_aero_without_locked_cashflow"),
    "ldo": ("actual_revenue_hurdle_deficit_cleared", "purchases_verified",
            "not_eth_price_only_activation"),
    "ondo": ("verified_entitlement_gap_supply_thesis", "neutral_spot_release"),
}


# Existing row-condition IDs are documented research gaps, not equivalent
# to the approved thesis clauses. None means explicitly unmapped and inert.
SCHEMA_CONDITION_MAPPING = {
    'aave': {'current_budget_enacted': None, 'buyback_pause_or_resumption': None, 'settled_buyback_coverage': None, 'recurring_surplus_after_costs_and_bad_debt': None, 'reserve_reuse_vs_burn': None, 'treasury_grants_and_forward_sellable_supply': None, 'retained_borrowers_and_net_tvl': None, 'entity_adjusted_concentration': None, 'executable_listing_depth_and_cost': None},
    'aero': {'deployed_minter_parameters': None, 'actual_emissions_rebases_and_sellable_supply': None, 'organic_swap_fees_net_of_incentives': None, 'repeat_user_fees_and_retention': None, 'voting_nft_and_beneficial_owner_concentration': None, 'migration_terms_verified_and_executed': None, 'executable_listing_depth_and_cost': None},
    'hype': {'fee_distribution_after_hlp_deployer_referral_rebate': None, 'assistance_fund_conversion_and_burn_receipts': None, 'current_issuance_and_30_90d_sellable_supply': None, 'retained_fee_paying_traders': None, 'withdrawable_collateral_and_hlp_flows': None, 'beneficial_owner_concentration': None, 'executable_listing_depth_and_cost': None},
    'ldo': {'current_nest_deficit_and_hurdle': None, 'executed_purchase_transactions': None, 'current_30_90d_fees_revenue_and_costs': None, 'current_supply_treasury_and_sellable_unlocks': None, 'staking_deposits_and_queue_treatment': None, 'retained_depositors_and_usage': None, 'custody_cluster_and_beneficial_owner_concentration': None, 'executable_listing_depth_and_cost': None},
    'ondo': {'current_float_and_30_90d_sellable_releases': None, 'verified_token_attributable_cash_entitlement': None, 'product_aum_to_token_value_bridge': None, 'distinct_product_users_and_retention': None, 'entity_adjusted_holder_concentration': None, 'executable_approved_spot_listing_and_depth': None, 'shorting_excluded_from_initial_scope': None},
    'pendle': {'cash_fees_vs_points_and_in_kind': None, 'buyback_receipts_and_settlement_coverage': None, 'physical_supply_excluding_reconciled_locks': None, 'actual_emissions_and_sellable_unlocks': None, 'post_maturity_retained_users': None, 'collateral_adjusted_tvl': None, 'entity_adjusted_token_and_voting_concentration': None, 'executable_listing_depth_and_cost': None},
}

CLAIM_STATUSES = ("mechanism_confirmed", "execution_confirmed", "proposal", "estimate", "speculation", "unknown")
_MEASUREMENT_FIELDS = frozenset({"price", "market_cap", "fdv", "circulating_supply", "total_supply",
    "forward_supply", "revenue", "fees", "incentives", "token_accrual", "tvl_or_aum", "active_users",
    "concentration", "execution_costs"})


class GateKind(str, Enum):
    THESIS_APPROVAL = "thesis_approval"
    BEAR_CASE_VALUE = "bear_case_value"
    CONTRADICTION_CLEAR = "contradiction_clear"
    LISTING_VERIFIED = "listing_verified"
    EXECUTABLE_DEPTH = "executable_depth_usd"
    FDV_CONVENTION = "fdv_supply_convention_verified"
    BEAR_MODEL_APPROVAL = "bear_model_approval"


@dataclass(frozen=True)
class AdmissionGateProof:
    kind: GateKind
    value: Decimal
    observation_hash: str
    expires_at_us: int


ADMISSION_UNITS = {
    "price": "USD/token", "market_cap": "USD", "fdv": "USD",
    "circulating_supply": "token", "total_supply": "token", "forward_supply": "token/90d",
    "revenue": "USD/90d", "fees": "USD/90d", "incentives": "USD/90d",
    "token_accrual": "USD/90d", "tvl_or_aum": "USD", "active_users": "entity/30d",
    "concentration": "fraction", "execution_costs": "fraction", "thesis_approval": "bool",
    "bear_case_value": "USD/token", "contradiction_clear": "bool", "listing_verified": "bool",
    "executable_depth_usd": "USD", "fdv_supply": "token", "fdv_supply_convention_verified": "bool",
    "bear_model_approval": "bool",
    **{clause: "bool" for clauses in CONDITION_NAMES.values() for clause in clauses},
}


def thesis_definition_hash(candidate: CandidateDefinition) -> str:
    schema_hash = None
    if candidate.candidate_id.lower() in CONDITION_NAMES:
        schema_path = Path(__file__).resolve().parents[2]/"config/autonomous/schemas"/(candidate.candidate_id.lower()+".json")
        schema_hash = content_hash(json.loads(schema_path.read_text()))
    return content_hash({"definition_version": "canonical-thesis-v1", "candidate_id": candidate.candidate_id,
                         "instrument": candidate.instrument, "condition_names": candidate.condition_names,
                         "schema_hash": schema_hash})


def bear_estimate_subject_hash(observation: EvidenceObservation) -> str:
    from research.autonomous.evidence import decode_verified_observation
    metadata, error = decode_verified_observation(observation)
    if error or metadata is None or observation.metric != "bear_case_value" or not isinstance(metadata["model_version"], str) or not metadata["model_version"]:
        raise ValueError("bear approval requires a versioned estimate")
    return content_hash({"estimate_hash": content_hash(observation), "model_version": metadata["model_version"]})


def missing_evidence(values: dict[str, object]) -> tuple[str, ...]:
    missing = []
    for field in REQUIRED_EVIDENCE:
        value = values.get(field)
        if type(value) not in (Decimal, int, float):
            missing.append(field)
        elif isinstance(value, Decimal) and not value.is_finite():
            missing.append(field)
        elif isinstance(value, float) and not math.isfinite(value):
            missing.append(field)
    return tuple(missing)


def evaluate_thesis(candidate: CandidateDefinition, evidence: EvidenceManifest, *, registry: EvidenceRegistry | None = None,
                    evaluated_at_us: int | None = None) -> ThesisAssessment:
    """Return HOLD unless typed, source-issued, candidate-specific evidence clears every gate.

    Independent gate proofs use the same typed, durable primary observations as
    financial measurements. Input eligibility booleans cannot substitute for
    approval, valuation, listing or canonical thesis-clause proof.
    """
    if not isinstance(candidate, CandidateDefinition) or not isinstance(evidence, EvidenceManifest):
        raise TypeError("evaluate_thesis requires CandidateDefinition and EvidenceManifest")
    candidate_id = candidate.candidate_id.lower()
    reasons: set[str] = set()
    expected_conditions = CONDITION_NAMES.get(candidate_id, ())
    now = evidence.cutoff_us if evaluated_at_us is None else evaluated_at_us
    if type(now) is not int or now < evidence.cutoff_us:
        raise ValueError("evaluation time must be an exact integer at/after evidence cutoff")
    if not expected_conditions:
        reasons.add("unsupported_candidate")
    if evidence.candidate_id != candidate.candidate_id:
        reasons.add("manifest_candidate_mismatch")
    if candidate.condition_names != expected_conditions:
        reasons.add("candidate_rubric_mismatch")
    if not candidate.eligible:
        reasons.add("thesis_approval_missing_or_inactive")
    if candidate.research_only or candidate_id == "ondo":
        reasons.add("candidate_research_only")
    if candidate.evidence_manifest_hash != evidence.manifest_hash:
        reasons.add("candidate_manifest_hash_mismatch")
    if not evidence.eligible:
        reasons.add("manifest_not_eligible")
    if evidence.missing_fields:
        reasons.add("manifest_has_missing_fields")
    manifest_material = "\n".join(sorted(
        f"{item.observation_id}:{item.methodology_hash}:{item.source_event.payload_hash}"
        for item in evidence.observations if isinstance(item, EvidenceObservation)
    ))
    expected_methodology_hash = hashlib.sha256(manifest_material.encode()).hexdigest()
    if evidence.methodology_hash != expected_methodology_hash:
        reasons.add("manifest_methodology_hash_mismatch")

    by_metric: dict[str, list[tuple[EvidenceObservation, dict[str, Any]]]] = {}
    expiries = [evidence.expires_at_us]
    cutoffs: set[int] = set()
    cashflow_intervals: set[tuple[int, int, str]] = set()
    for obs in evidence.observations:
        if not isinstance(obs, EvidenceObservation) or obs.candidate_id != candidate.candidate_id:
            reasons.add("candidate_mismatch")
            continue
        if not is_source_issued_observation(obs, registry):
            reasons.add("untrusted_observation_source_binding")
            continue
        from research.autonomous.evidence import decode_verified_observation
        envelope, error = decode_verified_observation(obs)
        if error or envelope is None:
            reasons.add(error or "untrusted_observation_metadata")
            continue
        if envelope["instrument"] != candidate.instrument:
            reasons.add("candidate_instrument_mismatch")
            continue
        cutoffs.add(envelope["cutoff_us"])
        expiries.append(envelope["expires_at_us"])
        expected_status = "estimate" if obs.metric == "bear_case_value" else "execution_confirmed"
        if envelope["claim_status"] != expected_status:
            reasons.add(f"unconfirmed:{obs.metric}")
            continue
        if obs.unit != ADMISSION_UNITS.get(obs.metric):
            reasons.add(f"unit_mismatch:{obs.metric}")
            continue
        if envelope["expires_at_us"] <= now:
            reasons.add(f"expired:{obs.metric}")
            continue
        window_days = 90 if obs.metric in {"revenue", "fees", "incentives", "token_accrual"} else 30 if obs.metric == "active_users" else None
        if window_days is not None and (
            envelope["period_start_us"] is None or envelope["period_end_us"] is None
            or envelope["period_end_us"] - envelope["period_start_us"] != window_days * 86400 * 1_000_000
        ):
            reasons.add(f"window_mismatch:{obs.metric}")
            continue
        if window_days is not None:
            definition = registry.definition_for(envelope) if registry is not None else None
            if (definition is None or type(definition.accounting_max_lag_us) is not int
                    or definition.accounting_max_lag_us < 0
                    or evidence.cutoff_us - envelope["period_end_us"] > definition.accounting_max_lag_us):
                reasons.add(f"accounting_period_stale:{obs.metric}")
                continue
            if window_days == 90:
                if envelope["accounting_basis"] != "cash":
                    reasons.add(f"unsupported_accounting_basis:{obs.metric}")
                    continue
                cashflow_intervals.add((envelope["period_start_us"], envelope["period_end_us"], envelope["accounting_basis"]))
        if obs.metric == "forward_supply" and (
            envelope["forecast_coverage_complete"] is not True
            or type(envelope["forecast_start_us"]) is not int
            or type(envelope["forecast_end_us"]) is not int
            or envelope["forecast_start_us"] != evidence.cutoff_us
            or envelope["forecast_end_us"] != evidence.cutoff_us + 90 * 86400 * 1_000_000
        ):
            reasons.add("forward_supply_coverage_unproven")
            continue
        by_metric.setdefault(obs.metric, []).append((obs, envelope))

    if evidence.cutoff_us not in cutoffs or len(cutoffs) != 1:
        reasons.add("inconsistent_valuation_cutoff")
    if len(cashflow_intervals) != 1:
        reasons.add("cashflow_intervals_incompatible")
    for metric, records in by_metric.items():
        if len(records) > 1:
            reasons.add(f"conflicting:{metric}")
    measurements = {metric: records[0][0].value for metric, records in by_metric.items() if len(records) == 1}
    # All quantitative claims require an executed, unit-bearing primary record;
    # mechanism disclosures are never measurements. The full set is absent in
    # the approved research, so this remains a blocking gap.
    missing = missing_evidence(measurements)
    reasons.update(f"missing:{name}" for name in missing)
    if any(metric in _MEASUREMENT_FIELDS for metric in by_metric) and not all(
        len(by_metric.get(field, ())) == 1 for field in REQUIRED_EVIDENCE
    ):
        reasons.add("quantitative_measurement_set_incomplete")
    gates: dict[GateKind, AdmissionGateProof] = {}
    for kind in GateKind:
        records = by_metric.get(kind.value, ())
        if len(records) == 1:
            observation, metadata = records[0]
            gates[kind] = AdmissionGateProof(kind, observation.value, content_hash(observation), metadata["expires_at_us"])
        else:
            reasons.add(f"gate_missing:{kind.value}")
    for kind in (GateKind.THESIS_APPROVAL, GateKind.CONTRADICTION_CLEAR,
                 GateKind.LISTING_VERIFIED, GateKind.FDV_CONVENTION, GateKind.BEAR_MODEL_APPROVAL):
        if kind not in gates or gates[kind].value != 1:
            reasons.add(f"gate_not_verified:{kind.value}")
    thesis_approval = by_metric.get("thesis_approval", ())
    if len(thesis_approval) != 1 or thesis_approval[0][1]["approval_subject_hash"] != thesis_definition_hash(candidate):
        reasons.add("thesis_approval_subject_mismatch")
    model_approval = by_metric.get("bear_model_approval", ())
    estimate = by_metric.get("bear_case_value", ())
    try:
        expected_subject = bear_estimate_subject_hash(estimate[0][0]) if len(estimate) == 1 else None
    except ValueError:
        expected_subject = None
    if expected_subject is None or len(model_approval) != 1 or model_approval[0][1]["approval_subject_hash"] != expected_subject:
        reasons.add("bear_model_approval_subject_mismatch")
    if GateKind.EXECUTABLE_DEPTH not in gates or gates[GateKind.EXECUTABLE_DEPTH].value <= 0:
        reasons.add("listing_or_depth_unproven")
    price = measurements.get("price")
    cost = measurements.get("execution_costs")
    bear = gates.get(GateKind.BEAR_CASE_VALUE)
    if price is None or price <= 0 or cost is None or not 0 <= cost < 1 or bear is None or bear.value * (1 - cost) <= price:
        reasons.add("bear_case_discount_after_costs_not_positive")
    if "concentration" in measurements and not 0 <= measurements["concentration"] <= 1:
        reasons.add("invalid_concentration")
    if not missing:
        if any(measurements[name] < 0 for name in REQUIRED_EVIDENCE if name != "revenue"):
            reasons.add("negative_quantitative_input")
        if measurements["circulating_supply"] > measurements["total_supply"]:
            reasons.add("inconsistent_supply")
        if measurements["circulating_supply"] <= 0 or measurements["total_supply"] <= 0:
            reasons.add("nonpositive_supply")
        fdv_supply = measurements.get("fdv_supply")
        if (fdv_supply is None or fdv_supply < measurements["circulating_supply"]
                or measurements["market_cap"] != measurements["price"] * measurements["circulating_supply"]
                or measurements["fdv"] != measurements["price"] * fdv_supply):
            reasons.add("inconsistent_valuation_denominator")
        if measurements["revenue"] > measurements["fees"] or measurements["token_accrual"] > max(measurements["revenue"], Decimal(0)):
            reasons.add("unreconciled_cashflow")

    # Research-gap row IDs remain unmapped. Only independently proved exact
    # canonical clause names can set bits; mere field presence never sets one.
    condition_bits = 0
    for index, name in enumerate(expected_conditions):
        if measurements.get(name) == 1:
            condition_bits |= 1 << index
        else:
            reasons.add(f"condition_unproven:{name}")
    expiry = min(expiries) if expiries else evidence.cutoff_us
    if expiry <= now:
        reasons.add("expired_or_unbounded_evidence_manifest")
    eligible = not reasons
    return ThesisAssessment(
        candidate_id=candidate.candidate_id,
        eligible=eligible,
        condition_bits=condition_bits,
        evidence_manifest_hash=evidence.manifest_hash,
        reason_codes=tuple(sorted(reasons)),
        expires_at_us=expiry,
    )
