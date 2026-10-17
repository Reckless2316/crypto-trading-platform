import asyncio
import unittest
import json
from pathlib import Path
import time
from unittest.mock import patch

import httpx

from dataclasses import replace
from bot.autonomous.contracts import JudgeRequest, StateHoldDraft, content_hash
from bot.autonomous.jev_client import (
    CandidateQuestionSchema, JevClient, QuestionRubric, compile_questions,
    validate_choice,
)
from tests.autonomous.test_state import StateEncodingHelpers


class SyntheticResolver:
    labels = {
        "regime": ("trending", "mean_reverting", "high_vol", "crisis"),
        "direction": ("long", "short", "neutral"),
        "setup_quality": ("0", "1", "2", "3"),
        "risk_state": ("safe", "near_limit", "reduce"),
        "toxic_flow": ("true", "false"),
    }

    def resolve(self, schema_hash, candidate_id):
        return CandidateQuestionSchema(schema_hash, candidate_id, tuple(
            QuestionRubric(kind, f"Synthetic test-only rubric for {kind}.",
                           tuple((label, f"Synthetic test-only criterion {label}.")
                                 for label in labels))
            for kind, labels in self.labels.items()
        ))


class AsyncMockTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler

    async def handle_async_request(self, request):
        response = await self.handler(request)
        content = response.content
        headers = dict(response.headers)
        status = response.status_code
        response.close()
        return httpx.Response(status, headers=headers, stream=AsyncBytes(content))


class AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        yield self.content

    async def aclose(self):
        return None


