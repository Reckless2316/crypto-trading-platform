"""Strict conversion of captured primary evidence into causal contract records.

Network/API clients are injected by the caller. This module never discovers a
source, upgrades a secondary source, or treats an absent response as zero.

The shared ``EvidenceObservation`` contract does not yet expose raw artifact
path/hash, nullable publication time, period bounds, expiry, or validity reason
as independent fields. This module keeps the full, canonical envelope in
``SourceEvent.stream`` and binds it to ``logical_key``/``methodology_hash``;
the remaining integration gap is a shared contract revision that adds these
fields explicitly so downstream consumers need not decode this envelope.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import stat
import sqlite3
from dataclasses import dataclass, replace
from contextlib import closing
from decimal import Decimal
from decimal import ROUND_CEILING
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Protocol, Sequence
from types import MappingProxyType

from bot.autonomous.contracts import (
    EvidenceObservation,
    EvidenceRequest,
    MetricPayload,
    SourceEvent,
    content_hash,
)


_SHA256_LEN = 64
def is_source_issued_observation(observation: EvidenceObservation, registry: EvidenceRegistry | None) -> bool:
    """Verify durable issuance and the exact artifact against locally trusted definitions."""
    return registry is not None and registry.verify(observation)


@dataclass(frozen=True)
class SourceDefinition:
    """Caller-reviewed source allowlist; this is configuration, not discovery."""

    source_id: str
    primary: bool
    allowed_urls: tuple[str, ...]
    allowed_metrics: tuple[str, ...]
    metric_units: Mapping[str, tuple[str, ...]]
    adapter_identity: str
    max_age_us: int | None = None
    finality_policy: str | None = None
    chain_id: int | None = None
    contract: str | None = None
    instrument: str | None = None
    accounting_max_lag_us: int | None = None

    def __post_init__(self) -> None:
        if self.chain_id is not None and (type(self.chain_id) is not int or self.chain_id <= 0 or not isinstance(self.contract, str) or not self.contract):
            raise ValueError("pinned chain source requires a chain ID and contract")
        if self.chain_id is None and self.contract is not None:
            raise ValueError("pinned contract requires its chain identity")
        if self.instrument is not None and (not isinstance(self.instrument, str) or not self.instrument):
            raise ValueError("invalid pinned instrument")
        object.__setattr__(self, "metric_units", MappingProxyType({key: tuple(value) for key, value in self.metric_units.items()}))

    @property
    def binding_hash(self) -> str:
        return hashlib.sha256(_canonical({
            "source_id": self.source_id,
            "primary": self.primary,
            "allowed_urls": tuple(sorted(self.allowed_urls)),
            "allowed_metrics": tuple(sorted(self.allowed_metrics)),
            "metric_units": {key: tuple(sorted(value)) for key, value in sorted(self.metric_units.items())},
            "adapter_identity": self.adapter_identity,
            "max_age_us": self.max_age_us,
            "finality_policy": self.finality_policy,
            "chain_id": self.chain_id,
            "contract": self.contract,
            "instrument": self.instrument,
            "accounting_max_lag_us": self.accounting_max_lag_us,
        }).encode("utf-8")).hexdigest()


class EvidenceRegistry:
    """Versioned local research registry, separate from the execution ledger.

    The caller owns ``artifact_root`` and may supply an explicit database path
    beneath it. Each operation opens/closes its own SQLite connection; writes
    use a FULL-synchronous atomic transaction. Trusted source definitions are
    supplied locally again after restart, never promoted from database content.
    """

    def __init__(self, artifact_root: Path, definitions: Sequence[SourceDefinition], *,
                 database_path: Path | None = None) -> None:
        self.artifact_root = Path(artifact_root).resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.database_path = Path(database_path) if database_path is not None else self.artifact_root / "evidence-registry-v1.sqlite3"
        if self.database_path.is_symlink() or self.database_path.resolve().parent != self.artifact_root:
            raise ValueError("evidence registry database must be directly inside its owned artifact root")
        self._definitions: dict[str, SourceDefinition] = {}
        for definition in definitions:
            self.trust(definition)
        newly_created = not self.database_path.exists()
        with closing(self._connect(allow_empty_creation=newly_created)) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1) or (version == 0 and not newly_created):
                raise ValueError("unsupported evidence registry version")
            db.execute("CREATE TABLE IF NOT EXISTS issued (observation_hash TEXT PRIMARY KEY, definition_hash TEXT NOT NULL, raw_path TEXT NOT NULL, raw_sha256 TEXT NOT NULL)")
            db.execute("PRAGMA user_version=1")

    def _connect(self, *, allow_empty_creation: bool = False) -> sqlite3.Connection:
        if self.database_path.is_symlink():
            raise ValueError("evidence registry database cannot be a symlink")
        db = sqlite3.connect(self.database_path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version != 1 and not (version == 0 and allow_empty_creation and not db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()):
                raise ValueError("unsupported evidence registry version")
        except BaseException:
            db.close()
            raise
        return db

    def trust(self, definition: SourceDefinition) -> None:
        self._definitions[definition.binding_hash] = definition

    def definition_for(self, metadata: Mapping[str, object]) -> SourceDefinition | None:
        return self._definitions.get(metadata.get("source_definition_hash"))

    def _record(self, issued: tuple[tuple[EvidenceRequest, "EvidenceEnvelope", EvidenceObservation], ...]) -> None:
        """Issue observations only as rebuilt from envelopes re-validated against trusted definitions.

        Private to ``EvidenceSource.fetch``; there is deliberately no public issuance API.
        """
        rows = []
        for request, envelope, observation in issued:
            definition = self._definitions.get(envelope.source_definition_hash)
            if definition is None or definition.source_id != request.source_id:
                raise ValueError("cannot register evidence without a trusted definition")
            EvidenceSource._validate_envelope(request, definition, envelope)
            if hashlib.sha256(envelope.raw_bytes).hexdigest() != envelope.raw_sha256:
                raise ValueError("envelope bytes differ from their declared digest")
            if _observation_from(envelope) != observation:
                raise ValueError("observation is not derived from its validated envelope")
            metadata, error = decode_verified_observation(observation)
            if error or metadata is None:
                raise ValueError("cannot register unverified evidence")
            rows.append((content_hash(observation), metadata["source_definition_hash"], metadata["raw_path"], metadata["raw_sha256"]))
        with closing(self._connect()) as db, db:
            db.executemany("INSERT OR IGNORE INTO issued VALUES (?, ?, ?, ?)", rows)

    def verify(self, observation: EvidenceObservation) -> bool:
        metadata, error = decode_verified_observation(observation)
        if error or metadata is None:
            return False
        with closing(self._connect()) as db:
            row = db.execute("SELECT definition_hash, raw_path, raw_sha256 FROM issued WHERE observation_hash=?", (content_hash(observation),)).fetchone()
        if row is None:
            return False
        definition = self._definitions.get(row[0])
        if definition is None or row != (metadata["source_definition_hash"], metadata["raw_path"], metadata["raw_sha256"]):
            return False
        if (metadata["chain_id"], metadata["contract"], metadata["instrument"]) != (definition.chain_id, definition.contract, definition.instrument):
            return False
        if (definition.source_id != metadata["source_id"] or not definition.primary
                or metadata["adapter_identity"] != definition.adapter_identity
                or metadata["source_url"] not in definition.allowed_urls
                or metadata["metric"] not in definition.allowed_metrics
                or metadata["unit"] not in definition.metric_units.get(metadata["metric"], ())
                or metadata["finality"] != definition.finality_policy
                or type(definition.max_age_us) is not int or definition.max_age_us <= 0
                or metadata["expires_at_us"] > metadata["event_at_us"] + definition.max_age_us):
            return False
        try:
            raw = self._read_artifact(row[1])
        except (OSError, ValueError):
            return False
        return hashlib.sha256(raw).hexdigest() == row[2]

    def _read_artifact(self, relative_path: str) -> bytes:
        parts = relative_path.split("/")
        if relative_path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("invalid registry artifact path")
        directory = os.open(self.artifact_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            handle = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(handle, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("artifact is not regular")
                return stream.read()
        finally:
            os.close(directory)


@dataclass(frozen=True)
class EvidenceEnvelope:
    """Lossless source capture metadata before conversion to boundary records.

    ``raw_bytes`` must be the exact bytes whose SHA-256 is recorded. Unknown
    publication time stays null. An upper-bound receipt is never promoted to an
    exact receipt. Every valid observation records all envelope metadata in its
    stable source-event key and binds it into ``methodology_hash``.
    """

    observation_id: str
    candidate_id: str
    source_id: str
    source_url: str
    primary: bool
    metric: str
    value: Decimal
    unit: str
    cutoff_us: int
    event_at_us: int
    publication_at_us: int | None
    request_started_at_us: int
    received_at_us: int | None
    received_at_upper_bound_us: int | None
    period_start_us: int | None
    period_end_us: int | None
    method: str
    parser_version: str
    raw_path: str
    raw_bytes: bytes
    raw_sha256: str
    finality: str | None
    chain_id: int | None
    contract: str | None
    instrument: str | None
    block_height: int | None
    block_hash: str | None
    expires_at_us: int
    valid: bool
    invalid_reason: str | None
    claim_status: str = "unknown"
    adapter_identity: str = ""
    source_definition_hash: str = ""
    forecast_start_us: int | None = None
    forecast_end_us: int | None = None
    forecast_coverage_complete: bool = False
    accounting_basis: str | None = None
    approval_subject_hash: str | None = None
    model_version: str | None = None

    @property
    def provenance(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "candidate_id": self.candidate_id,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "primary": self.primary,
            "metric": self.metric,
            "unit": self.unit,
            "cutoff_us": self.cutoff_us,
            "event_at_us": self.event_at_us,
            "publication_at_us": self.publication_at_us,
            "request_started_at_us": self.request_started_at_us,
            "received_at_us": self.received_at_us,
            "received_at_upper_bound_us": self.received_at_upper_bound_us,
            "period_start_us": self.period_start_us,
            "period_end_us": self.period_end_us,
            "method": self.method,
            "parser_version": self.parser_version,
            "raw_path": self.raw_path,
            "raw_sha256": self.raw_sha256,
            "finality": self.finality,
            "chain_id": self.chain_id,
            "contract": self.contract,
            "instrument": self.instrument,
            "block_height": self.block_height,
            "block_hash": self.block_hash,
            "expires_at_us": self.expires_at_us,
            "valid": self.valid,
            "invalid_reason": self.invalid_reason,
            "claim_status": self.claim_status,
            "adapter_identity": self.adapter_identity,
            "source_definition_hash": self.source_definition_hash,
            "forecast_start_us": self.forecast_start_us,
            "forecast_end_us": self.forecast_end_us,
            "forecast_coverage_complete": self.forecast_coverage_complete,
            "accounting_basis": self.accounting_basis,
            "approval_subject_hash": self.approval_subject_hash,
            "model_version": self.model_version,
        }

    @property
    def provenance_hash(self) -> str:
        return hashlib.sha256(_canonical(self.provenance).encode("utf-8")).hexdigest()

    @property
    def logical_key(self) -> str:
        # Contract identifiers are bounded ASCII. The lossless metadata itself
        # is carried in SourceEvent.stream; this key provides stable linkage.
        return f"{self.observation_id}:{self.provenance_hash[:32]}"


class Adapter(Protocol):
    adapter_identity: str
    def __call__(self, request: EvidenceRequest) -> Sequence[EvidenceEnvelope] | Awaitable[Sequence[EvidenceEnvelope]]: ...


@dataclass(frozen=True)
class SupplyEvent:
    amount: Decimal | None
    effective_at_us: int
    kind: str
    already_circulating: bool | None
    sellable: bool | None


def _quantity(value: object) -> bool:
    return type(value) is Decimal and value.is_finite() and value >= 0


_FORWARD_SUPPLY_HORIZON_US = 90 * 86400 * 1_000_000


def normalize_supply(*, total: Decimal | None, excluded: Mapping[str, Decimal | None],
                     required_exclusions: tuple[str, ...], events: tuple[SupplyEvent, ...],
                     cutoff_us: int, horizon_us: int, unit: str, complete: bool) -> dict[str, Decimal | None]:
    """Reconcile a same-block total/float and complete forward sellable schedule.

    Addresses/exclusion classes must be independently verified by the decoder.
    Buybacks to reusable reserves never reduce total supply. Forward supply is
    gross newly sellable inventory, including already-circulating locked tokens;
    promised future burns cannot offset a sellable issuance risk.
    """
    result: dict[str, Decimal | None] = dict(total_supply=None, circulating_supply=None, forward_supply=None)
    if unit != "token" or not _quantity(total):
        return result
    result["total_supply"] = total
    if (len(set(key.lower() for key in required_exclusions)) != len(required_exclusions)
            or set(excluded) != set(required_exclusions) or not all(_quantity(value) for value in excluded.values())):
        return result
    locked = sum(excluded.values(), Decimal(0))
    if locked > total:
        return result
    result["circulating_supply"] = total - locked
    # Forward supply is the admission unit token/90d: the horizon must be exactly 90 days.
    if complete is not True or type(cutoff_us) is not int or type(horizon_us) is not int or cutoff_us < 0 or horizon_us != _FORWARD_SUPPLY_HORIZON_US:
        return result
    forward = Decimal(0)
    seen: set[SupplyEvent] = set()
    for event in events:
        if (not _quantity(event.amount) or type(event.effective_at_us) is not int
                or type(event.already_circulating) is not bool or type(event.sellable) is not bool
                or event.kind not in {"unlock", "mint", "burn", "relock", "reserve_purchase", "redistribution"}
                or event in seen):
            return result
        seen.add(event)
        # Inclusive at the cutoff: an unlock at the snapshot block is conservatively still forward supply.
        if cutoff_us <= event.effective_at_us <= cutoff_us + horizon_us and event.sellable:
            if event.kind in {"unlock", "mint", "redistribution"}:
                forward += event.amount
    result["forward_supply"] = forward
    return result


def normalize_cashflow(*, gross_fees: Decimal | None, rebates: Decimal | None,
                       referrals: Decimal | None, incentives: Decimal | None,
                       expenses: Decimal | None, bad_debt: Decimal | None,
                       attributable_cash: Decimal | None, token_entitlement_verified: bool,
                       unit: str, complete: bool) -> dict[str, Decimal | None]:
    """Normalize one fully reconciled window. Offchain points are not cash.

    The source decoder must reconcile coverage and distinguish expenses from
    already-netted fields. Treasury distributions/purchases are not automatically
    attributable cash; enforceable entitlement or executed absorption must be proved.
    """
    result: dict[str, Decimal | None] = dict(fees=None, revenue=None, incentives=None, token_accrual=None)
    if unit != "USD" or complete is not True:
        return result
    if _quantity(gross_fees):
        result["fees"] = gross_fees
    if _quantity(incentives):
        result["incentives"] = incentives
    deductions = (rebates, referrals, incentives, expenses, bad_debt)
    if _quantity(gross_fees) and all(_quantity(value) for value in deductions):
        result["revenue"] = gross_fees - sum(deductions, Decimal(0))
    if token_entitlement_verified is True and _quantity(attributable_cash):
        result["token_accrual"] = attributable_cash
    return result


@dataclass(frozen=True)
class AssetClaim:
    asset: str
    economic_claim_id: str
    amount: Decimal | None
    usd_mark: Decimal | None
    quote_unit: str
    cutoff_us: int


def normalize_tvl(claims: tuple[AssetClaim, ...], *, cutoff_us: int, complete: bool) -> Decimal | None:
    """Value unique net economic claims; recursive/bridge duplicates block totals."""
    if complete is not True or not claims:
        return None
    seen: set[str] = set()
    total = Decimal(0)
    for claim in claims:
        if (not claim.asset or not claim.economic_claim_id or claim.economic_claim_id in seen
                or claim.quote_unit != "USD" or claim.cutoff_us != cutoff_us
                or not _quantity(claim.amount) or not _quantity(claim.usd_mark)):
            return None
        seen.add(claim.economic_claim_id)
        total += claim.amount * claim.usd_mark
    return total


@dataclass(frozen=True)
class HolderCluster:
    address: str
    amount: Decimal | None
    category: str
    entity_id: str | None
    clustering_proof: str | None


def normalize_concentration(holders: tuple[HolderCluster, ...], *, supply: Decimal | None,
                            complete: bool) -> Decimal | None:
    """Largest verified beneficial entity / reconciled eligible supply.

    A custody/bridge/escrow/treasury/vesting wallet is never silently assigned a
    beneficial owner. Resolve/exclude those holdings with proved denominator
    reconciliation upstream; unresolved clustering yields unknown.
    """
    if complete is not True or not _quantity(supply) or supply == 0:
        return None
    entities: dict[str, Decimal] = {}
    seen: set[str] = set()
    for holder in holders:
        if (not holder.address or holder.address.lower() in seen or not _quantity(holder.amount)
                or holder.category != "beneficial" or not holder.entity_id or not holder.clustering_proof):
            return None
        seen.add(holder.address.lower())
        entities[holder.entity_id] = entities.get(holder.entity_id, Decimal(0)) + holder.amount
    if sum(entities.values(), Decimal(0)) != supply:
        return None
    return max(entities.values(), default=Decimal(0)) / supply


class CapturedProtocolAdapter:
    """Offline adapter from one immutable contract/API capture and reviewed decoder.

    Decoder output contains only qualified normalized values and explicit units;
    unknowns are omitted, never emitted as zero. The decoder is caller-reviewed
    code bound to its adapter identity and source definition. This adapter makes
    no network request and cannot certify an unresolved live endpoint contract.
    """

    def __init__(self, template: EvidenceEnvelope,
                 decoder: Callable[[object], Mapping[str, tuple[Decimal | None, str]]], *,
                 adapter_identity: str, rpc_request_ids: tuple[int, ...] | None = None) -> None:
        self.template, self.decoder = template, decoder
        self.adapter_identity = adapter_identity
        self.rpc_request_ids = rpc_request_ids

    def __call__(self, request: EvidenceRequest) -> tuple[EvidenceEnvelope, ...]:
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate captured JSON key")
                result[key] = value
            return result
        document = json.loads(self.template.raw_bytes, parse_float=Decimal, object_pairs_hook=unique_keys)
        if self.rpc_request_ids is not None:
            expected = self.rpc_request_ids
            if (any(type(value) is not int for value in expected) or len(set(expected)) != len(expected)
                    or not isinstance(document, list)
                    or any(not isinstance(row, dict) or type(row.get("id")) is not int or "error" in row for row in document)
                    or len(document) != len(expected) or {row["id"] for row in document} != set(expected)):
                raise ValueError("ambiguous or incomplete RPC request/response identities")
        normalized = self.decoder(document)
        return tuple(replace(self.template, observation_id=f"{self.template.observation_id}-{metric}",
                     metric=metric, value=value, unit=unit)
                     for metric, (value, unit) in sorted(normalized.items())
                     if metric in request.metric_names and value is not None)


@dataclass(frozen=True)
class DecodedCandidateEvidence:
    candidate_id: str
    decoder_version: str
    raw_sha256: str
    metrics: Mapping[str, Decimal | None]
    disclosures: tuple[str, ...]
    event_at_us: int | None = None


# Exact versions reviewed against the preserved official captures. A changed
# document requires a new decoder review, rather than silently scraping numbers.
_PROTOCOL_DOCUMENTS = {
    "aave": ("93f2739651437e3af4c1ce71e0ababeee26c8629be9411c328fae146a453c566",
             ("documented_reusable_reserve_purchases_not_burn", "operative_budget_and_pause_require_execution_proof")),
    "hype": ("75e55504b1b887a89e1668a9544d09b013242747bcc29f8648a5410142a71258",
             ("fees_split_among_hlp_assistance_fund_and_deployers", "issuance_and_burn_transactions_unverified")),
    "pendle": ("fc2e0393693afd575d144e08cc700e2a4dfc32b9b0a4c03712042e6a76ecb5d3",
               ("historical_vesting_completion_not_current_float", "documented_terminal_emissions_not_measured_supply")),
    "aero": ("5bf4d28db92356f8fe156690b172920b72c6b1476d28f4da8dd09c6a626c1833",
             ("repository_spec_not_live_minter_state", "liquid_token_not_locked_voting_cashflow")),
    "ldo": ("35b3af9868e7aae490d7c6f0f5b6129a2b137c93fd427de2cd1381b9d90cf0cf",
            ("h1_report_is_historical_unaudited_disclosure", "nest_hurdle_and_deficit_not_current_execution_proof")),
    "ondo": ("b3ebc301e5f6c8c5cab7b03b47d2897096ba976e1a6ee5cf3b16d66537a11592",
             ("initial_supply_not_current_float_or_unlock_schedule", "product_aum_not_proven_token_cash_entitlement")),
}

# Concrete Coinbase captures admitted by the offline decoder. The ticker value
# is one historical last trade from a single venue; pinning bytes prevents a
# caller from relabeling an edited or refreshed quote as this capture.
_TICKER_CAPTURE_HASHES = {
    "aave": "f60f7e007e5cbf5a623da659799f3915f373519b6b7f2f1fe72546e86cd4a611",
    "hype": "9edd3db93dcb625a64b6e52730e98a30c9e0728387201aee5ef2e8ed875311c7",
    "pendle": "b79a8a72f048af267e569cefd3795a831f44880276222190d55811b77e0ae912",
    "aero": "f1795792a340dec9c94601e6f615db2763c3926294439f7a808df14655b10198",
    "ldo": "d2cfc3be7cc9c9946bba4ed4ac4cbaca9532701bc35e9d4608000c1f54795965",
    "ondo": "80f87739c57e2e89fba3e6546bd0bf99a8272a378625f951a851b3d4d7419dd2",
}
_PENDLE_SPENDLE_CAPTURE_HASH = "0a4db4e8883514dbdab8ea9af43735a1251f162a7676ea9ab53e465e12e4d73e"


def decode_candidate_capture(candidate_id: str, raw_bytes: bytes, *, format_version: str,
                             unit: str | None = None) -> DecodedCandidateEvidence:
    """Concrete offline decoders for the six captured source contracts.

    Official documents produce versioned mechanism disclosures only. Coinbase's
    captured ticker contract supplies a single USD last-trade price, never a
    synchronized valuation or executable depth. PENDLE epoch fields whose units,
    settlement and coverage were unqualified stay unknown. No decoder substitutes
    a source publication or API epoch for exact local receipt time.
    """
    from research.autonomous.valuation import REQUIRED_EVIDENCE
    if candidate_id not in _PROTOCOL_DOCUMENTS or not isinstance(raw_bytes, bytes):
        raise ValueError("unsupported candidate capture")
    metrics = {name: None for name in REQUIRED_EVIDENCE}
    digest = hashlib.sha256(raw_bytes).hexdigest()
    if format_version == "official-document-20261017-v1":
        expected, disclosures = _PROTOCOL_DOCUMENTS[candidate_id]
        if digest != expected or unit is not None:
            raise ValueError("unreviewed protocol document version or invented document unit")
        return DecodedCandidateEvidence(candidate_id, format_version, digest, MappingProxyType(metrics), disclosures)

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate source JSON key")
            result[key] = value
        return result
    document = json.loads(raw_bytes, parse_float=Decimal, object_pairs_hook=unique_keys)
    if not isinstance(document, dict):
        raise ValueError("unsupported protocol response schema")
    if format_version == "coinbase-ticker-v1":
        if digest != _TICKER_CAPTURE_HASHES[candidate_id]:
            raise ValueError("ticker bytes do not match the reviewed candidate capture")
        required = {"ask", "bid", "volume", "trade_id", "price", "size", "time"}
        if (unit != "USD/token" or not required <= set(document)
                or set(document) - required - {"rfq_volume"}
                or type(document["trade_id"]) is not int):
            raise ValueError("unsupported ticker schema or quote units")
        for name in ("price", "bid", "ask", "volume", "size"):
            if not isinstance(document[name], str) or not _quantity(Decimal(document[name])):
                raise ValueError("ticker quantities require nonnegative decimal strings")
        if Decimal(document["price"]) <= 0:
            raise ValueError("ticker price must be positive")
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z", document["time"])
        if match is None:
            raise ValueError("unsupported ticker event timestamp")
        event = datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        delta = event - datetime(1970, 1, 1, tzinfo=timezone.utc)
        fraction_us = int((Decimal("0." + (match[2] or "0")) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
        event_us = (delta.days * 86400 + delta.seconds) * 1_000_000 + fraction_us
        metrics["price"] = Decimal(document["price"])
        return DecodedCandidateEvidence(candidate_id, format_version, digest, MappingProxyType(metrics),
            ("single_venue_last_trade_only", "receipt_and_listing_depth_not_certified"), event_us)
    if candidate_id == "pendle" and format_version == "pendle-spendle-epochs-v1":
        if digest != _PENDLE_SPENDLE_CAPTURE_HASH:
            raise ValueError("PENDLE epoch bytes do not match the reviewed capture")
        history = document.get("sPendleHistoricalData")
        if unit is not None or not isinstance(history, dict):
            raise ValueError("unsupported PENDLE epoch contract")
        names = ("timestamps", "fees", "revenues", "buybackAmounts", "ptLoopingFees")
        if any(not isinstance(history.get(name), list) for name in names):
            raise ValueError("incomplete PENDLE epoch schema")
        times = history["timestamps"]
        if (not times or any(type(value) is not int or value < 0 for value in times)
                or len(set(times)) != len(times)
                or any(len(history[name]) != len(times) for name in names)):
            raise ValueError("ambiguous or incomplete PENDLE epoch coverage")
        for name in names[1:]:
            if any(type(value) not in (int, Decimal) or not _quantity(Decimal(value)) for value in history[name]):
                raise ValueError("invalid PENDLE epoch quantities")
        return DecodedCandidateEvidence(candidate_id, format_version, digest, MappingProxyType(metrics),
            ("epoch_units_and_settlement_unqualified", "pt_looping_fees_already_included_not_additive",
             "zero_api_purchase_field_does_not_prove_no_transactions", "staking_virtual_units_not_physical_float"))
    raise ValueError("unsupported candidate source-format version")


def _observation_from(envelope: "EvidenceEnvelope") -> EvidenceObservation:
    """The only construction of an issued observation from a validated envelope."""
    payload = MetricPayload(metric=envelope.metric, value=envelope.value, unit=envelope.unit)
    payload_hash = content_hash(payload)
    # Receipt is required for causal ingestion. Publication remains an
    # explicitly nullable item inside logical_key; it is never inferred
    # from period dates, response write time, or an upper bound.
    received = envelope.received_at_us
    assert received is not None
    known_at = max(received, envelope.publication_at_us or received)
    stream = f"evidence-v1|{_canonical(envelope.provenance)}"
    event = SourceEvent(
        event_id=envelope.observation_id,
        source=envelope.source_id,
        stream=stream,
        instrument=envelope.instrument,
        logical_key=envelope.logical_key,
        revision=0,
        event_at_us=envelope.event_at_us,
        received_at_us=received,
        known_at_us=known_at,
        payload=payload,
        payload_hash=payload_hash,
        sequence=envelope.block_height,
        block_hash=envelope.block_hash,
    )
    methodology_hash = envelope.provenance_hash
    finality_hash = (
        hashlib.sha256(envelope.finality.encode("utf-8")).hexdigest()
        if envelope.finality else None
    )
    return EvidenceObservation(
        observation_id=envelope.observation_id,
        candidate_id=envelope.candidate_id,
        source_event=event,
        metric=envelope.metric,
        value=envelope.value,
        unit=envelope.unit,
        methodology_hash=methodology_hash,
        source_url=envelope.source_url,
        finality_policy_hash=finality_hash,
    )


class EvidenceSource:
    """Allowlisted adapter registry that emits only verified causal observations."""

    def __init__(
        self,
        *,
        adapters: Mapping[str, Adapter],
        definitions: Mapping[str, SourceDefinition],
        artifact_root: Path,
        registry: EvidenceRegistry | None = None,
    ) -> None:
        self.adapters = dict(adapters)
        self.definitions = dict(definitions)
        self.artifact_root = Path(artifact_root).resolve()
        self.registry = registry or EvidenceRegistry(self.artifact_root, tuple(self.definitions.values()))
        if self.registry.artifact_root != self.artifact_root:
            raise ValueError("registry artifact root differs from source artifact root")
        for definition in self.definitions.values():
            self.registry.trust(definition)

    async def fetch(self, request: EvidenceRequest) -> tuple[EvidenceObservation, ...]:
        definition = self.definitions.get(request.source_id)
        adapter = self.adapters.get(request.source_id)
        if definition is None or adapter is None or definition.source_id != request.source_id:
            return ()
        if not definition.adapter_identity or getattr(adapter, "adapter_identity", None) != definition.adapter_identity:
            raise ValueError("adapter identity does not match local source definition")
        if not definition.primary:
            raise ValueError("secondary source cannot be configured as primary evidence")
        # A source outage is an unknown/missing value. It must not be converted
        # into a measured zero or substituted by a generic search result.
        try:
            fetched = adapter(request)
            if inspect.isawaitable(fetched):
                fetched = await fetched
            envelopes = tuple(fetched)
        except (OSError, TimeoutError, ConnectionError):
            return ()
        if not envelopes:
            return ()

        observations: list[EvidenceObservation] = []
        seen: set[tuple[str, str]] = set()
        # Validate the whole response before any observation can be issued.
        for envelope in envelopes:
            if envelope.source_id != request.source_id:
                raise ValueError("adapter returned an observation for another source")
            self._validate_envelope(request, definition, envelope)
            key = (envelope.candidate_id, envelope.metric)
            if key in seen:
                raise ValueError("duplicate source observation for candidate/metric")
            seen.add(key)
        for envelope in envelopes:
            self._preserve_raw(envelope)

            observations.append(_observation_from(envelope))
        self.registry._record(tuple((request, envelope, observation)
            for envelope, observation in zip(envelopes, observations)))
        return tuple(observations)

    def _preserve_raw(self, item: EvidenceEnvelope) -> None:
        """Persist exact captures without overwriting or following child symlinks.

        The artifact root is an explicitly configured local research directory.
        Every directory and file is opened relative to a verified directory fd;
        a raw path can never redirect a write outside that root.
        """
        parts = item.raw_path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("raw evidence requires a normalized relative path")
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        directory = os.open(self.artifact_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                    os.fsync(directory)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            try:
                output = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            except FileExistsError:
                existing = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(existing, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("raw evidence path is not a regular file")
                    if stream.read() != item.raw_bytes:
                        raise ValueError("raw evidence artifact conflicts with existing capture")
                return
            with os.fdopen(output, "wb") as stream:
                stream.write(item.raw_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
        finally:
            os.close(directory)

    @staticmethod
    def _validate_envelope(
        request: EvidenceRequest,
        definition: SourceDefinition,
        item: EvidenceEnvelope,
    ) -> None:
        if not definition.primary or not item.primary:
            raise ValueError("secondary evidence cannot be admitted as primary")
        if item.source_url not in definition.allowed_urls:
            raise ValueError("source URL is not allowlisted")
        if item.metric not in definition.allowed_metrics or item.metric not in request.metric_names:
            raise ValueError("source metric is not allowlisted/requested")
        if item.unit not in definition.metric_units.get(item.metric, ()):
            raise ValueError("metric unit is missing or inconsistent with the source contract")
        if item.adapter_identity != definition.adapter_identity or item.source_definition_hash != definition.binding_hash:
            raise ValueError("evidence envelope is not bound to its local source definition")
        if item.candidate_id != request.candidate_id:
            raise ValueError("candidate identity mismatch")
        if (item.chain_id, item.contract, item.instrument) != (definition.chain_id, definition.contract, definition.instrument):
            raise ValueError("chain/contract/instrument differs from pinned source identity")
        if (not item.valid or item.invalid_reason is not None or not item.observation_id
                or not item.unit or not item.method or not item.parser_version
                or not item.raw_path or item.raw_path.startswith("/") or not item.finality
                or ".." in item.raw_path.split("/") or not item.raw_bytes
                or type(item.value) is not Decimal or type(item.primary) is not bool
                or type(item.valid) is not bool):
            raise ValueError("evidence envelope is incomplete or invalid")
        digest = hashlib.sha256(item.raw_bytes).hexdigest()
        if len(item.raw_sha256) != _SHA256_LEN or digest != item.raw_sha256.lower():
            raise ValueError("raw evidence hash mismatch")
        if not item.value.is_finite():
            raise ValueError("non-finite evidence value")
        if item.received_at_us is None:
            raise ValueError("exact local receipt is required for causal evidence")
        if item.received_at_upper_bound_us is not None:
            if type(item.received_at_upper_bound_us) is not int or item.received_at_us > item.received_at_upper_bound_us:
                raise ValueError("invalid or inconsistent receipt upper bound")
        for name in ("period_start_us", "period_end_us"):
            value = getattr(item, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"invalid {name}")
        times = (item.cutoff_us, item.event_at_us, item.request_started_at_us,
                 item.received_at_us, item.expires_at_us)
        if any(type(value) is not int or value < 0 for value in times):
            raise ValueError("invalid evidence timestamp")
        if item.cutoff_us != request.cutoff_us or item.event_at_us > request.cutoff_us:
            raise ValueError("evidence cutoff mismatch or future observation")
        if item.request_started_at_us > item.received_at_us or item.received_at_us > request.cutoff_us:
            raise ValueError("noncausal request/receipt timestamps")
        if item.event_at_us > item.received_at_us:
            raise ValueError("capture precedes observation event")
        if item.publication_at_us is not None and (
            type(item.publication_at_us) is not int or item.publication_at_us < 0
            or item.publication_at_us > request.cutoff_us
        ):
            raise ValueError("publication timestamp is invalid or after cutoff")
        if item.publication_at_us is not None and item.publication_at_us > item.received_at_us:
            raise ValueError("capture precedes publication")
        if item.period_start_us is not None and item.period_end_us is not None:
            if item.period_start_us < 0 or item.period_end_us < item.period_start_us:
                raise ValueError("invalid evidence period")
            if item.period_end_us > item.event_at_us:
                raise ValueError("evidence period is after observation event")
        elif (item.period_start_us is None) != (item.period_end_us is None):
            raise ValueError("partial evidence period")
        if item.expires_at_us <= request.cutoff_us:
            raise ValueError("evidence is expired at valuation cutoff")
        if type(definition.max_age_us) is not int or definition.max_age_us <= 0:
            raise ValueError("source requires an explicit positive freshness limit")
        if request.cutoff_us - item.event_at_us > definition.max_age_us:
            raise ValueError("evidence is stale at valuation cutoff")
        if item.expires_at_us > item.event_at_us + definition.max_age_us:
            raise ValueError("evidence expiry exceeds source freshness horizon")
        if not definition.finality_policy or item.finality != definition.finality_policy:
            raise ValueError("evidence finality does not match source policy")
        if item.claim_status not in {
            "mechanism_confirmed", "execution_confirmed", "proposal", "estimate",
            "speculation", "unknown",
        }:
            raise ValueError("unsupported claim classification")
        if item.block_height is not None and (type(item.block_height) is not int or item.block_height < 0):
            raise ValueError("invalid block height")
        if item.chain_id is not None and (type(item.chain_id) is not int or item.chain_id <= 0):
            raise ValueError("invalid chain identity")
        if item.block_hash is not None and not item.block_hash:
            raise ValueError("invalid block hash")
        if any(value is not None for value in (item.chain_id, item.contract, item.block_height, item.block_hash)):
            if (item.chain_id is None or not item.contract or item.block_height is None
                    or not isinstance(item.block_hash, str)
                    or re.fullmatch(r"0x[0-9a-fA-F]{64}", item.block_hash) is None):
                raise ValueError("chain evidence requires complete canonical block identity")


def decode_verified_observation(observation: EvidenceObservation) -> tuple[dict[str, object] | None, str | None]:
    """Decode the lossless envelope after checking contract bindings and exact integer types."""
    try:
        prefix, encoded = observation.source_event.stream.split("|", 1)
        if prefix != "evidence-v1":
            return None, "untrusted_observation_metadata"
        data = json.loads(encoded)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None, "untrusted_observation_metadata"
    if not isinstance(data, dict):
        return None, "untrusted_observation_metadata"
    expected = set(EvidenceEnvelope.__dataclass_fields__) - {"value", "raw_bytes"}
    if set(data) != expected:
        return None, "untrusted_observation_metadata"
    digest = hashlib.sha256(_canonical(data).encode()).hexdigest()
    if (observation.methodology_hash != digest or observation.source_event.logical_key !=
            f"{data['observation_id']}:{digest[:32]}"):
        return None, "untrusted_observation_provenance"
    integer_fields = ("cutoff_us", "event_at_us", "request_started_at_us", "received_at_us", "expires_at_us")
    for field in integer_fields:
        if type(data[field]) is not int or data[field] < 0:
            return None, "untrusted_observation_times"
    for field in ("period_start_us", "period_end_us", "received_at_upper_bound_us", "publication_at_us"):
        if data[field] is not None and (type(data[field]) is not int or data[field] < 0):
            return None, "untrusted_observation_times"
    if (data["received_at_upper_bound_us"] is not None and
            data["received_at_us"] > data["received_at_upper_bound_us"]):
        return None, "untrusted_observation_times"
    if data["expires_at_us"] <= data["cutoff_us"] or data["event_at_us"] > data["cutoff_us"] or data["received_at_us"] > data["cutoff_us"]:
        return None, "untrusted_observation_causality"
    if (data["request_started_at_us"] > data["received_at_us"]
            or data["event_at_us"] > data["received_at_us"]
            or (data["publication_at_us"] is not None and data["publication_at_us"] > data["received_at_us"])):
        return None, "untrusted_observation_causality"
    if (data["period_start_us"] is None) != (data["period_end_us"] is None):
        return None, "untrusted_observation_period"
    if data["period_start_us"] is not None and not (
        data["period_start_us"] <= data["period_end_us"] <= data["event_at_us"]
    ):
        return None, "untrusted_observation_period"
    if (data["observation_id"] != observation.observation_id or data["candidate_id"] != observation.candidate_id
            or data["metric"] != observation.metric or data["unit"] != observation.unit
            or data["source_id"] != observation.source_event.source or data["source_url"] != observation.source_url
            or data["event_at_us"] != observation.source_event.event_at_us
            or data["received_at_us"] != observation.source_event.received_at_us
            or data["instrument"] != observation.source_event.instrument
            or data["block_height"] != observation.source_event.sequence
            or data["block_hash"] != observation.source_event.block_hash
            or observation.source_event.known_at_us != max(data["received_at_us"], data["publication_at_us"] or data["received_at_us"])
            or observation.finality_policy_hash != hashlib.sha256(str(data["finality"]).encode()).hexdigest()):
        return None, "untrusted_observation_binding"
    payload = observation.source_event.payload
    if (getattr(payload, "metric", None) != observation.metric or getattr(payload, "unit", None) != observation.unit
            or getattr(payload, "value", None) != observation.value or observation.source_event.payload_hash != content_hash(payload)):
        return None, "untrusted_observation_payload_binding"
    if data["primary"] is not True or data["valid"] is not True or data["invalid_reason"] is not None:
        return None, "untrusted_observation_provenance"
    return data, None


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

