"""Inert Jev REFLEX adapter: approved-schema gate, strict answers, latency budget, fail-safe holds. No network."""
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from bot.autonomous.contracts import AdvisoryRisk, Direction, Regime
from bot.autonomous.jev_reflex import APPROVED_SCHEMA_FILE, JevReflex, compile_schema, schema_hash, write_proposed_schema
from bot.autonomous.runtime import PaperRuntime, RuntimeConfig, Snapshot, main
from tests.autonomous.test_runtime import FakeBooks, FakeRules, RuntimeCase, seed_labels


def choice(labels, top, p_top):
    rest = (1 - p_top) / (len(labels) - 1)
    probabilities = {label: (p_top if label == top else rest) for label in labels}
    return {"type": "choice", "choice": top, "probabilities": probabilities,
            "confidence": (p_top - 1 / len(labels)) / (1 - 1 / len(labels))}


def answers(instruments, *, regime="trending", direction="long", p_direction=0.9, quality="2", risk="safe", toxic=0.1):
    out = {}
    for i in instruments:
        out[f"{i}:regime"] = choice(("trending", "mean_reverting", "high_vol", "crisis"), regime, 0.85)
        out[f"{i}:direction"] = choice(("long", "short", "neutral"), direction, p_direction)
        out[f"{i}:setup_quality"] = choice(("0", "1", "2", "3"), quality, 0.85)
        out[f"{i}:risk_state"] = choice(("safe", "near_limit", "reduce"), risk, 0.9)
        out[f"{i}:toxic_flow"] = {"type": "noul", "noul": toxic}
    return out


class Mono:
    def __init__(self):
        self.ns = 10**12

    def __call__(self):
        return self.ns


class FakeTransport:
    def __init__(self, mono, body=None, *, status=200, advance_ms=5, raises=None):
        self.mono, self.body, self.status, self.advance_ms, self.raises, self.calls = mono, body, status, advance_ms, raises, []

    def __call__(self, url, headers, body, timeout_s):
        self.calls.append(json.loads(body))
        self.mono.ns += self.advance_ms * 1_000_000
        if self.raises:
            raise self.raises
        payload = self.body(self.calls[-1]) if callable(self.body) else self.body
        return self.status, "req-1", payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def snapshot(instrument):
    return Snapshot(instrument, 0, 0, 0, 0, 100.0, 1.0, 0.1, 5.0, 300.0, 3.0, 30, 0.0, 0.0, 0.0)


def echo(**kw):
    """Answer exactly the instruments asked about, with the requested model."""
    def respond(request):
        instruments = sorted({qid.rsplit(":", 1)[0] for qid in request["questions"]})
        return {"model": request["model"], "answers": answers(instruments, **kw), "usage": {"input_tokens": 1, "output_tokens": 1}}
    return respond


class JevReflexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.mono = Mono()

    def reflex(self, transport, *, approve=True, api_key="test-key", **kw):
        reflex = JevReflex(data_dir=self.dir, api_key=api_key, transport=transport, monotonic_ns=self.mono, **kw)
        if approve:
            (self.dir / APPROVED_SCHEMA_FILE).write_text(json.dumps(reflex.schema))
        return reflex

    def judge(self, reflex, instruments=("BTC-USD", "ETH-USD")):
        return reflex.judge_batch({i: snapshot(i) for i in instruments})

    def test_valid_batch_is_one_call_with_typed_judgments(self):
        transport = FakeTransport(self.mono, echo())
        reflex = self.reflex(transport)
        out = self.judge(reflex)
        self.assertEqual(1, len(transport.calls))
        request = transport.calls[0]
        self.assertEqual("jev-1.14.0", request["model"])
        self.assertEqual(10, len(request["questions"]))
        self.assertEqual({"BTC-USD", "ETH-USD"}, set(request["state"]["rows"]))
        self.assertNotIn("i", request["state"]["rows"]["BTC-USD"])  # numeric rows only
        for judgment in out.values():
            self.assertEqual((Regime.TRENDING, Direction.LONG, 2, AdvisoryRisk.SAFE, False),
                             (judgment.regime, judgment.direction, judgment.setup_quality, judgment.risk_state, judgment.toxic_flow))
            self.assertAlmostEqual(0.85, judgment.direction_confidence)
            self.assertAlmostEqual(0.8, judgment.critical_confidence)
            self.assertFalse(judgment.calibrated)
            self.assertEqual(f"jev:jev-1.14.0:{reflex.schema_hash[:16]}", judgment.reflex_id)
        self.assertEqual(("ok", 5.0), (reflex.last_call["outcome"], reflex.last_call["latency_ms"]))
        self.assertIsNotNone(reflex.last_call["response_hash"])

    def test_noul_boundary_maps_half_to_toxic(self):
        out = self.judge(self.reflex(FakeTransport(self.mono, echo(toxic=0.5))))
        self.assertTrue(all(j.toxic_flow for j in out.values()))

    def test_invalid_answers_hold_every_instrument(self):
        def drop(field):
            def respond(request):
                body = echo()(request)
                del body["answers"]["BTC-USD:direction"][field]
                return body
            return respond
        def quality_four(request):
            body = echo()(request)
            body["answers"]["BTC-USD:setup_quality"] = choice(("0", "1", "2", "3", "4"), "4", 0.9)
            return body
        def wrong_confidence(request):
            body = echo()(request)
            body["answers"]["ETH-USD:direction"]["confidence"] = 0.9
            return body
        def wrong_model(request):
            return dict(echo()(request), model="jev-latest")
        cases = {"malformed": b"{not json", "duplicate": b'{"model":"jev-1.14.0","model":"x","answers":{}}',
                 "missing_field": drop("confidence"), "quality_out_of_range": quality_four,
                 "inconsistent_confidence": wrong_confidence, "wrong_model": wrong_model}
        for name, body in cases.items():
            with self.subTest(name):
                out = self.judge(self.reflex(FakeTransport(self.mono, body)))
                self.assertEqual({"BTC-USD", "ETH-USD"}, set(out))
                self.assertTrue(all(isinstance(v, str) and v.startswith("invalid_answer_") for v in out.values()), out)

    def test_timeout_and_latency_budget(self):
        out = self.judge(self.reflex(FakeTransport(self.mono, echo(), raises=TimeoutError())))
        self.assertEqual({"timeout"}, set(out.values()))
        reflex = self.reflex(FakeTransport(self.mono, echo(), advance_ms=251))
        self.assertEqual({"latency_budget"}, set(self.judge(reflex).values()))
        self.assertGreater(reflex.last_call["latency_ms"], 250)
        self.assertEqual({"http_529"}, set(self.judge(self.reflex(FakeTransport(self.mono, echo(), status=529))).values()))

    def test_gates_block_before_any_request(self):
        transport = FakeTransport(self.mono, echo())
        self.assertEqual({"schema_not_approved"}, set(self.judge(self.reflex(transport, approve=False)).values()))
        (self.dir / APPROVED_SCHEMA_FILE).write_text(json.dumps(compile_schema("jev-latest")))
        self.assertEqual({"schema_hash_mismatch"}, set(self.judge(JevReflex(data_dir=self.dir, api_key="k", transport=transport,
                                                                         monotonic_ns=self.mono)).values()))
        self.assertEqual({"no_api_key"}, set(self.judge(self.reflex(transport, api_key=None)).values()))
        self.assertEqual([], transport.calls)

    def test_proposed_schema_round_trips_to_the_same_hash(self):
        reflex = self.reflex(FakeTransport(self.mono, echo()), approve=False)
        path = write_proposed_schema(self.dir, reflex.schema)
        self.assertEqual(reflex.schema_hash, schema_hash(json.loads(path.read_text())))
        self.assertEqual("schema_not_approved", reflex.gate())
        path.rename(self.dir / APPROVED_SCHEMA_FILE)  # the operator's approval step
        self.assertIsNone(reflex.gate())

    def test_budget_is_bounded(self):
        with self.assertRaises(ValueError):
            JevReflex(data_dir=self.dir, api_key="k", budget_ms=1_001)


