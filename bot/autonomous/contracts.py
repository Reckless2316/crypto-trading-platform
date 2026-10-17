"""Immutable, strictly validated values at autonomous process boundaries.

USD amounts are signed int64 microdollars; timestamps are UTC microseconds.
Decimal wire values are canonical strings, never JSON floats. Construction is
strict; from_dict/from_json are the only conversions from untrusted wire data.
"""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import math
import re
import types
from dataclasses import dataclass, fields
from decimal import Context, Decimal, InvalidOperation, localcontext
from enum import Enum
from functools import lru_cache
from typing import Protocol, Self, Union, get_args, get_origin, get_type_hints, runtime_checkable


class ContractViolation(ValueError):
    """A boundary value is invalid; it cannot authorize an action."""


class PersistenceUnavailable(RuntimeError): pass
class ProviderUnavailable(RuntimeError): pass
class VenueUnavailable(RuntimeError): pass
class StateUnavailable(RuntimeError): pass
class TokenAccountingUnavailable(RuntimeError): pass
class AuthorityUnavailable(RuntimeError): pass


INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1
DECIMAL_PRECISION = 50
MAX_DECIMAL_DIGITS = 38
MAX_DECIMAL_SCALE = 38
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


def checked_money(value: int) -> int:
    if type(value) is not int or not INT64_MIN <= value <= INT64_MAX:
        raise ContractViolation("invalid USD microdollars")
    return value


def decimal_value(value: str) -> Decimal:
    if type(value) is not str or len(value) > 80 or not _DECIMAL.fullmatch(value):
        raise ContractViolation("expected bounded plain decimal text")
    try:
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            result = Decimal(value)
    except InvalidOperation as exc:
        raise ContractViolation("invalid decimal") from exc
    _checked_decimal(result)
    return result


def _checked_decimal(value: Decimal) -> None:
    if type(value) is not Decimal or not value.is_finite():
        raise ContractViolation("expected finite Decimal")
    digits = value.as_tuple().digits
    exponent = value.as_tuple().exponent
    if len(digits) > MAX_DECIMAL_DIGITS or exponent < -MAX_DECIMAL_SCALE or value.adjusted() >= MAX_DECIMAL_DIGITS:
        raise ContractViolation("decimal precision/scale exceeds 38")


def decimal_text(value: Decimal) -> str:
    _checked_decimal(value)
    if not value:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def check_scale(value: Decimal, scale: int) -> Decimal:
    _checked_decimal(value)
    if type(scale) is not int or not 0 <= scale <= MAX_DECIMAL_SCALE:
        raise ContractViolation("invalid instrument scale")
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        if value.as_tuple().exponent < -scale and value != value.quantize(Decimal(1).scaleb(-scale)):
            raise ContractViolation("instrument scale exceeded")
    return value


def to_microdollars(value: str, *, rounding: str | None = None) -> int:
    amount = decimal_value(value)
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        scaled = amount * 1_000_000
        if rounding is None:
            integer = scaled.to_integral_value()
            if scaled != integer:
                raise ContractViolation("microdollar conversion needs explicit rounding")
        else:
            if rounding not in ("ROUND_DOWN", "ROUND_UP", "ROUND_FLOOR", "ROUND_CEILING", "ROUND_HALF_EVEN"):
                raise ContractViolation("unsupported rounding policy")
            integer = scaled.to_integral_value(rounding=rounding)
        return checked_money(int(integer))


