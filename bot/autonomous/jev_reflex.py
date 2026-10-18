"""Inert Jev REFLEX adapter for the paper runtime: one batched call per cycle, strict validation, fail-safe holds.

Jev only judges. It receives the numeric paper snapshots (computed in code) and returns, per instrument, the
typed fields regime / direction+confidence / toxic_flow / setup_quality / risk_state. Every threshold, size and
side effect stays in ``runtime.py``.

Gates before any request leaves the process:
  1. an API key is supplied (the CLI reads ``JEV_API_KEY``; nothing here reads the environment);
  2. ``<data-dir>/jev_schema.approved.json`` exists and its canonical hash equals the schema this adapter compiles
     (the operator approves by copying the proposed schema file; a model can never write it).
Any failure -- gate, transport, timeout, latency budget, malformed/extra/missing answer -- yields a reason string
instead of a Judgment, and the runtime HOLDS entries with ``REFLEX_UNAVAILABLE:<reason>``. There is no implicit
fallback to the offline reflex. Exactly one POST per cycle; no retries.

Vendor latency (~81 ms) and calibration are vendor claims, not measurements; the local budget is enforced here.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Callable

from .contracts import AdvisoryRisk, Direction, Regime, canonical_bytes, content_hash
from .jev_client import _CHOICE_LABELS, _ENDPOINT, _MAX_RESPONSE_BYTES, _QUESTION_KINDS, _exact_keys, _object_no_duplicates, _usage, validate_choice

DEFAULT_MODEL = "jev-1.14.0"  # documented exact version; account access unverified
DEFAULT_BUDGET_MS = 250
MAX_INSTRUMENTS = 5
STATE_TOKEN_LIMIT = 400
APPROVED_SCHEMA_FILE = "jev_schema.approved.json"
PROPOSED_SCHEMA_FILE = "jev_schema.proposed.json"

_FIELDS = ("c=candle close (unix microseconds); mid=midpoint price; spr=quoted spread in basis points; "
           "imb=depth imbalance within 10 bp of mid in [-1,1], positive means more bid depth; rv=realized volatility "
           "of one-candle log returns in basis points; mom=log return over the contiguous history in basis points; "
           "t=momentum t-statistic; n=contiguous history length in candles; inv=held inventory as a fraction of "
           "equity; dd=account drawdown from high water as a fraction; dl=account loss since the UTC day open as a fraction")

_RUBRICS = {
    "regime": ("Classify the market regime for this instrument from its numeric row.",
               {"trending": "persistent directional movement: |t| large relative to realized volatility",
                "mean_reverting": "no persistent direction: small |t|, moves revert",
                "high_vol": "realized volatility or spread elevated relative to normal for this instrument",
                "crisis": "disorderly market: extreme volatility, very wide spread, or account drawdown near limits"}),
    "direction": ("Forecast the direction of the executable net return over the stated horizon.",
                  {"long": "price likely to rise by more than round-trip costs",
                   "short": "price likely to fall by more than round-trip costs",
                   "neutral": "move likely smaller than round-trip costs"}),
    "setup_quality": ("Score the quality of an entry setup right now from 0 (none) to 3 (strong).",
                      {"0": "no setup", "1": "weak setup", "2": "acceptable setup", "3": "strong setup"}),
    "risk_state": ("Assess account and market risk for adding exposure in this instrument.",
                   {"safe": "risk normal: adding a small position is acceptable",
                    "near_limit": "risk elevated: inventory, drawdown or daily loss approaching limits",
                    "reduce": "risk excessive: exposure should be reduced"}),
    "toxic_flow": ("Is order flow toxic (informed, one-sided or manipulative) such that taking liquidity now is unsafe?",
                   {"true": "flow is toxic", "false": "flow is not toxic"}),
}


def compile_schema(model: str = DEFAULT_MODEL, *, horizon_candles: int = 60, candle_seconds: int = 60) -> dict:
    """The full question schema. Its canonical hash is what an operator approves."""
    return {"version": "paper-jev-schema-v1", "model": model, "fields": _FIELDS,
            "horizon": f"the next {horizon_candles} completed candles of {candle_seconds} seconds",
            "rubrics": {kind: {"instructions": text, "criteria": criteria} for kind, (text, criteria) in _RUBRICS.items()}}


def schema_hash(schema: dict) -> str:
    return content_hash(schema)


def write_proposed_schema(data_dir: Path, schema: dict) -> Path:
    """Write the proposal for operator review. Approval is the operator copying it to the approved name."""
    path = Path(data_dir) / PROPOSED_SCHEMA_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=2, sort_keys=True))
    return path


def http_transport(url: str, headers: dict, body: bytes, timeout_s: float) -> tuple[int, str | None, bytes]:
    """Single POST, no retries, no environment proxies. Used only after every gate has passed."""
    import httpx
    with httpx.Client(timeout=httpx.Timeout(timeout_s), trust_env=False) as client:
        response = client.post(url, content=body, headers=headers)
        return response.status_code, response.headers.get("x-typesafe-request-id"), response.content[:_MAX_RESPONSE_BYTES + 1]


class JevReflex:
    """Batched REFLEX port. ``judge_batch`` returns {instrument: Judgment | reason}; ``judge`` is unsupported."""

    def __init__(self, *, data_dir: Path, api_key: str | None, model: str = DEFAULT_MODEL,
                 transport: Callable[[str, dict, bytes, float], tuple[int, str | None, bytes]] | None = None,
                 budget_ms: int = DEFAULT_BUDGET_MS, monotonic_ns: Callable[[], int] = time.monotonic_ns,
                 horizon_candles: int = 60, candle_seconds: int = 60, endpoint: str = _ENDPOINT):
        if type(budget_ms) is not int or not 1 <= budget_ms <= 1_000:
            raise ValueError("Jev latency budget must be 1..1000 ms")
        self.schema = compile_schema(model, horizon_candles=horizon_candles, candle_seconds=candle_seconds)
        self.schema_hash = schema_hash(self.schema)
        self.model, self.api_key, self.data_dir = model, api_key, Path(data_dir)
        self.transport = transport or http_transport
        self.budget_ns = budget_ms * 1_000_000
        self.monotonic_ns, self.endpoint = monotonic_ns, endpoint
        self.reflex_id = f"jev:{model}:{self.schema_hash[:16]}"
        self.last_call: dict | None = None

    def judge(self, snapshot):
        raise NotImplementedError("JevReflex judges whole cycles via judge_batch")

    def gate(self) -> str | None:
        if not self.api_key:
            return "no_api_key"
        path = self.data_dir / APPROVED_SCHEMA_FILE
        if not path.exists():
            return "schema_not_approved"
        try:
            approved = json.loads(path.read_text())
        except (OSError, ValueError):
            return "schema_unreadable"
        if not isinstance(approved, dict) or schema_hash(approved) != self.schema_hash:
            return "schema_hash_mismatch"
        return None

    def questions(self, instruments: tuple[str, ...]) -> dict:
        out = {}
        for instrument in instruments:
            for kind in _QUESTION_KINDS:
                rubric = self.schema["rubrics"][kind]
                prompt = (f"Schema SHA-256: {self.schema_hash}. Instrument: {instrument}. Read only this instrument's row "
                          f"state.rows[{json.dumps(instrument)}]; do not infer or substitute values. Fields: {self.schema['fields']}. "
                          f"Horizon: {self.schema['horizon']}. Rubric ({kind}): {rubric['instructions']}")
                out[f"{instrument}:{kind}"] = {"type": "noul" if kind == "toxic_flow" else "choice",
                                               "instructions": prompt, "criteria": dict(rubric["criteria"])}
        return out

    def judge_batch(self, snapshots: dict) -> dict:
        from .runtime import Judgment
        instruments = tuple(sorted(snapshots))
        started = self.monotonic_ns()
        record = {"reflex_id": self.reflex_id, "questions": 5 * len(instruments), "outcome": None, "latency_ms": None,
                  "request_hash": None, "response_hash": None, "provider_request_id": None, "status": None}
        self.last_call = record

        def fail(reason):
            record["outcome"] = reason
            record["latency_ms"] = (self.monotonic_ns() - started) / 1e6
            return {i: reason for i in instruments}

        if not instruments:
            return {}
        if len(instruments) > MAX_INSTRUMENTS:
            return fail("too_many_instruments")
        if (reason := self.gate()) is not None:
            return fail(reason)
        rows = {i: {k: v for k, v in json.loads(snapshots[i].wire()).items() if k not in ("v", "i")} for i in instruments}
        state = {"rows": rows}
        if math.ceil(len(canonical_bytes(state)) / 3) >= STATE_TOKEN_LIMIT:  # ESTIMATE, as for single snapshots
            return fail("state_budget")
        questions = self.questions(instruments)
        body = canonical_bytes({"model": self.model, "state": state, "questions": questions})
        record["request_hash"] = hashlib.sha256(body).hexdigest()
        remaining_s = (started + self.budget_ns - self.monotonic_ns()) / 1e9
        if remaining_s <= 0:
            return fail("timeout")
        try:
            status, provider_id, raw = self.transport(self.endpoint, {"Authorization": f"Bearer {self.api_key}",
                                                      "Content-Type": "application/json", "Accept-Encoding": "identity"},
                                                      body, remaining_s)
        except TimeoutError:
            return fail("timeout")
        except Exception as exc:  # exception text may carry headers; keep only the category
            return fail("transport_" + type(exc).__name__)
        record["status"] = status
        record["provider_request_id"] = provider_id if isinstance(provider_id, str) and len(provider_id) <= 256 else None
        if self.monotonic_ns() - started > self.budget_ns:
            return fail("latency_budget")
        if not isinstance(raw, (bytes, bytearray)) or len(raw) > _MAX_RESPONSE_BYTES:
            return fail("response_size")
        record["response_hash"] = hashlib.sha256(raw).hexdigest()
        if type(status) is not int or not 200 <= status < 300:
            return fail(f"http_{status}")
        try:
            judgments = self._parse(bytes(raw), instruments, Judgment)
        except Exception as exc:
            return fail("invalid_answer_" + type(exc).__name__)
        if self.monotonic_ns() - started > self.budget_ns:
            return fail("latency_budget")
        record["outcome"] = "ok"
        record["latency_ms"] = (self.monotonic_ns() - started) / 1e6
        return judgments

    def _parse(self, raw: bytes, instruments: tuple[str, ...], judgment_type) -> dict:
        parsed = _object_no_duplicates(raw)
        if type(parsed) is not dict or set(parsed) not in ({"model", "answers"}, {"model", "answers", "usage"}):
            raise ValueError("invalid provider response fields")
        if parsed["model"] != self.model:
            raise ValueError("provider returned a different model")
        _usage(parsed.get("usage"))
        answers = parsed["answers"]
        expected = {f"{i}:{k}" for i in instruments for k in _QUESTION_KINDS}
        if type(answers) is not dict or set(answers) != expected:
            raise ValueError("missing or extra provider answers")
        out = {}
        for instrument in instruments:
            values = {}
            for kind in _QUESTION_KINDS:
                answer = answers[f"{instrument}:{kind}"]
                if kind == "toxic_flow":
                    _exact_keys(answer, {"type", "noul"}, "Noul answer")
                    p = answer["noul"]
                    if answer["type"] != "noul" or type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
                        raise ValueError("invalid Noul answer")
                    values[kind] = float(p)
                    continue
                _exact_keys(answer, {"type", "choice", "probabilities", "confidence"}, "Choice answer")
                probabilities = answer["probabilities"]
                if (answer["type"] != "choice" or type(probabilities) is not dict
                        or set(probabilities) != set(_CHOICE_LABELS[kind])):
                    raise ValueError("Choice labels differ from approved schema")
                validate_choice(probabilities, answer["choice"], answer["confidence"])
                values[kind] = (answer["choice"], float(answer["confidence"]))
            critical = min(values[k][1] for k in ("regime", "direction", "setup_quality", "risk_state"))
            out[instrument] = judgment_type(Regime(values["regime"][0]), Direction(values["direction"][0]),
                                            values["direction"][1], values["toxic_flow"] >= 0.5,  # approved .50 boundary
                                            int(values["setup_quality"][0]), AdvisoryRisk(values["risk_state"][0]),
                                            self.reflex_id, False, critical)
        return out
