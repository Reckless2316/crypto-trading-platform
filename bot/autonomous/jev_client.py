"""Offline, fail-closed client for the documented Typesafe JEV HTTP contract.

The module deliberately has no default transport or credential lookup: callers
must inject both, so unit coverage cannot accidentally call a provider.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import math
import time
from typing import Protocol

import httpx

from .contracts import (
    AdvisoryRisk, CandidateDecision, ChoiceDistribution, DecisionBatch,
    Direction, DirectionDistribution, EncodedState, JudgeRequest, Question,
    QuestionBatch, Regime, canonical_bytes, candidate_wire_id, content_hash,
    default_field_dictionary, strict_json, _encode_jev_audit,
)


_CHOICE_KINDS = ("regime", "direction", "setup_quality", "risk_state")
_QUESTION_KINDS = (*_CHOICE_KINDS, "toxic_flow")
_CHOICE_LABELS = {
    "regime": ("trending", "mean_reverting", "high_vol", "crisis"),
    "direction": ("long", "short", "neutral"),
    "setup_quality": ("0", "1", "2", "3"),
    "risk_state": ("safe", "near_limit", "reduce"),
    "toxic_flow": ("true", "false"),
}
_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_MAX_DEADLINE_NS = 1_000_000_000
_MAX_SNAPSHOT_AGE_US = 2_000_000
_MAX_RESPONSE_BYTES = 262_144
_TOLERANCE = 1e-6


@dataclass(frozen=True)
class QuestionRubric:
    kind: str
    instructions: str
    criteria: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CandidateQuestionSchema:
    schema_hash: str
    candidate_id: str
    rubrics: tuple[QuestionRubric, ...]


class SchemaResolver(Protocol):
    def resolve(self, schema_hash: str, candidate_id: str) -> CandidateQuestionSchema: ...


def _validate_rubric(schema_hash: str, candidate_id: str, schema: CandidateQuestionSchema) -> None:
    if type(schema) is not CandidateQuestionSchema or schema.schema_hash != schema_hash or schema.candidate_id != candidate_id:
        raise ValueError("schema mapping identity mismatch")
    if tuple(item.kind for item in schema.rubrics) != _QUESTION_KINDS:
        raise ValueError("schema must explicitly map all five JEV question kinds")
    for rubric in schema.rubrics:
        if (type(rubric.instructions) is not str or not rubric.instructions.strip()
                or tuple(label for label, _ in rubric.criteria) != _CHOICE_LABELS[rubric.kind]
                or any(type(text) is not str or not text.strip() for _, text in rubric.criteria)):
            raise ValueError("schema has an unmapped or malformed rubric")


def compile_questions(bundle, state: EncodedState, schema_resolver: SchemaResolver) -> QuestionBatch:
    """Compile exactly five explicit, row-bound questions per included candidate."""
    if type(state) is not EncodedState or schema_resolver is None:
        raise ValueError("encoded state and approved schema resolver are required")
    if not state.snapshots or any(not snapshot.snapshot_id or snapshot.snapshot_id == "unpersisted"
                                  for snapshot in state.snapshots):
        raise ValueError("all snapshots must be persisted before question compilation")
    approved = bundle.bundle
    dictionary = default_field_dictionary()
    if (state.token_count.state_tokens >= 400 or state.token_count.state_tokens < 0
            or state.field_dictionary_hash != approved.dictionary_hash
            or dictionary.dictionary_hash != approved.dictionary_hash
            or state.candidate_ids == () or len(state.candidate_ids) > 5):
        raise ValueError("state does not satisfy the pinned bundle contract")
    if any(snapshot.release_hash != bundle.bundle_hash for snapshot in state.snapshots):
        raise ValueError("state release does not match approved bundle")
    if content_hash(strict_json(state.canonical_state_json)) != state.state_hash:
        raise ValueError("state hash mismatch")
    bundle_candidates = {candidate.candidate_id: candidate for candidate in approved.candidates}
    if any(cid not in bundle_candidates or not bundle_candidates[cid].eligible for cid in state.candidate_ids):
        raise ValueError("included candidate is not eligible in the approved bundle")
    schema_hash = approved.schema_hash
    state_value = strict_json(state.canonical_state_json)
    if type(state_value) is not list or len(state_value) != 2 or type(state_value[1]) is not list:
        raise ValueError("invalid numeric state layout")
    rows = state_value[1]
    if len(rows) != len(state.candidate_ids):
        raise ValueError("candidate rows do not match state identity")
    eligible_slot = next(index for index, field in enumerate(dictionary.candidate_fields) if field.name == "eligible")
    field_description = "; ".join(
        f"{field.name}[unit={field.unit},window={field.window},encoding={field.encoding}]"
        for field in dictionary.shared_fields + dictionary.candidate_fields
    )
    questions = []
    for row_index, (candidate_id, row) in enumerate(zip(state.candidate_ids, rows)):
        if type(row) is not list or row[0] != candidate_wire_id(candidate_id):
            raise ValueError("numeric row identity mismatch")
        if row[eligible_slot] != 1:
            raise ValueError("candidate row is not marked eligible")
        schema = schema_resolver.resolve(schema_hash, candidate_id)
        _validate_rubric(schema_hash, candidate_id, schema)
        for rubric in schema.rubrics:
            question_id = f"{candidate_id}:{rubric.kind}"
            prompt = (
                f"Schema SHA-256: {schema_hash}. Candidate: {candidate_id}; "
                f"candidate row index: {row_index}. Read that candidate's numeric row only from the "
                "supplied numeric state; do not infer or substitute values. Evaluate only this candidate. "
                "Forecast horizon: the next four completed hourly candles. Numeric dictionary v1: " + field_description + ". "
                f"Rubric ({rubric.kind}): {rubric.instructions}"
            )
            questions.append(Question(candidate_id, rubric.kind, content_hash(prompt),
                                      _CHOICE_LABELS[rubric.kind], 4, question_id, prompt,
                                      rubric.criteria))
    if len(questions) != 5 * len(state.candidate_ids) or len(questions) > 25:
        raise ValueError("invalid JEV question count")
    return QuestionBatch(state.state_hash, bundle.bundle_hash, schema_hash, tuple(questions))


def validate_choice(probabilities: dict[str, float], choice: str, confidence: float) -> None:
    """Validate exact Choice consistency and the approved derived confidence."""
    if (type(probabilities) is not dict or len(probabilities) < 2
            or type(choice) is not str or choice not in probabilities):
        raise ValueError("invalid Choice options")
    if any(type(label) is not str or not label for label in probabilities):
        raise ValueError("invalid Choice labels")
    values = tuple(probabilities.values())
    if any(type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1 for p in values):
        raise ValueError("invalid probability")
    if abs(math.fsum(values) - 1.0) > _TOLERANCE or probabilities[choice] != max(values):
        raise ValueError("inconsistent Choice")
    expected = (max(values) - 1 / len(values)) / (1 - 1 / len(values))
    if (type(confidence) not in (int, float) or not math.isfinite(confidence)
            or not 0 <= confidence <= 1 or abs(confidence - expected) > _TOLERANCE):
        raise ValueError("inconsistent confidence")


def _object_no_duplicates(raw: bytes):
    try:
        return strict_json(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid provider JSON") from exc


def _exact_keys(value: object, keys: set[str], label: str) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise ValueError(f"invalid {label} fields")
    return value


def _usage(value: object) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    if type(value) is not dict or not set(value) <= {"input_tokens", "output_tokens"}:
        raise ValueError("invalid usage fields")
    counts = []
    for name in ("input_tokens", "output_tokens"):
        count = value.get(name)
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError("invalid provider usage")
        counts.append(count)
    return tuple(counts)


def _parse_answers(questions: QuestionBatch, answers: object) -> tuple[CandidateDecision, ...]:
    question_map = {question.question_id: question for question in questions.questions}
    if type(answers) is not dict or set(answers) != set(question_map):
        raise ValueError("missing or extra provider answers")
    grouped: dict[str, dict[str, object]] = {cid: {} for cid in dict.fromkeys(q.candidate_id for q in questions.questions)}
    for question_id, question in question_map.items():
        answer = answers[question_id]
        if question.kind == "toxic_flow":
            _exact_keys(answer, {"type", "noul"}, "Noul answer")
            probability = answer["noul"]
            if answer["type"] != "noul" or type(probability) not in (int, float) or not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError("invalid Noul answer")
            grouped[question.candidate_id][question.kind] = float(probability)
            continue
        _exact_keys(answer, {"type", "choice", "probabilities", "confidence"}, "Choice answer")
        if answer["type"] != "choice" or type(answer["probabilities"]) is not dict:
            raise ValueError("invalid Choice answer")
        probabilities = answer["probabilities"]
        if set(probabilities) != set(question.labels) or len(probabilities) != len(question.labels):
            raise ValueError("Choice labels differ from approved schema")
        validate_choice(probabilities, answer["choice"], answer["confidence"])
        normalized = {label: float(probabilities[label]) for label in question.labels}
        grouped[question.candidate_id][question.kind] = (answer["choice"], normalized, float(answer["confidence"]))
    result = []
    for candidate_id, values in grouped.items():
        if set(values) != set(_QUESTION_KINDS):
            raise ValueError("incomplete candidate question set")
        regime, rp, rc = values["regime"]
        direction, dp, dc = values["direction"]
        quality, qp, qc = values["setup_quality"]
        risk, kp, kc = values["risk_state"]
        result.append(CandidateDecision(
            candidate_id, Regime(regime), Direction(direction), int(quality), AdvisoryRisk(risk),
            DirectionDistribution(dp["long"], dp["short"], dp["neutral"]), dc, rc, qc, kc,
            values["toxic_flow"],
            ChoiceDistribution(_CHOICE_LABELS["regime"], tuple(rp[label] for label in _CHOICE_LABELS["regime"])),
            ChoiceDistribution(_CHOICE_LABELS["setup_quality"], tuple(qp[label] for label in _CHOICE_LABELS["setup_quality"])),
            ChoiceDistribution(_CHOICE_LABELS["risk_state"], tuple(kp[label] for label in _CHOICE_LABELS["risk_state"])),
        ))
    return tuple(result)


class JevClient:
    """One-attempt HTTPX client with injected transport.

    ``call_latency_ns`` measures local processing through audit preparation and
    the first immutable result validation. Its timestamp precedes the final
    timing-envelope copy; the deadline is checked again after that copy, so
    the metric is not an exact elapsed time to coroutine return.
    """
    def __init__(self, *, transport: httpx.AsyncBaseTransport, schema_resolver: SchemaResolver,
                 api_key: str, monotonic_ns=time.monotonic_ns, wall_clock_us=None,
                 endpoint: str = _ENDPOINT, total_deadline_ms: int = 1_000):
        if transport is None or schema_resolver is None or type(api_key) is not str or not api_key:
            raise ValueError("injected transport, schema resolver, and provider credential are required")
        if type(total_deadline_ms) is not int or not 1 <= total_deadline_ms <= 1_000:
            raise ValueError("JEV total deadline must be at most 1,000ms")
        self._transport = transport
        self._resolver = schema_resolver
        self._api_key = api_key
        self._monotonic_ns = monotonic_ns
        self._wall_clock_us = wall_clock_us or (lambda: time.time_ns() // 1000)
        self._endpoint = endpoint
        self._budget_ns = total_deadline_ms * 1_000_000

    async def judge(self, state: EncodedState, bundle, request: JudgeRequest) -> DecisionBatch:
        started = self._monotonic_ns()
        local_deadline = min(request.deadline_mono_ns, started + self._budget_ns,
                             *(snapshot.cutoffs.inference_deadline_mono_ns for snapshot in state.snapshots))
        raw_request = None
        raw_response = None
        provider_request_id = None
        provider_status = None
        input_tokens = output_tokens = None
        provider_model = None
        question_hash = None
        schema_hash = bundle.bundle.schema_hash
        rejection = "provider_unavailable"
        decisions = ()
        try:
            if (request.state_hash != state.state_hash or request.bundle_hash != bundle.bundle_hash
                    or request.model_id != bundle.bundle.model_id
                    or request.boot_id != state.snapshots[0].cutoffs.boot_id):
                raise ValueError("request identity mismatch")
            now_us = self._wall_clock_us()
            candle_close_us = state.snapshots[0].cutoffs.candle_close_us
            if now_us < candle_close_us or now_us - candle_close_us > _MAX_SNAPSHOT_AGE_US:
                raise ValueError("snapshot stale or from the future")
            if started >= local_deadline:
                raise ValueError("request deadline expired")
            questions = compile_questions(bundle, state, self._resolver)
            question_hash = content_hash(questions)
            body = {
                "model": request.model_id,
                "state": strict_json(state.canonical_state_json),
                "questions": {
                    question.question_id: {
                        "type": "noul" if question.kind == "toxic_flow" else "choice",
                        "instructions": question.prompt,
                        "criteria": dict(question.criteria),
                    }
                    for question in questions.questions
                },
            }
            raw_request = canonical_bytes(body)
            if len(raw_request) > _MAX_RESPONSE_BYTES:
                raw_request = None
                raise ValueError("provider request exceeds local size bound")
            latest_us = self._wall_clock_us()
            if latest_us < candle_close_us or latest_us - candle_close_us > _MAX_SNAPSHOT_AGE_US:
                raw_request = None
                raise ValueError("snapshot stale at dispatch")
            remaining = (local_deadline - self._monotonic_ns()) / 1_000_000_000
            if remaining <= 0:
                raw_request = None
                raise ValueError("request deadline expired before dispatch")
            # HTTPX has no retry policy enabled here; exactly one POST is issued.
            try:
                async with asyncio.timeout(remaining):
                    async with httpx.AsyncClient(transport=self._transport,
                            timeout=httpx.Timeout(remaining), trust_env=False) as client:
                        async with client.stream("POST", self._endpoint, content=raw_request,
                                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json",
                                         "Accept-Encoding": "identity"}) as response:
                            provider_status = response.status_code
                            provider_request_id = response.headers.get("x-typesafe-request-id")
                            content_encoding = response.headers.get("content-encoding", "identity").lower()
                            chunks = bytearray()
                            async for chunk in response.aiter_raw():
                                if len(chunks) + len(chunk) > _MAX_RESPONSE_BYTES:
                                    raise ValueError("provider response exceeds local size bound")
                                chunks.extend(chunk)
                            raw_response = bytes(chunks)
                            if content_encoding != "identity":
                                raise ValueError("compressed provider response is not accepted")
            except TimeoutError as exc:
                raise ValueError("provider call exceeded local deadline") from exc
            if provider_request_id is not None and (len(provider_request_id) > 256 or not provider_request_id.isascii()):
                provider_request_id = None
                raise ValueError("invalid provider request identifier")
            if self._monotonic_ns() > local_deadline:
                raise ValueError("provider response exceeded local deadline")
            if provider_status is None or provider_status < 200 or provider_status >= 300:
                raise ValueError(f"provider HTTP status {provider_status}")
            parsed = _object_no_duplicates(raw_response)
            if type(parsed) is not dict or set(parsed) not in ({"model", "answers"}, {"model", "answers", "usage"}):
                raise ValueError("invalid provider response fields")
            reported_model = parsed["model"]
            if type(reported_model) is not str or reported_model != request.model_id:
                raise ValueError("provider returned a different model")
            provider_model = reported_model
            input_tokens, output_tokens = _usage(parsed.get("usage"))
            decisions = _parse_answers(questions, parsed["answers"])
            if self._monotonic_ns() > local_deadline:
                decisions = ()
                raise ValueError("provider response validation exceeded local deadline")
            rejection = None
        except Exception as exc:
            # Transport exception text may contain headers or credentials. Keep
            # only the exception category in the durable rejection record.
            rejection = f"hold:{type(exc).__name__}"
            decisions = ()
        raw_request = raw_request or b""
        try:
            request_audit = _encode_jev_audit(raw_request) if raw_request else None
            response_audit = _encode_jev_audit(raw_response) if raw_response is not None else None
            request_hash = hashlib.sha256(raw_request).hexdigest() if raw_request else None
            response_hash = hashlib.sha256(raw_response).hexdigest() if raw_response is not None else None
        except Exception as exc:
            request_audit = response_audit = request_hash = response_hash = None
            raw_request = b""
            raw_response = None
            rejection = f"hold:audit_encoding:{type(exc).__name__}"
            decisions = ()
        received = self._monotonic_ns()
        if received > local_deadline:
            rejection = "hold:local_deadline"
            decisions = ()
        def batch_or_hold(final_decisions, final_rejection):
            try:
                return DecisionBatch(
                    request.request_id, request.state_hash, request.bundle_hash, request.model_id,
                    local_deadline, received, self._wall_clock_us(), final_decisions,
                    response_hash, final_rejection,
                    provider_request_id=provider_request_id, provider_status_code=provider_status,
                    usage_input_tokens=input_tokens, usage_output_tokens=output_tokens, boot_id=request.boot_id,
                    schema_hash=schema_hash, question_hash=question_hash, provider_model_id=provider_model,
                    request_body_hash=request_hash,
                    raw_request_length=len(raw_request) if raw_request else None,
                    raw_response_length=len(raw_response) if raw_response is not None else None,
                    raw_request_zlib_b64_chunks=request_audit,
                    raw_response_zlib_b64_chunks=response_audit,
                    request_started_mono_ns=started, call_latency_ns=received - started,
                )
            except Exception as exc:
                return DecisionBatch(
                    request.request_id, request.state_hash, request.bundle_hash, request.model_id,
                    local_deadline, received, self._wall_clock_us(), (), None,
                    f"hold:batch_validation:{type(exc).__name__}", boot_id=request.boot_id,
                    request_started_mono_ns=started, call_latency_ns=received - started,
                )
        batch = batch_or_hold(decisions, rejection)
        # Immutable audit validation is part of the local inference budget,
        # including bounded decompression/hash checks in DecisionBatch.
        received = self._monotonic_ns()
        if received > local_deadline:
            return batch_or_hold((), "hold:local_deadline")
        batch = replace(batch, received_mono_ns=received,
                        call_latency_ns=received - started)
        completed = self._monotonic_ns()
        if completed > local_deadline:
            received = completed
            return batch_or_hold((), "hold:local_deadline")
        return batch