def _wire(value: object) -> object:
    if isinstance(value, Record):
        return {field.name: _wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if type(value) is Decimal:
        return decimal_text(value)
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ContractViolation("nonfinite JSON number")
        return value
    if type(value) in (list, tuple):
        return [_wire(item) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ContractViolation("JSON keys must be strings")
        return {key: _wire(item) for key, item in value.items()}
    raise ContractViolation("unsupported canonical value")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(_wire(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractViolation("duplicate JSON field")
        result[key] = value
    return result


def strict_json(raw: str) -> object:
    if type(raw) is not str or len(raw.encode("utf-8")) > 1_048_576:
        raise ContractViolation("invalid or oversized JSON")
    try:
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ContractViolation("nonfinite JSON")))
    except (ValueError, RecursionError) as exc:
        raise ContractViolation("invalid strict JSON") from exc


@lru_cache(maxsize=None)
def _hints(cls: type) -> dict[str, object]:
    return get_type_hints(cls)


def _typed(value: object, annotation: object, *, decode: bool = False) -> object:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        for option in args:
            try:
                return _typed(value, option, decode=decode)
            except (ValueError, TypeError):
                pass
        raise ContractViolation("value does not match declared union")
    if annotation is type(None):
        if value is not None:
            raise ContractViolation("expected null")
        return None
    if origin is tuple:
        if type(value) is not tuple and not (decode and type(value) is list):
            raise ContractViolation("expected immutable tuple")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_typed(item, args[0], decode=decode) for item in value)
        if len(value) != len(args):
            raise ContractViolation("tuple length mismatch")
        return tuple(_typed(item, kind, decode=decode) for item, kind in zip(value, args))
    if isinstance(annotation, type) and issubclass(annotation, Record):
        if type(value) is annotation:
            return value
        if decode and type(value) is dict:
            return annotation.from_dict(value)
        raise ContractViolation("expected typed record")
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if type(value) is annotation:
            return value
        if decode:
            try:
                return annotation(value)
            except (ValueError, TypeError):
                pass
        raise ContractViolation("invalid enum")
    if annotation is Decimal:
        if decode:
            return decimal_value(value)
        _checked_decimal(value)
        return value
    if annotation in (str, int, float, bool):
        if type(value) is not annotation:
            raise ContractViolation(f"expected {annotation.__name__}")
        if annotation is float and not math.isfinite(value):
            raise ContractViolation("nonfinite float")
        if annotation is int:
            checked_money(value)
        if annotation is str and (not value or len(value) > 4096 or "\x00" in value):
            raise ContractViolation("invalid bounded text")
        return value
    if isinstance(annotation, type) and getattr(annotation, "_is_protocol", False):
        if not isinstance(value, annotation):
            raise ContractViolation("dependency does not implement protocol")
        return value
    raise ContractViolation("unsupported declared boundary type")


class Record:
    """Shared validation and exact, recursively typed JSON round trips."""
    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            _typed(value, _hints(type(self))[field.name])
            if value is None:
                continue
            if field.name.endswith(("_hash", "_sha256")) and type(value) is str and not _HASH.fullmatch(value):
                raise ContractViolation(f"invalid SHA-256: {field.name}")
            if field.name.endswith(("_us", "_ns")) and (type(value) is not int or value < 0):
                raise ContractViolation(f"invalid timestamp/deadline: {field.name}")
            if field.name.endswith(("_id", "_key")) and type(value) is str and (len(value) > 256 or not value.isascii()):
                raise ContractViolation(f"invalid identifier: {field.name}")
            if field.name.endswith(("_confidence", "_probability")) and not 0 <= value <= 1:
                raise ContractViolation(f"invalid probability: {field.name}")
            if type(value) is Decimal and field.name not in ("native_delta", "units_delta", "net_return", "value") and value < 0:
                raise ContractViolation(f"negative magnitude: {field.name}")
        self._validate()

    def _validate(self) -> None:
        pass

    def to_dict(self) -> dict[str, object]:
        return _wire(self)

    def to_json(self) -> str:
        return canonical_bytes(self).decode("ascii")

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> Self:
        if type(value) is not dict:
            raise ContractViolation("expected JSON object")
        names = {field.name for field in fields(cls)}
        if value.keys() - names:
            raise ContractViolation("unknown JSON field")
        try:
            return cls(**{name: _typed(item, _hints(cls)[name], decode=True) for name, item in value.items()})
        except TypeError as exc:
            raise ContractViolation("missing or invalid field") from exc

    @classmethod
    def from_json(cls, raw: str) -> Self:
        return cls.from_dict(strict_json(raw))


class Mode(str, Enum):
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE = "live"

class Role(str, Enum):
    AUTHORITY = "authority"
    REFLEX = "reflex"
    BRAIN = "brain"
    DASHBOARD = "dashboard"

class Regime(str, Enum):
    TRENDING = "trending"
    MEAN_REVERTING = "mean_reverting"
    HIGH_VOL = "high_vol"
    CRISIS = "crisis"

class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


# Approved specification's immutable wire identities; independent of row order.
CANDIDATE_WIRE_IDS: tuple[tuple[str, int], ...] = (
    ("aave", 1), ("hype", 2), ("pendle", 3), ("aero", 4), ("ldo", 5), ("ondo", 6),
)


def candidate_wire_id(candidate_id: str) -> int:
    if type(candidate_id) is str:
        for internal_id, wire_id in CANDIDATE_WIRE_IDS:
            if candidate_id == internal_id:
                return wire_id
    raise ContractViolation("unknown candidate wire identity")

class AdvisoryRisk(str, Enum):
    SAFE = "safe"
    NEAR_LIMIT = "near_limit"
    REDUCE = "reduce"

class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

class IntentState(str, Enum):
    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"

class Permit(str, Enum):
    NONE = "none"
    ENTRY = "entry"
    REDUCE_ONLY = "reduce_only"

class ControlKind(str, Enum):
    ARM = "arm"
    DISARM = "disarm"
    HARD_KILL = "hard_kill"
    RENEW = "renew"
    CLEAR_DAILY = "clear_daily"
    ACTIVATE_RELEASE = "activate_release"

class ReceiptStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    REJECTED = "rejected"

class ReconcileReason(str, Enum):
    STARTUP = "startup"
    PERIODIC = "periodic"
    UNKNOWN_ORDER = "unknown_order"
    STREAM_GAP = "stream_gap"
    OPERATOR = "operator"


@dataclass(frozen=True)
class AccountRevision(Record):
    journal_sequence: int = 0
    version: int = 0
    def _validate(self):
        if min(self.journal_sequence, self.version) < 0:
            raise ContractViolation("negative revision")


@dataclass(frozen=True)
class CausalCutoffs(Record):
    candle_close_us: int
    knowledge_cutoff_us: int
    inference_deadline_mono_ns: int
    boot_id: str
    def _validate(self):
        if self.knowledge_cutoff_us < self.candle_close_us:
            raise ContractViolation("knowledge cutoff precedes close")


@dataclass(frozen=True)
class BookPayload(Record):
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    is_snapshot: bool = True
    def _validate(self):
        if self.is_snapshot and (not self.bids or not self.asks):
            raise ContractViolation("snapshot requires both book sides")
        if any(price <= 0 or qty < 0 or (self.is_snapshot and qty == 0) for price, qty in self.bids + self.asks):
            raise ContractViolation("invalid book levels")
        if len({p for p, _ in self.bids}) != len(self.bids) or len({p for p, _ in self.asks}) != len(self.asks):
            raise ContractViolation("duplicate book price")
        if tuple(sorted(self.bids, reverse=True)) != self.bids or tuple(sorted(self.asks)) != self.asks:
            raise ContractViolation("book sides must be ordered")
        if self.bids and self.asks and self.bids[0][0] >= self.asks[0][0]:
            raise ContractViolation("crossed book")


@dataclass(frozen=True)
class CandlePayload(Record):
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    interval_us: int = 60_000_000
    def _validate(self):
        if self.low <= 0 or not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high or self.interval_us <= 0:
            raise ContractViolation("invalid candle")


@dataclass(frozen=True)
class MetricPayload(Record):
    metric: str
    value: Decimal
    unit: str


@dataclass(frozen=True)
class SourceEvent(Record):
    event_id: str
    source: str
    stream: str
    instrument: str | None
    logical_key: str
    revision: int
    event_at_us: int
    received_at_us: int
    known_at_us: int
    payload: BookPayload | CandlePayload | MetricPayload
    payload_hash: str
    sequence: int | None = None
    block_hash: str | None = None
    supersedes_id: str | None = None
    def _validate(self):
        if self.revision < 0 or self.known_at_us < self.received_at_us or (self.sequence is not None and self.sequence < 0):
            raise ContractViolation("invalid event revision/knowledge/sequence")
        if content_hash(self.payload) != self.payload_hash:
            raise ContractViolation("payload hash mismatch")


@dataclass(frozen=True)
class Feature(Record):
    name: str
    value: float
    valid: bool


@dataclass(frozen=True)
class NumericSnapshot(Record):
    candidate_id: str
    instrument: str
    snapshot_hash: str
    cutoffs: CausalCutoffs
    features: tuple[Feature, ...]
    validity_mask: int
    lineage_hash: str
    account_revision: AccountRevision
    snapshot_id: str = "unpersisted"
    account_id: str = "paper"
    release_hash: str | None = None
    def _validate(self):
        if self.validity_mask < 0 or self.validity_mask >= 1 << len(self.features):
            raise ContractViolation("invalid feature mask")
        if len({f.name for f in self.features}) != len(self.features):
            raise ContractViolation("duplicate feature")
        if any(f.valid != bool(self.validity_mask & (1 << i)) for i, f in enumerate(self.features)):
            raise ContractViolation("feature validity mask mismatch")


@dataclass(frozen=True)
class SharedContext(Record):
    features: tuple[Feature, ...]
    validity_mask: int
    def _validate(self):
        if self.validity_mask < 0 or self.validity_mask >= 1 << len(self.features):
            raise ContractViolation("invalid shared validity mask")
        if any(f.valid != bool(self.validity_mask & (1 << i)) for i, f in enumerate(self.features)):
            raise ContractViolation("shared validity mismatch")


@dataclass(frozen=True)
class FieldDefinition(Record):
    name: str
    unit: str
    scale: Decimal
    precision: int
    directionality: str
    window: str
    minimum: float | None
    maximum: float | None
    validity_bit: int
    encoding: str = "numeric"
    def _validate(self):
        if not 0 <= self.precision <= 38 or self.scale <= 0 or self.validity_bit < 0:
            raise ContractViolation("invalid field definition")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ContractViolation("invalid field bounds")


@dataclass(frozen=True)
class FieldDictionary(Record):
    """Bit i marks slot i valid, least-significant bit first.

    Identity and mask slots are metadata and exempt from zero placeholders;
    every other invalid slot must be the fixed numeric zero placeholder.
    """
    version: int
    shared_fields: tuple[FieldDefinition, ...]
    candidate_fields: tuple[FieldDefinition, ...]
    def _validate(self):
        if self.version != 1 or len(self.shared_fields) != 17 or len(self.candidate_fields) != 16:
            raise ContractViolation("unsupported dictionary shape/version")
        for group in (self.shared_fields, self.candidate_fields):
            if tuple(f.validity_bit for f in group) != tuple(range(len(group))) or len({f.name for f in group}) != len(group):
                raise ContractViolation("invalid dictionary slots")
    @property
    def dictionary_hash(self) -> str:
        return content_hash(self)


def default_field_dictionary() -> FieldDictionary:
    shared = (
        ("dictionary_version", "version", "none", 1., 1.),
        ("candle_close_ms", "unix_ms", "instant", 0., None),
        ("btc_return_1h", "fraction", "1h", None, None),
        ("eth_return_1h", "fraction", "1h", None, None),
        ("btc_trend", "category", "1h", -1., 1.),
        ("eth_trend", "category", "1h", -1., 1.),
        ("dominance_change_7d", "fraction", "7d", None, None),
        ("stablecoin_change_7d", "fraction", "7d", None, None),
        ("funding_change", "fraction", "1h", None, None),
        ("oi_change_24h", "fraction", "24h", None, None),
        ("macro_minutes", "minutes", "instant", 0., None),
        ("volatility_percentile", "fraction", "release_pinned", 0., 1.),
        ("gross_fraction", "fraction", "instant", 0., None),
        ("drawdown", "fraction", "lifetime", 0., 1.),
        ("daily_loss", "fraction", "UTC_day", 0., 1.),
        ("cash_headroom", "fraction", "instant", 0., 1.),
        ("shared_validity_mask", "bits", "instant", 0., 131071.),
    )
    candidate = (
        ("candidate_id", "category", "none", 1., 6.),
        ("mid", "USD_per_base", "instant", 0., None),
        ("spread_bps", "bps", "instant", 0., None),
        ("imbalance", "fraction", "10bps_depth", -1., 1.),
        ("rv_60m", "unannualized_log_std", "60x1m", 0., None),
        ("rv_samples", "count", "60x1m", 0., 60.),
        ("return_1h", "fraction", "1h", None, None),
        ("base_inventory", "base", "instant", 0., None),
        ("inventory_usd", "USD", "instant", 0., None),
        ("eligible", "bool", "instant", 0., 1.),
        ("evidence_age_minutes", "minutes", "instant", 0., None),
        ("evidence_expiry_minutes", "minutes", "instant", 0., None),
        ("thesis_condition_bits", "bits", "instant", 0., 63.),
        ("position_headroom", "fraction", "instant", 0., 1.),
        ("estimated_roundtrip_cost_bps", "bps", "instant", 0., None),
        ("candidate_validity_mask", "bits", "instant", 0., 65535.),
    )
    def encoding(name, width):
        if name == "candidate_id":
            return "metadata;validity_exempt;1=aave,2=hype,3=pendle,4=aero,5=ldo,6=ondo"
        if name == "dictionary_version":
            return "metadata;validity_exempt;version=1"
        if "validity_mask" in name:
            return f"metadata;validity_exempt;bit_i=slot_i;lsb=slot0;width={width};invalid_measurement=0"
        if name == "thesis_condition_bits":
            return "six_condition_bitset;bits=0..5;lsb=condition0;unknown=0,invalid"
        if name.endswith("trend"):
            return "trend:-1=down,0=flat,1=up;unknown=0,invalid"
        return "numeric;unknown=0,invalid"
    def group(rows):
        return tuple(FieldDefinition(name, unit, Decimal("1"), 8 if unit not in ("bits", "category", "bool", "count", "version", "unix_ms") else 0,
            "signed" if low is None or low < 0 else "nonnegative", window, low, high, i,
            encoding(name, len(rows)))
            for i, (name, unit, window, low, high) in enumerate(rows))
    return FieldDictionary(1, group(shared), group(candidate))


@dataclass(frozen=True)
class TokenCount(Record):
    state_tokens: int
    tokenizer_id: str
    provider_id: str
    proof_method: str
    compatibility_hash: str
    def _validate(self):
        if self.state_tokens < 0 or self.proof_method not in ("pinned_provider_tokenizer", "provider_approved_upper_bound"):
            raise TokenAccountingUnavailable("unverified token accounting")


@runtime_checkable
class ProviderTokenCounter(Protocol):
    def count_state(self, canonical_state_json: str) -> TokenCount: ...


def _validate_wire_slots(values: list[int | float], definitions: tuple[FieldDefinition, ...]) -> int:
    """Validate versioned numeric domains and zero placeholders at the boundary."""
    mask = values[-1]
    if type(mask) is not int or not 0 <= mask < (1 << len(definitions)):
        raise ContractViolation("invalid validity mask type/width")
    for value, definition in zip(values, definitions):
        if definition.precision == 0:
            checked_money(value)
        if definition.minimum is not None and value < definition.minimum:
            raise ContractViolation(f"wire slot below minimum: {definition.name}")
        if definition.maximum is not None and value > definition.maximum:
            raise ContractViolation(f"wire slot above maximum: {definition.name}")
        if "validity_exempt" not in definition.encoding and not (mask & (1 << definition.validity_bit)) and value != 0:
            raise ContractViolation(f"invalid measurement must be zero: {definition.name}")
    return mask


@dataclass(frozen=True)
class EncodedState(Record):
    canonical_state_json: str
    state_hash: str
    field_dictionary_hash: str
    candidate_ids: tuple[str, ...]
    token_count: TokenCount
    snapshots: tuple[NumericSnapshot, ...]
    def _validate(self):
        parsed = strict_json(self.canonical_state_json)
        if canonical_bytes(parsed).decode("ascii") != self.canonical_state_json or content_hash(parsed) != self.state_hash:
            raise ContractViolation("noncanonical/mismatched state")
        if not 0 < len(self.candidate_ids) <= 5 or len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ContractViolation("invalid included candidates")
        if tuple(s.candidate_id for s in self.snapshots) != self.candidate_ids or self.token_count.state_tokens >= 400:
            raise ContractViolation("snapshot order/token budget mismatch")
        if type(parsed) is not list or len(parsed) != 2:
            raise ContractViolation("state must contain shared array and candidate rows")
        shared, rows = parsed
        numeric = lambda value: type(value) in (int, float) and (type(value) is int or math.isfinite(value))
        if type(shared) is not list or len(shared) != 17 or shared[0] != 1 or not all(numeric(v) for v in shared):
            raise ContractViolation("invalid complete shared state")
        if type(rows) is not list or len(rows) != len(self.candidate_ids):
            raise ContractViolation("candidate row count mismatch")
        dictionary = default_field_dictionary()
        _validate_wire_slots(shared, dictionary.shared_fields)
        for candidate_id, row, snapshot in zip(self.candidate_ids, rows, self.snapshots):
            if type(row) is not list or len(row) != 16 or type(row[0]) is not int or row[0] != candidate_wire_id(candidate_id) or not all(numeric(v) for v in row[1:]):
                raise ContractViolation("invalid complete candidate row/order")
            if _validate_wire_slots(row, dictionary.candidate_fields) != snapshot.validity_mask:
                raise ContractViolation("candidate wire/snapshot validity mask mismatch")


@dataclass(frozen=True)
class Question(Record):
    candidate_id: str
    kind: str
    prompt_hash: str
    labels: tuple[str, ...]
    horizon_candles: int = 4
    def _validate(self):
        if self.kind not in ("regime", "direction", "setup_quality", "risk_state", "toxic_flow") or self.horizon_candles != 4:
            raise ContractViolation("invalid question")


@dataclass(frozen=True)
class QuestionBatch(Record):
    state_hash: str
    bundle_hash: str
    schema_hash: str
    questions: tuple[Question, ...]


@dataclass(frozen=True)
class JudgeRequest(Record):
    request_id: str
    state_hash: str
    bundle_hash: str
    model_id: str
    deadline_mono_ns: int
    boot_id: str


@dataclass(frozen=True)
class DirectionDistribution(Record):
    long: float
    short: float
    neutral: float
    def _validate(self):
        if any(not 0 <= p <= 1 for p in (self.long, self.short, self.neutral)) or abs(self.long + self.short + self.neutral - 1) > 1e-6:
            raise ContractViolation("invalid complete direction distribution")


@dataclass(frozen=True)
class CandidateDecision(Record):
    candidate_id: str
    regime: Regime
    direction: Direction
    setup_quality: int
    risk_state: AdvisoryRisk
    direction_probabilities: DirectionDistribution
    direction_confidence: float
    regime_confidence: float
    quality_confidence: float
    risk_confidence: float
    noul_probability: float
    def _validate(self):
        if not 0 <= self.setup_quality <= 3:
            raise ContractViolation("invalid setup quality")
        probabilities = self.direction_probabilities
        if getattr(probabilities, self.direction.value) < max(probabilities.long, probabilities.short, probabilities.neutral) - 1e-6:
            raise ContractViolation("selected direction is not a maximizer")
    @property
    def toxic_flow(self) -> bool:
        return self.noul_probability >= 0.50


@dataclass(frozen=True)
class DecisionBatch(Record):
    request_id: str
    state_hash: str
    bundle_hash: str
    model_id: str
    deadline_mono_ns: int
    received_mono_ns: int
    received_at_us: int
    decisions: tuple[CandidateDecision, ...]
    raw_response_hash: str
    rejection_reason: str | None = None
    def _validate(self):
        if len({d.candidate_id for d in self.decisions}) != len(self.decisions):
            raise ContractViolation("duplicate candidate decision")
        if self.decisions and (self.rejection_reason is not None or self.received_mono_ns > self.deadline_mono_ns):
            raise ContractViolation("rejected/late batch cannot contain decisions")
        if not self.decisions and self.rejection_reason is None:
            raise ContractViolation("empty judgment batch needs a rejection reason")


@dataclass(frozen=True)
class CalibrationBin(Record):
    lower: float
    upper: float
    conservative_probability: float
    independent_samples: int
    def _validate(self):
        if not 0 <= self.lower < self.upper <= 1 or self.independent_samples <= 0:
            raise ContractViolation("invalid calibration bin")


@dataclass(frozen=True)
class CalibrationArtifact(Record):
    artifact_hash: str
    model_id: str
    schema_hash: str
    dictionary_hash: str
    label_hash: str
    cost_hash: str
    instruments: tuple[str, ...]
    regimes: tuple[Regime, ...]
    training_cutoff_us: int
    valid_from_us: int
    expires_at_us: int
    mapping: tuple[CalibrationBin, ...]
    independent_samples: int
    win_per_dollar: Decimal
    loss_per_dollar: Decimal
    uncertainty_passed: bool
    tail_checks_passed: bool
    cost_convention: str = "net_of_all_costs"
    def _validate(self):
        if not self.training_cutoff_us < self.valid_from_us < self.expires_at_us or self.independent_samples <= 0 or not self.mapping or not self.instruments or not self.regimes:
            raise ContractViolation("invalid calibration support/window")
        if self.win_per_dollar <= 0 or self.loss_per_dollar <= 0 or self.cost_convention != "net_of_all_costs":
            raise ContractViolation("invalid calibrated payoff convention")


@dataclass(frozen=True)
class EdgeEstimate(Record):
    calibrated_probability: float
    win_per_dollar: Decimal
    loss_per_dollar: Decimal
    cost_convention: str
    valid_until_us: int
    artifact_hash: str
    supported_input_hash: str
    def _validate(self):
        if self.win_per_dollar <= 0 or self.loss_per_dollar <= 0 or self.cost_convention != "net_of_all_costs":
            raise ContractViolation("invalid edge payoffs")


@dataclass(frozen=True)
class Balance(Record):
    asset: str
    quantity: Decimal
    cost_basis_usd_u: int
    def _validate(self):
        if self.cost_basis_usd_u < 0:
            raise ContractViolation("negative cost basis")


@dataclass(frozen=True)
class Holding(Record):
    instrument: str
    base_asset: str
    quantity: Decimal
    cost_basis_usd_u: int
    liquidation_value_usd_u: int
    valuation_at_us: int
    lineage_hash: str
    def _validate(self):
        if min(self.cost_basis_usd_u, self.liquidation_value_usd_u) < 0:
            raise ContractViolation("negative holding value/basis")


@dataclass(frozen=True)
class Reservation(Record):
    intent_id: str
    cash_usd_u: int
    base_quantity: Decimal
    gross_usd_u: int
    loss_budget_usd_u: int
    remaining_quantity: Decimal
    state: IntentState
    version: int = 0
    def _validate(self):
        if min(self.cash_usd_u, self.gross_usd_u, self.loss_budget_usd_u, self.version) < 0 or self.remaining_quantity > self.base_quantity:
            raise ContractViolation("negative reservation")


# At most 4096 decimal digits per reduced component, ~13.6 kbits. Supports
# thousands of ordinary flows while bounding GCD/cross-product memory. Chunked
# conversion never depends on Python's process-wide int/string digit setting.
def _ratio_integer(text: str) -> int:
    if type(text) is not str or not 1 <= len(text) <= 4096:
        raise ContractViolation("exact ratio resource bound")
    negative=text.startswith('-')
    digits=text[1:] if negative else text
    if not digits or not digits.isascii() or not digits.isdigit() or (len(digits)>1 and digits[0]=='0') or text=='-0':
        raise ContractViolation("noncanonical exact integer")
    result=0
    for start in range(0,len(digits),9):
        part=digits[start:start+9]
        result=result*10**len(part)+int(part)
    return -result if negative else result


def _ratio_text(value: int) -> str:
    if value.bit_length()>13607:
        raise ContractViolation("exact ratio resource bound")
    sign='-' if value<0 else ''
    value=abs(value)
    chunks=[]
    while value:
        value,part=divmod(value,10**9)
        chunks.append(part)
    text=sign+(str(chunks[-1])+''.join(f'{p:09d}' for p in reversed(chunks[:-1])) if chunks else '0')
    if len(text)>4096:
        raise ContractViolation("exact ratio resource bound")
    return text


@dataclass(frozen=True)
class ExactRatio(Record):
    numerator: str
    denominator: str
    def _validate(self):
        n,d=_ratio_integer(self.numerator),_ratio_integer(self.denominator)
        if d<=0 or math.gcd(n,d)!=1:
            raise ContractViolation("exact ratio must be reduced with positive denominator")
    def fraction(self) -> Fraction:
        return Fraction(_ratio_integer(self.numerator),_ratio_integer(self.denominator))
    def projection(self) -> Decimal:
        """Bounded floor display, never the unit or loss authority."""
        value=self.fraction()
        with localcontext(Context(prec=38,rounding='ROUND_FLOOR')):
            projected=Decimal(value.numerator)/Decimal(value.denominator)
            if projected.as_tuple().exponent < -38:
                projected=projected.quantize(Decimal('1e-38'))
            decimal_text(projected)
            return projected
    @classmethod
    def of(cls,value: Fraction) -> ExactRatio:
        return cls(_ratio_text(value.numerator),_ratio_text(value.denominator))


@dataclass(frozen=True)
class ExactNavEvidence(Record):
    account_id: str
    mark_id: str
    revision: AccountRevision
    valuation_version: int
    equity_usd_u: int
    units: ExactRatio
    nav: ExactRatio
    high_water_nav: ExactRatio
    day_open_nav: ExactRatio
    lineage_hash: str
    def _validate(self):
        u,n,h,d=(v.fraction() for v in (self.units,self.nav,self.high_water_nav,self.day_open_nav))
        if self.valuation_version<0 or min(u,n,h,d)<=0 or h<n or n*u!=Fraction(self.equity_usd_u,1000000):
            raise ContractViolation("inconsistent exact NAV evidence")


@dataclass(frozen=True)
class AccountView(Record):
    account_id: str
    mode: Mode = Mode.PAPER
    revision: AccountRevision = AccountRevision()
    liquidation_equity_usd_u: int = 0
    units: Decimal = Decimal("0")
    nav: Decimal = Decimal("0")
    high_water_nav: Decimal = Decimal("0")
    day_open_nav: Decimal = Decimal("0")
    balances: tuple[Balance, ...] = ()
    holdings: tuple[Holding, ...] = ()
    reservations: tuple[Reservation, ...] = ()
    open_intent_ids: tuple[str, ...] = ()
    unknown_intent_ids: tuple[str, ...] = ()
    reconcile_id: str | None = None
    reconciled_at_us: int | None = None
    private_heartbeat_us: int | None = None
    marked_at_us: int | None = None
    ready: bool = False
    exact_nav: ExactNavEvidence | None = None
    def _validate(self):
        if self.ready and (self.liquidation_equity_usd_u <= 0 or min(self.units, self.nav, self.high_water_nav, self.day_open_nav) <= 0 or self.reconcile_id is None or self.reconciled_at_us is None or self.marked_at_us is None or self.private_heartbeat_us is None):
            raise ContractViolation("ready account lacks reconciled positive baselines")
        if self.ready and self.exact_nav is None:
            raise ContractViolation("ready account requires exact NAV evidence")
        if self.exact_nav is not None and (self.exact_nav.account_id != self.account_id or self.exact_nav.revision != self.revision or self.exact_nav.equity_usd_u != self.liquidation_equity_usd_u):
            raise ContractViolation("exact NAV evidence is not bound to account revision/equity")
        if self.exact_nav is not None and any(getattr(self,key)!=getattr(self.exact_nav,key).projection() for key in ('units','nav','high_water_nav','day_open_nav')):
            raise ContractViolation("display projection disagrees with exact NAV evidence")
        if self.high_water_nav < self.nav:
            raise ContractViolation("high water NAV below current NAV")
        if len({b.asset for b in self.balances}) != len(self.balances):
            raise ContractViolation("duplicate balance")


@dataclass(frozen=True)
class MarketRules(Record):
    venue: str
    instrument: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    lot_size: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal
    fee_bound: Decimal
    max_price_precision: int
    capability_hash: str
    expires_at_us: int
    spot_allowed: bool = False
    limit_ioc_verified: bool = False
    book_at_us: int | None = None
    available_buy_notional_usd_u: int = 0
    def _validate(self):
        if self.tick_size <= 0 or self.lot_size <= 0 or not 0 <= self.fee_bound <= 1 or not 0 <= self.max_price_precision <= 38:
            raise ContractViolation("invalid market increments/fees")
        if self.spot_allowed and self.quote_asset != "USD":
            raise ContractViolation("USD quote required; stablecoins are distinct assets")


@dataclass(frozen=True)
class DecisionReference(Record):
    decision_id: str
    request_id: str
    snapshot_hash: str
    release_hash: str


@dataclass(frozen=True)
class OrderProposal(Record):
    action_id: str
    account_id: str
    candidate_id: str
    instrument: str
    side: Side
    base_quantity: Decimal
    limit_price: Decimal
    max_fee_usd_u: int
    release_hash: str
    decision: DecisionReference | None
    candle_close_us: int
    action_slot: str
    reducing_only: bool
    control_epoch: int
    deadline_mono_ns: int
    loss_budget_usd_u: int = 0
    def _validate(self):
        if self.base_quantity <= 0 or self.limit_price <= 0 or min(self.max_fee_usd_u, self.loss_budget_usd_u, self.control_epoch) < 0:
            raise ContractViolation("invalid bounded order size/buffer")
        if self.side is Side.SELL and not self.reducing_only:
            raise ContractViolation("spot sells must reduce holdings")
        if not self.reducing_only and self.decision is None:
            raise ContractViolation("entries require decision reference")
        if self.decision is not None and self.release_hash != self.decision.release_hash:
            raise ContractViolation("decision release mismatch")


@dataclass(frozen=True)
class PreparedIntent(Record):
    intent_id: str
    proposal: OrderProposal
    reservation: Reservation
    client_order_id: str
    generation: int
    account_revision: AccountRevision
    state: IntentState = IntentState.RESERVED
    def _validate(self):
        if self.reservation.intent_id != self.intent_id or self.generation < 0:
            raise ContractViolation("invalid committed reservation")


@dataclass(frozen=True)
class ControlState(Record):
    mode: Mode = Mode.PAPER
    permit: Permit = Permit.NONE
    hard_killed: bool = False
    drawdown_latched: bool = False
    daily_halted: bool = False
    crisis_halted: bool = False
    control_epoch: int = 0
    boot_id: str | None = None
    deadman_deadline_mono_ns: int | None = None
    active_release_hash: str | None = None
    reconciliation_ready: bool = False
    armed: bool = False
    def _validate(self):
        if self.control_epoch < 0:
            raise ContractViolation("negative control epoch")
        if self.armed and (self.permit is Permit.NONE or self.boot_id is None or self.deadman_deadline_mono_ns is None or self.active_release_hash is None or not self.reconciliation_ready or self.hard_killed):
            raise ContractViolation("armed control lacks explicit permit/readiness")


@dataclass(frozen=True)
class RiskLimits(Record):
    version: int = 1
    mode: Mode = Mode.PAPER
    armed: bool = False
    candidate_eligible: bool = False
    max_drawdown: Decimal = Decimal("0.15")
    max_daily_loss: Decimal = Decimal("0.02")
    max_position_fraction: Decimal = Decimal("0.05")
    max_gross_fraction: Decimal = Decimal("0.20")
    max_entry_loss_fraction: Decimal = Decimal("0.0025")
    max_pending_loss_fraction: Decimal = Decimal("0.01")
    kelly_fraction: Decimal = Decimal("0.25")
    jev_deadline_ms: int = 1000
    max_snapshot_age_ms: int = 2000
    max_book_age_ms: int = 2000
    private_heartbeat_ms: int = 5000
    reconciliation_age_ms: int = 15000
    imbalance_band_bps: Decimal = Decimal("10")
    volatility_returns: int = 60
    outcome_horizon_candles: int = 4
    noul_threshold: float = 0.50
    jev_retries: int = 0
    max_candidates: int = 5
    max_state_tokens: int = 399
    live_config_hash: str | None = None
    tokenizer_config_hash: str | None = None
    calibration_config_hash: str | None = None
    def _validate(self):
        fixed = {"version": 1, "max_drawdown": Decimal("0.15"), "max_daily_loss": Decimal("0.02"),
            "max_position_fraction": Decimal("0.05"), "max_gross_fraction": Decimal("0.20"),
            "max_entry_loss_fraction": Decimal("0.0025"), "max_pending_loss_fraction": Decimal("0.01"),
            "kelly_fraction": Decimal("0.25"), "jev_deadline_ms": 1000, "max_snapshot_age_ms": 2000,
            "max_book_age_ms": 2000, "private_heartbeat_ms": 5000, "reconciliation_age_ms": 15000,
            "imbalance_band_bps": Decimal("10"), "volatility_returns": 60, "outcome_horizon_candles": 4,
            "noul_threshold": 0.50, "jev_retries": 0, "max_candidates": 5, "max_state_tokens": 399}
        if any(getattr(self, name) != value for name, value in fixed.items()):
            raise ContractViolation("limits differ from approved immutable release defaults")
        if self.armed and not (self.candidate_eligible and self.tokenizer_config_hash and self.calibration_config_hash and (self.mode is not Mode.LIVE or self.live_config_hash)):
            raise ContractViolation("missing promotion configuration disables arming")
    @property
    def exposure_enabled(self) -> bool:
        return bool(self.armed and self.candidate_eligible and self.tokenizer_config_hash and self.calibration_config_hash and self.mode is not Mode.SHADOW and (self.mode is not Mode.LIVE or self.live_config_hash))


@dataclass(frozen=True)
class RiskVerdict(Record):
    allowed: bool
    reason_codes: tuple[str, ...]
    account_revision: AccountRevision
    proposal_hash: str
    halt_transitions: tuple[str, ...] = ()
    escalation_codes: tuple[str, ...] = ()
    def _validate(self):
        if not self.allowed and not self.reason_codes:
            raise ContractViolation("refusal requires reason")


@dataclass(frozen=True)
class PolicyResult(Record):
    propose: bool
    reason_codes: tuple[str, ...]
    account_revision: AccountRevision
    snapshot_hash: str
    proposal: OrderProposal | None = None
    halt_transitions: tuple[str, ...] = ()
    escalation_codes: tuple[str, ...] = ()
    def _validate(self):
        if self.propose != (self.proposal is not None) or (not self.propose and not self.reason_codes):
            raise ContractViolation("policy result/proposal mismatch")


@dataclass(frozen=True)
class SubmitOrderCommand(Record):
    request_id: str
    proposal: OrderProposal
    expected_revision: AccountRevision
    generation: int
    decision: DecisionReference | None
    def _validate(self):
        if self.generation < 0 or self.decision != self.proposal.decision:
            raise ContractViolation("submit decision/generation mismatch")


@dataclass(frozen=True)
class OrderReference(Record):
    account_id: str
    intent_id: str
    client_order_id: str
    venue_order_id: str | None = None


@dataclass(frozen=True)
class CancelOrderCommand(Record):
    request_id: str
    reference: OrderReference
    control_epoch: int


@dataclass(frozen=True)
class ControlCommand(Record):
    request_id: str
    account_id: str
    kind: ControlKind
    actor: str
    reason: str
    expected_epoch: int
    boot_id: str
    deadline_mono_ns: int
    release_hash: str | None = None


@dataclass(frozen=True)
class IntentReceipt(Record):
    request_id: str
    status: ReceiptStatus
    intent_id: str | None = None
    state: IntentState | None = None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ControlReceipt(Record):
    request_id: str
    status: ReceiptStatus
    control_epoch: int
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconcileReport(Record):
    reconcile_id: str
    account_id: str
    reason: ReconcileReason
    started_at_us: int
    finished_at_us: int | None
    ready: bool
    differences: tuple[str, ...]
    account_revision: AccountRevision
    def _validate(self):
        if self.finished_at_us is not None and self.finished_at_us < self.started_at_us:
            raise ContractViolation("reconciliation finishes before start")
        if self.ready and (self.finished_at_us is None or self.differences):
            raise ContractViolation("unfinished/different reconciliation cannot be ready")


@dataclass(frozen=True)
class VenueOrderResult(Record):
    reference: OrderReference
    state: IntentState
    filled_quantity: Decimal
    remaining_quantity: Decimal
    received_at_us: int
    venue_event_key: str | None = None
    reason_code: str | None = None


@dataclass(frozen=True)
class VenueAccountSnapshot(Record):
    account_id: str
    venue: str
    balances: tuple[Balance, ...]
    open_orders: tuple[VenueOrderResult, ...]
    received_at_us: int
    cursor: str | None = None


@dataclass(frozen=True)
class FillCursor(Record):
    account_id: str
    after_trade_id: str | None = None


@dataclass(frozen=True)
class Fill(Record):
    fill_id: str
    account_id: str
    intent_id: str
    venue: str
    venue_trade_id: str
    venue_order_id: str
    instrument: str
    side: Side
    base_quantity: Decimal
    quote_quantity: Decimal
    price: Decimal
    fee_asset: str
    fee_quantity: Decimal
    event_at_us: int
    received_at_us: int
    payload_hash: str
    def _validate(self):
        if min(self.base_quantity, self.quote_quantity, self.price) <= 0:
            raise ContractViolation("invalid fill magnitude")


@dataclass(frozen=True)
class FillPage(Record):
    fills: tuple[Fill, ...]
    next_cursor: FillCursor
    complete: bool


@dataclass(frozen=True)
class OrderEvent(Record):
    event_id: str
    intent_id: str
    kind: IntentState
    result: VenueOrderResult
    received_at_us: int
    event_at_us: int | None = None
    venue_event_key: str | None = None
    def _validate(self):
        if self.intent_id != self.result.reference.intent_id or self.kind != self.result.state:
            raise ContractViolation("order event reference/state mismatch")


@dataclass(frozen=True)
class AppendResult(Record):
    appended: bool
    record_id: str
    account_revision: AccountRevision


@dataclass(frozen=True)
class SessionCutoff(Record):
    account_id: str
    cutoff_us: int
    reconcile_id: str


@dataclass(frozen=True)
class ManifestEntry(Record):
    record_id: str
    content_hash: str


@dataclass(frozen=True)
class SessionArchive(Record):
    session_id: str
    cutoff: SessionCutoff
    account_revision: AccountRevision
    database_export_hash: str
    event_manifest: tuple[ManifestEntry, ...]
    forecast_manifest: tuple[ManifestEntry, ...]
    outcome_manifest: tuple[ManifestEntry, ...]
    artifact_manifest: tuple[ManifestEntry, ...]
    manifest_hash: str


@dataclass(frozen=True)
class EvidenceRequest(Record):
    candidate_id: str
    source_id: str
    metric_names: tuple[str, ...]
    cutoff_us: int


@dataclass(frozen=True)
class EvidenceObservation(Record):
    observation_id: str
    candidate_id: str
    source_event: SourceEvent
    metric: str
    value: Decimal
    unit: str
    methodology_hash: str
    source_url: str
    finality_policy_hash: str | None = None


@dataclass(frozen=True)
class EvidenceManifest(Record):
    candidate_id: str
    observations: tuple[EvidenceObservation, ...]
    methodology_hash: str
    cutoff_us: int
    expires_at_us: int
    eligible: bool = False
    missing_fields: tuple[str, ...] = ()
    def _validate(self):
        if self.expires_at_us <= self.cutoff_us or any(o.candidate_id != self.candidate_id or o.source_event.known_at_us > self.cutoff_us or o.source_event.received_at_us > self.cutoff_us or o.source_event.event_at_us > self.cutoff_us for o in self.observations):
            raise ContractViolation("noncausal/expired evidence manifest")
        if self.eligible and (self.missing_fields or not self.observations):
            raise ContractViolation("incomplete evidence cannot grant eligibility")
    @property
    def manifest_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True)