class JevContractTests(StateEncodingHelpers, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.resolver = SyntheticResolver()

    def state_and_bundle(self, candidate_id="aave", eligible_ids=None):
        ids = (candidate_id,)
        eligible_ids = ids if eligible_ids is None else eligible_ids
        bundle = self.ranked_bundle(ids, eligible_ids)
        encoded = self.persisted(self.encode(ids, eligible_ids))
        return encoded, bundle

    def persisted(self, encoded, deadline_ns=None):
        if type(encoded) is StateHoldDraft:
            return encoded
        deadline_ns = deadline_ns or time.monotonic_ns() + 5_000_000_000
        snapshots = tuple(replace(snapshot,
            snapshot_id=content_hash(("synthetic-persisted-snapshot", snapshot.candidate_id)),
            cutoffs=replace(snapshot.cutoffs, inference_deadline_mono_ns=deadline_ns))
            for snapshot in encoded.snapshots)
        return replace(encoded, snapshots=snapshots)

    def test_confidence_is_not_top_probability(self):
        probabilities = {"long": 0.9, "short": 0.05, "neutral": 0.05}
        validate_choice(probabilities, "long", 0.85)
        with self.assertRaises(ValueError):
            validate_choice(probabilities, "long", 0.9)

    def test_choice_rejects_unhashable_choice_labels(self):
        probabilities = {"long": 0.9, "short": 0.05, "neutral": 0.05}
        for choice in ([], {}):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                validate_choice(probabilities, choice, 0.85)

    def test_choice_rejects_bool_nonfinite_sum_and_nonmax_choice(self):
        for probabilities, choice, confidence in (
            ({"yes": True, "no": 0.0}, "yes", 1.0),
            ({"yes": float("nan"), "no": 1.0}, "yes", 1.0),
            ({"yes": 0.7, "no": 0.2}, "yes", 0.55),
            ({"yes": 0.7, "no": 0.3}, "no", 0.4),
        ):
            with self.subTest(probabilities=probabilities), self.assertRaises(ValueError):
                validate_choice(probabilities, choice, confidence)

    def test_choice_label_must_be_a_string(self):
        for choice in ([], {}, True, 1, None):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                validate_choice({"long": 0.9, "short": 0.05, "neutral": 0.05}, choice, 0.85)

    def test_compile_questions_includes_exact_five_per_included_candidate(self):
        for candidate in ("aave", "hype", "pendle", "aero", "ldo"):
            with self.subTest(candidate=candidate):
                state, bundle = self.state_and_bundle(candidate)
                batch = compile_questions(bundle, state, self.resolver)
                self.assertEqual(len(batch.questions), 5)
                self.assertEqual({q.kind for q in batch.questions}, {
                    "regime", "direction", "setup_quality", "risk_state", "toxic_flow"
                })
                for question in batch.questions:
                    self.assertIn(f"Candidate: {candidate}", question.prompt)
                    self.assertIn("candidate row index:", question.prompt)
                    self.assertIn("four completed hourly candles", question.prompt)
                    self.assertIn("Numeric dictionary v1:", question.prompt)
                    row = json.loads(state.canonical_state_json)[1][0]
                    from bot.autonomous.contracts import canonical_bytes
                    self.assertNotIn(canonical_bytes(row).decode("ascii"), question.prompt)
                    self.assertEqual(tuple(label for label, _ in question.criteria), question.labels)

    def test_unpersisted_snapshot_cannot_compile_or_post(self):
        state = self.encode(("aave",))
        bundle = self.ranked_bundle(("aave",))
        with self.assertRaisesRegex(ValueError, "persisted"):
            compile_questions(bundle, state, self.resolver)
        calls = []
        async def handler(request):
            calls.append(request)
            return httpx.Response(500)
        client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                           api_key="synthetic-test-only", wall_clock_us=lambda: 60_000_000)
        request = JudgeRequest("unpersisted", state.state_hash, bundle.bundle_hash,
                               bundle.bundle.model_id, time.monotonic_ns() + 1_000_000_000, "test-boot")
        result = asyncio.run(client.judge(state, bundle, request))
        self.assertEqual(calls, [])
        self.assertEqual(result.decisions, ())

    def test_five_candidates_compile_to_one_batch_of_twenty_five_questions(self):
        candidates = ("aave", "hype", "pendle", "aero", "ldo")
        state = self.persisted(self.encode(candidates))
        bundle = self.ranked_bundle(candidates)
        batch = compile_questions(bundle, state, self.resolver)
        self.assertEqual(len(batch.questions), 25)
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(503, content=b"synthetic unavailable")

        now = time.monotonic_ns()
        client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                           api_key="synthetic-test-only", wall_clock_us=lambda: 60_000_000)
        request = JudgeRequest("all-candidates", state.state_hash, bundle.bundle_hash,
                               bundle.bundle.model_id, now + 1_000_000_000, "test-boot")
        result = asyncio.run(client.judge(state, bundle, request))
        self.assertEqual(len(calls), 1)
        posted = json.loads(calls[0].content)
        self.assertEqual(len(posted["questions"]), 25)
        self.assertEqual(result.provider_status_code, 503)

    def test_unmapped_schema_or_ineligible_candidate_fails_closed(self):
        class MissingResolver:
            def resolve(self, schema_hash, candidate_id):
                raise LookupError("schema unmapped")

        state, bundle = self.state_and_bundle()
        with self.assertRaises(LookupError):
            compile_questions(bundle, state, MissingResolver())
        ondo_state, ondo_bundle = self.state_and_bundle("ondo", eligible_ids=())
        with self.assertRaises(ValueError):
            compile_questions(ondo_bundle, ondo_state, self.resolver)

    def test_injected_transport_posts_once_and_preserves_provider_audit(self):
        state, bundle = self.state_and_bundle()
        payload = json.loads((Path(__file__).parent / "fixtures" / "jev_responses.json").read_text())["response"]
        payload["model"] = bundle.bundle.model_id
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(200, json=payload, headers={"x-typesafe-request-id": "provider-123"})

        now = time.monotonic_ns()
        client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                           api_key="synthetic-test-only", wall_clock_us=lambda: 60_000_000)
        request = JudgeRequest("local-123", state.state_hash, bundle.bundle_hash,
                               bundle.bundle.model_id, now + 10_000_000_000, "test-boot")
        result = asyncio.run(client.judge(state, bundle, request))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.request_id, "local-123")
        self.assertEqual(result.provider_request_id, "provider-123")
        self.assertEqual(result.usage_input_tokens, None)
        self.assertEqual(len(result.decisions), 1)
        decision = result.decisions[0]
        self.assertEqual(decision.setup_quality, 2)
        self.assertTrue(decision.toxic_flow)
        self.assertEqual(decision.direction_confidence, 0.85)
        self.assertEqual(len(decision.regime_distribution.probabilities), 4)
        self.assertTrue(result.raw_request_bytes.startswith(b'{"model"'))
        self.assertIn(b'"answers"', result.raw_response_bytes)
        self.assertEqual(result.call_latency_ns, result.received_mono_ns - result.request_started_mono_ns)
        self.assertLessEqual(result.deadline_mono_ns, min(request.deadline_mono_ns,
                             state.snapshots[0].cutoffs.inference_deadline_mono_ns))

    def test_malformed_model_and_integer_json_numbers_fail_closed_or_normalize(self):
        state, bundle = self.state_and_bundle()
        path = Path(__file__).parent / "fixtures" / "jev_responses.json"
        template = json.loads(path.read_text())["response"]
        template["model"] = bundle.bundle.model_id
        malformed = json.loads(json.dumps(template))
        malformed["model"] = []

        async def run_payload(payload, request_id):
            async def handler(request):
                return httpx.Response(200, json=payload)
            now = time.monotonic_ns()
            client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                               api_key="synthetic-test-only", wall_clock_us=lambda: 60_000_000)
            request = JudgeRequest(request_id, state.state_hash, bundle.bundle_hash,
                                   bundle.bundle.model_id, now + 1_000_000_000, "test-boot")
            return await client.judge(state, bundle, request)

        held = asyncio.run(run_payload(malformed, "bad-model"))
        self.assertEqual(held.decisions, ())
        one_hot = json.loads(json.dumps(template))
        for answer in one_hot["answers"].values():
            if answer["type"] == "noul":
                answer["noul"] = 1
            else:
                chosen = answer["choice"]
                answer["probabilities"] = {label: int(label == chosen) for label in answer["probabilities"]}
                answer["confidence"] = 1
        accepted = asyncio.run(run_payload(one_hot, "int-values"))
        self.assertEqual(len(accepted.decisions), 1)
        self.assertIs(type(accepted.decisions[0].noul_probability), float)
        self.assertTrue(all(type(v) is float for v in accepted.decisions[0].regime_distribution.probabilities))

    def test_expired_frozen_snapshot_deadline_never_dispatches(self):
        state, bundle = self.state_and_bundle()
        state = self.persisted(state, time.monotonic_ns() - 1)
        calls = []
        async def handler(request):
            calls.append(request)
            return httpx.Response(200, json={})
        now = time.monotonic_ns()
        client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                           api_key="synthetic-test-only", wall_clock_us=lambda: 60_000_000)
        request = JudgeRequest("expired-snapshot", state.state_hash, bundle.bundle_hash,
                               bundle.bundle.model_id, now + 1_000_000_000, "test-boot")
        result = asyncio.run(client.judge(state, bundle, request))
        self.assertEqual(calls, [])
        self.assertEqual(result.deadline_mono_ns, state.snapshots[0].cutoffs.inference_deadline_mono_ns)
        self.assertEqual(result.decisions, ())

    def test_provider_error_is_hold_without_retry(self):
        state, bundle = self.state_and_bundle()
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(503, content=b"unavailable")

        now = time.monotonic_ns()
        client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                           api_key="synthetic-test-only", wall_clock_us=lambda: 60_000_000)
        request = JudgeRequest("local-503", state.state_hash, bundle.bundle_hash,
                               bundle.bundle.model_id, now + 1_000_000_000, "test-boot")
        result = asyncio.run(client.judge(state, bundle, request))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.decisions, ())
        self.assertTrue(result.rejection_reason.startswith("hold:"))

    def test_transport_error_does_not_copy_credential_into_rejection(self):
        state, bundle = self.state_and_bundle()
        credential = "synthetic-secret-must-not-enter-audit"

        async def handler(request):
            raise RuntimeError(f"Authorization: Bearer {credential}")

        now = time.monotonic_ns()
        client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                           api_key=credential, wall_clock_us=lambda: 60_000_000)
        request = JudgeRequest("transport-secret", state.state_hash, bundle.bundle_hash,
                               bundle.bundle.model_id, now + 1_000_000_000, "test-boot")
        result = asyncio.run(client.judge(state, bundle, request))
        self.assertEqual(result.decisions, ())
        self.assertNotIn(credential, result.rejection_reason)
        self.assertNotIn("Authorization", result.rejection_reason)

    def test_audit_encoding_time_is_inside_total_deadline(self):
        state, bundle = self.state_and_bundle()
        payload = json.loads((Path(__file__).parent / "fixtures" / "jev_responses.json").read_text())["response"]
        payload["model"] = bundle.bundle.model_id
        now = time.monotonic_ns()
        clock = [now]
        state = self.persisted(state, now + 2_000_000_000)
        from bot.autonomous.jev_client import _encode_jev_audit

        def expensive_audit(raw):
            clock[0] += 600_000_000
            return _encode_jev_audit(raw)

        async def handler(request):
            return httpx.Response(200, json=payload)

        client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                           api_key="synthetic-test-only", monotonic_ns=lambda: clock[0],
                           wall_clock_us=lambda: 60_000_000)
        request = JudgeRequest("audit-late", state.state_hash, bundle.bundle_hash,
                               bundle.bundle.model_id, now + 1_000_000_000, "test-boot")
        with patch("bot.autonomous.jev_client._encode_jev_audit", side_effect=expensive_audit):
            result = asyncio.run(client.judge(state, bundle, request))
        self.assertEqual(result.decisions, ())
        self.assertGreater(result.received_mono_ns, result.deadline_mono_ns)
        self.assertGreaterEqual(result.call_latency_ns, 1_200_000_000)

    def test_envelope_validation_time_cannot_return_a_late_decision(self):
        state, bundle = self.state_and_bundle()
        payload = json.loads((Path(__file__).parent / "fixtures" / "jev_responses.json").read_text())["response"]
        payload["model"] = bundle.bundle.model_id
        now = time.monotonic_ns()
        clock = [now]
        state = self.persisted(state, now + 2_000_000_000)
        from bot.autonomous.contracts import DecisionBatch

        def expensive_validation(*args, **kwargs):
            result = DecisionBatch(*args, **kwargs)
            clock[0] += 600_000_000
            return result

        async def handler(request):
            return httpx.Response(200, json=payload)

        client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                           api_key="synthetic-test-only", monotonic_ns=lambda: clock[0],
                           wall_clock_us=lambda: 60_000_000, total_deadline_ms=500)
        request = JudgeRequest("envelope-late", state.state_hash, bundle.bundle_hash,
                               bundle.bundle.model_id, now + 1_000_000_000, "test-boot")
        with patch("bot.autonomous.jev_client.DecisionBatch", side_effect=expensive_validation):
            result = asyncio.run(client.judge(state, bundle, request))
        self.assertEqual(result.decisions, ())
        self.assertGreater(result.received_mono_ns, result.deadline_mono_ns)
        self.assertGreaterEqual(result.call_latency_ns, 600_000_000)

    def test_bad_confidence_and_wrong_model_are_hold(self):
        state, bundle = self.state_and_bundle()
        path = Path(__file__).parent / "fixtures" / "jev_responses.json"
        template = json.loads(path.read_text())["response"]
        template["model"] = bundle.bundle.model_id

        for case, mutate in (
            ("confidence", lambda p: p["answers"]["aave:direction"].__setitem__("confidence", 0.9)),
            ("model", lambda p: p.__setitem__("model", "jev-other")),
        ):
            with self.subTest(case=case):
                payload = json.loads(json.dumps(template))
                mutate(payload)
                calls = []

                async def handler(request):
                    calls.append(request)
                    return httpx.Response(200, json=payload)

                now = time.monotonic_ns()
                client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                                   api_key="synthetic-test-only", wall_clock_us=lambda: 60_000_000)
                request = JudgeRequest("local-bad", state.state_hash, bundle.bundle_hash,
                                       bundle.bundle.model_id, now + 1_000_000_000, "test-boot")
                result = asyncio.run(client.judge(state, bundle, request))
                self.assertEqual(len(calls), 1)
                self.assertEqual(result.decisions, ())
                self.assertTrue(result.rejection_reason.startswith("hold:"))

    def test_missing_extra_answers_and_malformed_probabilities_hold(self):
        state, bundle = self.state_and_bundle()
        template = json.loads((Path(__file__).parent / "fixtures" / "jev_responses.json").read_text())["response"]
        template["model"] = bundle.bundle.model_id
        mutations = (
            lambda p: p["answers"].pop("aave:risk_state"),
            lambda p: p["answers"].__setitem__("unrequested", p["answers"]["aave:risk_state"]),
            lambda p: p["answers"]["aave:direction"]["probabilities"].__setitem__("long", True),
            lambda p: p["answers"]["aave:direction"]["probabilities"].__setitem__("long", float("inf")),
            lambda p: p["answers"]["aave:direction"]["probabilities"].__setitem__("surprise", 0),
            lambda p: p["answers"]["aave:toxic_flow"].__setitem__("noul", True),
            lambda p: p.__setitem__("usage", {"input_tokens": True, "output_tokens": 1}),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                payload = json.loads(json.dumps(template))
                mutate(payload)
                calls = []

                async def handler(request):
                    calls.append(request)
                    return httpx.Response(200, content=json.dumps(payload).encode())

                client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                                   api_key="synthetic-test-only", wall_clock_us=lambda: 60_000_000)
                request = JudgeRequest(f"malformed-{index}", state.state_hash, bundle.bundle_hash,
                                       bundle.bundle.model_id, time.monotonic_ns() + 1_000_000_000, "test-boot")
                result = asyncio.run(client.judge(state, bundle, request))
                self.assertEqual(len(calls), 1)
                self.assertEqual(result.decisions, ())
                self.assertIsNotNone(result.rejection_reason)

    def test_nullable_usage_missing_request_id_quality_three_and_noul_boundary(self):
        state, bundle = self.state_and_bundle()
        template = json.loads((Path(__file__).parent / "fixtures" / "jev_responses.json").read_text())["response"]
        template["model"] = bundle.bundle.model_id
        for usage in ("missing", None, {"input_tokens": None, "output_tokens": None}):
            for probability in (0.49, 0.5, 0.51):
                with self.subTest(usage=usage, probability=probability):
                    payload = json.loads(json.dumps(template))
                    if usage == "missing":
                        payload.pop("usage")
                    else:
                        payload["usage"] = usage
                    quality = payload["answers"]["aave:setup_quality"]
                    quality["choice"] = "3"
                    quality["probabilities"] = {"0": 0.1, "1": 0.1, "2": 0.1, "3": 0.7}
                    payload["answers"]["aave:toxic_flow"]["noul"] = probability

                    async def handler(request):
                        return httpx.Response(200, json=payload)

                    client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                                       api_key="synthetic-test-only", wall_clock_us=lambda: 60_000_000)
                    request = JudgeRequest("nullable-usage", state.state_hash, bundle.bundle_hash,
                                           bundle.bundle.model_id, time.monotonic_ns() + 1_000_000_000, "test-boot")
                    result = asyncio.run(client.judge(state, bundle, request))
                    self.assertEqual(len(result.decisions), 1)
                    self.assertIsNone(result.provider_request_id)
                    self.assertIsNone(result.usage_input_tokens)
                    self.assertIsNone(result.usage_output_tokens)
                    self.assertEqual(result.decisions[0].setup_quality, 3)
                    self.assertEqual(result.decisions[0].toxic_flow, probability >= 0.5)

    def test_rate_limits_server_errors_and_connection_failures_never_retry(self):
        state, bundle = self.state_and_bundle()
        for status in (429, 500, 529, "connection"):
            with self.subTest(status=status):
                calls = []

                async def handler(request):
                    calls.append(request)
                    if status == "connection":
                        raise httpx.ConnectError("synthetic failure")
                    return httpx.Response(status, content=b"synthetic failure")

                client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                                   api_key="synthetic-test-only", wall_clock_us=lambda: 60_000_000)
                request = JudgeRequest(f"failure-{status}", state.state_hash, bundle.bundle_hash,
                                       bundle.bundle.model_id, time.monotonic_ns() + 1_000_000_000, "test-boot")
                result = asyncio.run(client.judge(state, bundle, request))
                self.assertEqual(len(calls), 1)
                self.assertEqual(result.decisions, ())

    def test_usage_object_fields_are_independently_optional_but_strict(self):
        state, bundle = self.state_and_bundle()
        template = json.loads((Path(__file__).parent / "fixtures" / "jev_responses.json").read_text())["response"]
        template["model"] = bundle.bundle.model_id
        cases = (
            ({}, (None, None)),
            ({"input_tokens": 12}, (12, None)),
            ({"output_tokens": 3}, (None, 3)),
            ({"input_tokens": None}, (None, None)),
            ({"output_tokens": None}, (None, None)),
            ({"unexpected": 0}, None),
            ({"input_tokens": True}, None),
            ({"output_tokens": -1}, None),
            ({"input_tokens": 1.5}, None),
            ({"output_tokens": "3"}, None),
        )
        for usage, expected in cases:
            with self.subTest(usage=usage):
                payload = json.loads(json.dumps(template))
                payload["usage"] = usage

                async def handler(request):
                    return httpx.Response(200, json=payload)

                client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                                   api_key="synthetic-test-only", wall_clock_us=lambda: 60_000_000)
                request = JudgeRequest("partial-usage", state.state_hash, bundle.bundle_hash,
                                       bundle.bundle.model_id, time.monotonic_ns() + 1_000_000_000, "test-boot")
                result = asyncio.run(client.judge(state, bundle, request))
                if expected is None:
                    self.assertEqual(result.decisions, ())
                    self.assertIsNotNone(result.rejection_reason)
                else:
                    self.assertEqual(len(result.decisions), 1)
                    self.assertEqual((result.usage_input_tokens, result.usage_output_tokens), expected)

    def test_final_timing_envelope_copy_remains_inside_deadline(self):
        state, bundle = self.state_and_bundle()
        payload = json.loads((Path(__file__).parent / "fixtures" / "jev_responses.json").read_text())["response"]
        payload["model"] = bundle.bundle.model_id
        now = time.monotonic_ns()
        clock = [now]
        state = self.persisted(state, now + 2_000_000_000)

        def expensive_final_copy(*args, **kwargs):
            result = replace(*args, **kwargs)
            clock[0] += 600_000_000
            return result

        async def handler(request):
            return httpx.Response(200, json=payload)

        client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                           api_key="synthetic-test-only", monotonic_ns=lambda: clock[0],
                           wall_clock_us=lambda: 60_000_000, total_deadline_ms=500)
        request = JudgeRequest("final-copy-late", state.state_hash, bundle.bundle_hash,
                               bundle.bundle.model_id, now + 1_000_000_000, "test-boot")
        with patch("bot.autonomous.jev_client.replace", side_effect=expensive_final_copy):
            result = asyncio.run(client.judge(state, bundle, request))
        self.assertEqual(result.decisions, ())
        self.assertGreater(result.received_mono_ns, result.deadline_mono_ns)
        self.assertGreaterEqual(result.call_latency_ns, 600_000_000)

    def test_stale_or_wrong_boot_request_never_dispatches(self):
        state, bundle = self.state_and_bundle()
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(500, content=b"unused")

        client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                           api_key="synthetic-test-only", wall_clock_us=lambda: 62_000_001)
        now = time.monotonic_ns()
        for request in (
            JudgeRequest("stale", state.state_hash, bundle.bundle_hash, bundle.bundle.model_id,
                         now + 1_000_000_000, "test-boot"),
            JudgeRequest("wrong-boot", state.state_hash, bundle.bundle_hash, bundle.bundle.model_id,
                         now + 1_000_000_000, "other-boot"),
        ):
            result = asyncio.run(client.judge(state, bundle, request))
            self.assertEqual(result.decisions, ())
            self.assertTrue(result.rejection_reason.startswith("hold:"))
        self.assertEqual(calls, [])

    def test_total_deadline_cancels_late_transport(self):
        state, bundle = self.state_and_bundle()
        calls = []

        async def handler(request):
            calls.append(request)
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={})

        now = time.monotonic_ns()
        client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                           api_key="synthetic-test-only", wall_clock_us=lambda: 60_000_000,
                           total_deadline_ms=5)
        request = JudgeRequest("late", state.state_hash, bundle.bundle_hash,
                               bundle.bundle.model_id, now + 1_000_000_000, "test-boot")
        result = asyncio.run(client.judge(state, bundle, request))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.decisions, ())
        self.assertTrue(result.rejection_reason.startswith("hold:"))

    def test_oversized_stream_is_cut_off_and_never_parsed(self):
        state, bundle = self.state_and_bundle()
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(200, content=b"x" * (1_048_577))

        now = time.monotonic_ns()
        client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                           api_key="synthetic-test-only", wall_clock_us=lambda: 60_000_000)
        request = JudgeRequest("large", state.state_hash, bundle.bundle_hash,
                               bundle.bundle.model_id, now + 1_000_000_000, "test-boot")
        result = asyncio.run(client.judge(state, bundle, request))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.decisions, ())
        self.assertIsNone(result.raw_response_bytes)
        self.assertTrue(result.rejection_reason.startswith("hold:"))

    def test_duplicate_response_keys_are_rejected_and_usage_may_be_missing(self):
        state, bundle = self.state_and_bundle()
        payload = json.loads((Path(__file__).parent / "fixtures" / "jev_responses.json").read_text())["response"]
        payload["model"] = bundle.bundle.model_id
        payload.pop("usage")
        raw = json.dumps(payload, separators=(",", ":")).encode()
        duplicate = raw.replace(b'"model":"model"', b'"model":"model","model":"model"')
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(200, content=duplicate)

        now = time.monotonic_ns()
        client = JevClient(transport=AsyncMockTransport(handler), schema_resolver=self.resolver,
                           api_key="synthetic-test-only", wall_clock_us=lambda: 60_000_000)
        request = JudgeRequest("duplicate", state.state_hash, bundle.bundle_hash,
                               bundle.bundle.model_id, now + 1_000_000_000, "test-boot")
        result = asyncio.run(client.judge(state, bundle, request))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.decisions, ())
        self.assertTrue(result.rejection_reason.startswith("hold:"))


if __name__ == "__main__":
    unittest.main()