class JevRuntimeTests(RuntimeCase):
    def jev_runtime(self, body=None, *, fallback="none", approve=True, **config):
        self.mono = Mono()
        self.transport = FakeTransport(self.mono, body or echo())
        reflex = JevReflex(data_dir=self.dir, api_key="test-key", transport=self.transport, monotonic_ns=self.mono,
                           horizon_candles=config.get("horizon_candles", 60))
        if approve:
            (self.dir / APPROVED_SCHEMA_FILE).write_text(json.dumps(reflex.schema))
        config.setdefault("instruments", ("AAVE-USD",))
        config.setdefault("paper_armed", True)
        self.rules = FakeRules()
        rt = PaperRuntime(RuntimeConfig(data_dir=self.dir, reflex_fallback=fallback, **config),
                          book_source=FakeBooks(self.clock, {"AAVE-USD": lambda: self.price["v"]}),
                          clock_us=self.clock, reflex=reflex, rules_source=self.rules)
        self.addCleanup(rt.close)
        return rt

    def test_jev_judgment_drives_the_code_gate_and_is_audited(self):
        rt = self.jev_runtime(explore_fraction=0.001)
        result = self.advance(rt, 21)["instruments"]["AAVE-USD"]
        self.assertEqual("buy", result["status"], result["reasons"])
        self.assertEqual(1, len(self.transport.calls))  # warmup candles never call Jev
        decision = rt.db.execute("SELECT reflex_id FROM decisions").fetchone()
        self.assertTrue(decision["reflex_id"].startswith("jev:jev-1.14.0:"))
        call = rt.db.execute("SELECT * FROM reflex_calls").fetchone()
        self.assertEqual(("ok", 5.0, 5), (call["outcome"], call["latency_ms"], call["questions"]))
        status = json.loads((self.dir / "status.json").read_text())
        self.assertEqual(rt.reflex.reflex_id, status["reflex"])
        self.assertEqual("ok", status["reflex_last_call"]["outcome"])

    def test_failure_holds_entries_without_silent_offline_fallback(self):
        rt = self.jev_runtime(explore_fraction=0.001)
        self.transport.raises = TimeoutError()
        result = self.advance(rt, 21)["instruments"]["AAVE-USD"]
        self.assertEqual("hold", result["status"])
        self.assertEqual(["REFLEX_UNAVAILABLE:timeout"], result["reasons"])
        self.assertIsNone(result["judgment"])
        self.assertEqual(0, rt.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0])
        row = rt.db.execute("SELECT reflex_id, phase, regime FROM decisions").fetchone()
        self.assertEqual((rt.reflex.reflex_id, "reflex_unavailable", None), tuple(row))

    def test_code_exits_keep_running_while_jev_is_down(self):
        rt = self.jev_runtime(explore_fraction=0.001, horizon_candles=3)
        self.assertEqual("buy", self.advance(rt, 21)["instruments"]["AAVE-USD"]["status"])
        self.transport.raises = ConnectionError("down")
        self.advance(rt, 2)
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
        self.assertEqual("sell", result["status"], result["reasons"])
        self.assertIn("REFLEX_UNAVAILABLE:transport_ConnectionError", result["reasons"])
        self.assertIn("TIME_STOP", result["reasons"])

    def test_explicit_offline_fallback_is_labeled_offline(self):
        rt = self.jev_runtime(fallback="offline", approve=False)
        result = self.advance(rt, 21)["instruments"]["AAVE-USD"]
        self.assertIn("REFLEX_FALLBACK_OFFLINE:schema_not_approved", result["reasons"])
        self.assertEqual("offline-reflex-v1", result["judgment"]["reflex_id"])
        self.assertEqual("offline-reflex-v1", rt.db.execute("SELECT reflex_id FROM decisions").fetchone()[0])
        self.assertEqual([], self.transport.calls)

    def test_jev_low_critical_confidence_escalates(self):
        rt = self.jev_runtime(echo(p_direction=0.9))
        def weak(request):
            body = echo()(request)
            body["answers"]["AAVE-USD:setup_quality"] = choice(("0", "1", "2", "3"), "2", 0.6)  # confidence .4667
            return body
        self.transport.body = weak
        self.assertIn("LOW_CONFIDENCE", self.advance(rt, 21)["instruments"]["AAVE-USD"]["reasons"])

    def test_calibration_cohorts_are_separated_by_reflex_id(self):
        rt = self.jev_runtime()
        seed_labels(rt, 72, 48, reflex_id="offline-reflex-v1")
        self.assertIn("CALIBRATION_INSUFFICIENT", self.advance(rt, 21)["instruments"]["AAVE-USD"]["reasons"])
        seed_labels(rt, 72, 48, reflex_id=rt.reflex.reflex_id, prefix="jev")
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
        self.assertEqual("buy", result["status"], result["reasons"])

    def test_cli_jev_without_key_holds_and_never_builds_http(self):
        import io
        from contextlib import redirect_stdout
        books = FakeBooks(self.clock, {"BTC-USD": 60000.0, "ETH-USD": 3000.0})
        for n in range(21):
            self.clock.us = START_PLUS(n)
            with mock.patch.dict("os.environ", {"JEV_API_KEY": ""}), \
                    mock.patch("bot.autonomous.runtime._public_book_source", return_value=books), \
                    mock.patch("bot.autonomous.runtime._public_rules_source", return_value=FakeRules()), \
                    mock.patch("bot.autonomous.jev_reflex.http_transport", side_effect=AssertionError("network")), \
                    mock.patch("bot.autonomous.runtime.time.time_ns", return_value=self.clock.us * 1000):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(0, main(["--paper", "--once", "--reflex", "jev", "--data-dir", str(self.dir)]))
        status = json.loads(out.getvalue())
        self.assertTrue(status["reflex"].startswith("jev:"))
        self.assertEqual({"REFLEX_UNAVAILABLE:no_api_key"}, {r["reasons"][0] for r in status["instruments"].values()})


def START_PLUS(n):
    from tests.autonomous.test_runtime import START_US
    return START_US + 1_000_000 + n * 60_000_000


if __name__ == "__main__":
    unittest.main()