class CandidateDefinition(Record):
    candidate_id: str
    instrument: str
    eligible: bool = False
    research_only: bool = False
    condition_names: tuple[str, ...] = ()
    evidence_manifest_hash: str | None = None
    def _validate(self):
        if self.eligible and (self.research_only or self.candidate_id.lower() == "ondo" or self.evidence_manifest_hash is None):
            raise ContractViolation("research-only/missing evidence candidate cannot be eligible")


@dataclass(frozen=True)
class ThesisAssessment(Record):
    candidate_id: str
    eligible: bool
    condition_bits: int
    evidence_manifest_hash: str
    reason_codes: tuple[str, ...]
    expires_at_us: int
    def _validate(self):
        if self.condition_bits < 0 or (self.eligible and self.candidate_id.lower() == "ondo"):
            raise ContractViolation("invalid thesis eligibility")


@dataclass(frozen=True)
class ReviewJob(Record):
    job_id: str
    dedupe_key: str
    kind: str
    payload_hash: str
    attempts: int = 0
    session_id: str | None = None
    snapshot_id: str | None = None
    decision_id: str | None = None
    next_attempt_us: int | None = None
    status: ReceiptStatus = ReceiptStatus.PENDING
    def _validate(self):
        if self.attempts < 0:
            raise ContractViolation("negative attempt count")


