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


class HttpTransportTests(unittest.TestCase):
    """Real httpx streaming through MockTransport and a local slow server: the deadline and size bound are wall-clock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def reflex_over(self, handler=None, *, url=None, budget_ms=250):
        import httpx
        from bot.autonomous.jev_reflex import make_http_transport
        transport = make_http_transport(httpx.MockTransport(handler) if handler else None)
        kw = {"endpoint": url} if url else {}
        reflex = JevReflex(data_dir=self.dir, api_key="test-key", transport=transport, budget_ms=budget_ms, **kw)
        (self.dir / APPROVED_SCHEMA_FILE).write_text(json.dumps(reflex.schema))
        return reflex

    def judge(self, reflex):
        import time
        started = time.monotonic()
        out = reflex.judge_batch({"BTC-USD": snapshot("BTC-USD")})
        return out, time.monotonic() - started

    @staticmethod
    def ok_body(request):
        return json.dumps(echo()(json.loads(request.content))).encode()

    def test_valid_streamed_response(self):
        import httpx
        out, _ = self.judge(self.reflex_over(lambda request: httpx.Response(200, content=self.ok_body(request))))
        self.assertEqual("trending", out["BTC-USD"].regime.value)

    def test_slow_trickle_hits_the_hard_deadline(self):
        import httpx, time
        def handler(request):
            body = self.ok_body(request)
            def trickle():
                for k in range(0, len(body), 64):
                    time.sleep(0.05)  # each chunk is fast; the total is not
                    yield body[k:k + 64]
            return httpx.Response(200, content=trickle())
        out, elapsed = self.judge(self.reflex_over(handler))
        self.assertEqual({"timeout"}, set(out.values()))
        self.assertLess(elapsed, 0.6)

    def test_stall_before_response_is_bounded_by_the_worker_join(self):
        import httpx, time
        def handler(request):
            time.sleep(1.5)  # e.g. DNS or connect stall, not covered by per-phase timeouts
            return httpx.Response(200, content=self.ok_body(request))
        out, elapsed = self.judge(self.reflex_over(handler))
        self.assertEqual({"timeout"}, set(out.values()))
        self.assertLess(elapsed, 0.5)

    def test_oversized_content_length_is_rejected_up_front(self):
        import httpx
        from bot.autonomous.jev_client import _MAX_RESPONSE_BYTES
        read = []
        def handler(request):
            def stream():
                read.append(1)
                yield b"{}"
            return httpx.Response(200, headers={"Content-Length": str(_MAX_RESPONSE_BYTES + 1)}, content=stream())
        out, _ = self.judge(self.reflex_over(handler))
        self.assertEqual({"response_size"}, set(out.values()))
        self.assertEqual([], read)

    def test_streamed_body_is_cut_at_the_size_bound(self):
        import httpx
        from bot.autonomous.jev_client import _MAX_RESPONSE_BYTES
        sent = []
        def handler(request):
            def flood():
                for _ in range(_MAX_RESPONSE_BYTES // 4096 + 50):
                    sent.append(4096)
                    yield b"x" * 4096
            return httpx.Response(200, content=flood())
        out, _ = self.judge(self.reflex_over(handler, budget_ms=1000))
        self.assertEqual({"response_size"}, set(out.values()))
        self.assertLess(sum(sent), _MAX_RESPONSE_BYTES + 3 * 4096)

    def test_local_slow_server_is_bounded(self):
        import threading, time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Slow(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()
                for _ in range(20):
                    try:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                    except OSError:
                        return
                    time.sleep(0.05)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        out, elapsed = self.judge(self.reflex_over(url=f"http://127.0.0.1:{server.server_port}/v1/systemone"))
        self.assertEqual({"timeout"}, set(out.values()))
        self.assertLess(elapsed, 0.6)

    def test_httpx_timeout_exception_maps_to_timeout(self):
        import httpx
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)
        out, _ = self.judge(self.reflex_over(handler))
        self.assertEqual({"timeout"}, set(out.values()))


class TemplateBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_approved_hash_covers_template_and_type_mapping(self):
        reflex = JevReflex(data_dir=self.dir, api_key="k", transport=lambda *a: (200, None, b""))
        for path, value in ((("types", "toxic_flow"), "choice"), (("question_template",), "Answer freely: {kind}")):
            with self.subTest(path):
                tampered = json.loads(json.dumps(reflex.schema))
                target = tampered
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                (self.dir / APPROVED_SCHEMA_FILE).write_text(json.dumps(tampered))
                self.assertEqual("schema_hash_mismatch", reflex.gate())

    def test_every_sent_question_is_rendered_from_the_schema(self):
        reflex = JevReflex(data_dir=self.dir, api_key="k", transport=lambda *a: (200, None, b""))
        questions = reflex.questions(("BTC-USD",))
        for kind in ("regime", "direction", "setup_quality", "risk_state", "toxic_flow"):
            q = questions[f"BTC-USD:{kind}"]
            self.assertEqual(reflex.schema["types"][kind], q["type"])
            self.assertEqual(reflex.schema["question_template"].format(
                schema_hash=reflex.schema_hash, instrument="BTC-USD", instrument_json=json.dumps("BTC-USD"),
                fields=reflex.schema["fields"], horizon=reflex.schema["horizon"], kind=kind,
                instructions=reflex.schema["rubrics"][kind]["instructions"]), q["instructions"])
            self.assertEqual(reflex.schema["rubrics"][kind]["criteria"], q["criteria"])


class Slow:
    """Non-batch reflex that takes ``seconds`` of the injected clock to answer."""
    reflex_id = "fixed-test"

    def __init__(self, clock, judgment, seconds):
        self.clock, self.judgment, self.seconds = clock, judgment, seconds

    def judge(self, snapshot):
        self.clock.us += int(self.seconds * 1_000_000)
        return self.judgment


class PostReflexFreshnessTests(RuntimeCase):
    def test_entry_holds_when_the_book_aged_during_the_reflex(self):
        from bot.autonomous.runtime import AdvisoryRisk as _A  # noqa: F401
        from tests.autonomous.test_runtime import GOOD
        rt = self.runtime(GOOD, explore_fraction=0.001)
        self.advance(rt, 20)
        rt.reflex = Slow(self.clock, GOOD, 11)
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
        self.assertIn("STALE_AFTER_REFLEX", result["reasons"])
        self.assertEqual(0, rt.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0])

    def test_exit_refetches_a_stale_book_once(self):
        from dataclasses import replace
        from tests.autonomous.test_runtime import GOOD
        rt = self.runtime(GOOD, explore_fraction=0.001)
        self.assertEqual("buy", self.advance(rt, 21)["instruments"]["AAVE-USD"]["status"])
        rt.reflex = Slow(self.clock, replace(GOOD, risk_state=AdvisoryRisk.REDUCE), 11)
        calls = rt.book_source.calls
        result = self.advance(rt, 1)["instruments"]["AAVE-USD"]
        self.assertEqual("sell", result["status"], result["reasons"])
        self.assertIn("EXIT_BOOK_REFETCHED", result["reasons"])
        self.assertEqual(calls + 2, rt.book_source.calls)

    def test_exit_holds_when_the_refetched_book_is_also_stale(self):
        from dataclasses import replace
        from tests.autonomous.test_runtime import GOOD
        rt = self.runtime(GOOD, explore_fraction=0.001)
        self.assertEqual("buy", self.advance(rt, 21)["instruments"]["AAVE-USD"]["status"])
        rt.reflex = Slow(self.clock, replace(GOOD, risk_state=AdvisoryRisk.REDUCE), 11)
        original, n = rt.book_source, {"calls": 0}
        def source(product):
            from tests.autonomous.test_runtime import stamp
            n["calls"] += 1
            book = original(product)
            if n["calls"] == 2:  # the post-reflex re-fetch is stale too
                book["exchange_time"] = stamp(self.clock.us - 20_000_000)
            return book
        rt.book_source = source
        self.clock.us += 60_000_000
        result = rt.cycle()["instruments"]["AAVE-USD"]
        self.assertEqual("hold", result["status"])
        self.assertIn("EXIT_STALE_BOOK", result["reasons"])


class BatchMembershipTests(RuntimeCase):
    def test_dust_only_instruments_are_not_sent_to_jev(self):
        rt = JevRuntimeTests.jev_runtime(self)
        rt.db.execute("INSERT INTO fills VALUES ('dust','d','AAVE-USD','buy','0.001','100','0.1','0',0,0)")
        self.advance(rt, 5)
        self.assertEqual([], self.transport.calls)

    def test_status_and_nightly_group_brier_by_reflex(self):
        from tests.autonomous.test_runtime import GOOD
        rt = self.runtime(GOOD, horizon_candles=3)
        self.advance(rt, 25)
        status = json.loads((self.dir / "status.json").read_text())
        self.assertIn("fixed-test", status["brier_by_reflex"])
        self.assertEqual(status["brier"], status["brier_by_reflex"]["fixed-test"]["brier"])
        from bot.autonomous.runtime import _day, nightly_review
        report = nightly_review(self.dir / "runtime.sqlite3", self.dir / "reviews", now_us=self.clock.us, day=_day(self.clock.us))
        self.assertIn("fixed-test", report["metrics"]["brier_by_reflex"])


def START_PLUS(n):
    from tests.autonomous.test_runtime import START_US
    return START_US + 1_000_000 + n * 60_000_000


if __name__ == "__main__":
    unittest.main()