@dataclass(frozen=True)
class EvaluationSplit(Record):
    dataset_manifest_hash: str
    split_manifest_hash: str
    holdout_id: str
    train_end_us: int
    validation_start_us: int
    validation_end_us: int
    holdout_start_us: int
    holdout_end_us: int
    def _validate(self):
        if not self.train_end_us < self.validation_start_us <= self.validation_end_us < self.holdout_start_us <= self.holdout_end_us:
            raise ContractViolation("overlapping/nonchronological evaluation split")


@dataclass(frozen=True)
class EvaluationMetric(Record):
    name: str
    value: float
    sample_count: int
    def _validate(self):
        if self.sample_count < 0:
            raise ContractViolation("negative sample count")


@dataclass(frozen=True)
class EvaluationResult(Record):
    run_id: str
    bundle_hash: str
    split: EvaluationSplit
    metrics: tuple[EvaluationMetric, ...]
    passed: bool
    reason_codes: tuple[str, ...]
    manifest_hash: str


@dataclass(frozen=True)
class GateApproval(Record):
    gate: str
    actor: str
    bundle_hash: str
    evidence_hash: str
    approved_at_us: int
    def _validate(self):
        if self.gate not in ("specification", "architecture", "plan", "build", "review", "ship", "live_activation", "evaluation", "risk", "evidence", "tokenizer", "operator"):
            raise ContractViolation("unknown release gate")


@dataclass(frozen=True)
class CandidateBundle(Record):
    model_id: str
    schema_hash: str
    dictionary_hash: str
    tokenizer_hash: str
    policy_hash: str
    risk_hash: str
    calibrator_hash: str
    code_hash: str
    evidence_manifest_hash: str
    expires_at_us: int
    candidates: tuple[CandidateDefinition, ...]
    evaluation_manifests: tuple[ManifestEntry, ...]
    gate_approvals: tuple[GateApproval, ...] = ()
    parent_hash: str | None = None
    dependency_lock_hash: str | None = None
    def _validate(self):
        if not self.candidates or len({c.candidate_id for c in self.candidates}) != len(self.candidates):
            raise ContractViolation("empty/duplicate candidate bundle")
        if sum(c.eligible for c in self.candidates) > 5:
            raise ContractViolation("too many eligible candidates")
    @property
    def bundle_hash(self) -> str:
        return content_hash(self)


@dataclass(frozen=True)
class ApprovedBundle(Record):
    bundle: CandidateBundle
    approvals: tuple[GateApproval, ...]
    validated_at_us: int
    validation_manifest_hash: str
    def _validate(self):
        if not self.approvals or any(a.bundle_hash != self.bundle.bundle_hash for a in self.approvals) or self.validated_at_us >= self.bundle.expires_at_us:
            raise ContractViolation("invalid release approvals/window")
    @property
    def bundle_hash(self) -> str:
        return self.bundle.bundle_hash


@dataclass(frozen=True)
class ReviewProposal(Record):
    job_id: str
    parent_bundle_hash: str
    proposed_bundle: CandidateBundle
    evidence_manifest_hash: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ReviewReport(Record):
    session_id: str
    proposal: ReviewProposal | None
    evaluation: EvaluationResult | None
    reason_codes: tuple[str, ...]
    report_hash: str


@dataclass(frozen=True)
class WorkerConfig(Record):
    role: Role = Role.AUTHORITY
    mode: Mode = Mode.PAPER
    account_id: str = "paper-autonomous"
    runtime_root: str = "/var/lib/surge/autonomous"
    armed: bool = False
    candidate_eligible: bool = False
    release_hash: str | None = None
    def _validate(self):
        if self.armed:
            raise ContractViolation("worker always restarts unarmed")


@runtime_checkable
class WorkerService(Protocol):
    async def run(self, config: WorkerConfig) -> None: ...


@dataclass(frozen=True)
class WorkerDependencies(Record):
    service: WorkerService
    token_counter: ProviderTokenCounter | None = None


@dataclass(frozen=True)
class IngestResult(Record):
    accepted: bool
    event_id: str
    reason_codes: tuple[str, ...] = ()
    lineage_hash: str | None = None
    def _validate(self):
        if not self.accepted and not self.reason_codes:
            raise ContractViolation("rejected ingestion needs reason")


@dataclass(frozen=True)
class SizedProposal(Record):
    base_quantity: Decimal
    notional_usd_u: int
    loss_budget_usd_u: int
    account_revision: AccountRevision
    artifact_hash: str
    def _validate(self):
        if self.base_quantity <= 0 or min(self.notional_usd_u, self.loss_budget_usd_u) <= 0:
            raise ContractViolation("nonpositive sized proposal")


@dataclass(frozen=True)
class Refusal(Record):
    reason_code: str
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class Hold(Refusal): pass
@dataclass(frozen=True)
class RiskRejection(Refusal): pass
@dataclass(frozen=True)
class SizeRejection(Refusal): pass
@dataclass(frozen=True)
class UnavailableEdge(Refusal): pass
@dataclass(frozen=True)
class BundleRejection(Refusal): pass
@dataclass(frozen=True)
class PendingReconciliation(Refusal): pass
