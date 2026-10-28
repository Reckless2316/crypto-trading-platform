"""Task 12: one order authority and the paper venue lifecycle (offline, synthetic books)."""
import dataclasses
import inspect
import unittest
from decimal import Decimal as D

from bot.autonomous.contracts import (BookPayload, ContractViolation, ExecutableBook, IntentState,
    ReceiptStatus, content_hash)
from tests.autonomous import test_ledger


def execution():
    from bot.autonomous import execution as module
    return module


class ActionKeyTests(unittest.TestCase):
    def test_action_key_is_stable(self):
        key = execution().economic_action_key("paper-1", 1000000, 1, "entry")
        self.assertEqual(key, execution().economic_action_key("paper-1", 1000000, 1, "entry"))
        self.assertNotEqual(key, execution().economic_action_key("paper-1", 1000000, 2, "entry"))
        for bad in (("", 1, 1, "entry"), ("a", -1, 1, "entry"), ("a", 1, 0, "entry"), ("a", 1, 1, "")):
            with self.assertRaises(ValueError):
                execution().economic_action_key(*bad)


class RecordingVenue:
    """Wraps a venue and counts submissions so tests can prove nothing reached it."""
    venue = "paper"
    def __init__(self, inner=None, error=None):
        self.inner, self.error, self.calls = inner, error, 0

    def submit(self, intent, book, market):
        self.calls += 1
        self.in_transaction = getattr(self, "ledger", None) is not None and self.ledger.connection.in_transaction
        if self.error:
            raise self.error
        return self.inner.submit(intent, book, market)


class AuthorityTests(unittest.TestCase):
    _A = test_ledger.AccountingTests
    setUp, tearDown = _A.setUp, _A.tearDown
    evidence, initialize, balances, mark = _A.evidence, _A.initialize, _A.balances, _A.mark
    wire_accounting_admission = _A.wire_accounting_admission

    def prepare(self, asks=((D("100"), D("0.5")), (D("100.05"), D("10"))), *, error=None, verifier=None,
                fee_rate=D("0.001"), slippage_bps=D("0")):
        from bot.autonomous import risk
        self.initialize()
        self.proposal = self.wire_accounting_admission()
        limits, market, _, boot = self.ledger._risk_config
        self.ledger.configure_risk_checks(limits=limits, market=market, assess=risk.assess, boot_id=boot,
            monotonic_ns=lambda: 1)
        self.asks = asks
        self.book_source = lambda instrument: ExecutableBook("paper", instrument, self.now,
            BookPayload(((D("99.95"), D("100")),), self.asks), "e" * 64)
        x = execution()
        self.paper = x.PaperVenue(fee_rate=fee_rate, slippage_bps=slippage_bps, clock_us=lambda: self.now)
        self.venue = RecordingVenue(self.paper, error)
        self.market = market
        self.authority = x.OrderAuthority(self.ledger, self.venue, book_source=lambda i: self.book_source(i),
            market_source=market, entry_verifier=verifier or (lambda proposal, account, book, market, now: True),
            clock_us=lambda: self.now, book_verifier=lambda book: True,
            market_capability_verifier=lambda market, now: True)
        return self.proposal

    def submit(self, proposal=None):
        p = proposal or self.proposal
        return self.authority.submit(p, decision=p.decision, request_id="request-" + p.action_id)

    def intent_state(self):
        return self.ledger.connection.execute("SELECT state FROM order_intents").fetchone()[0]

    def event_kinds(self):
        return [r[0] for r in self.ledger.connection.execute("SELECT kind FROM order_events ORDER BY append_sequence")]

    def test_caller_cannot_supply_a_book(self):
        params = inspect.signature(execution().OrderAuthority.submit).parameters
        self.assertNotIn("book", params)
        self.assertNotIn("market", params)

    def test_missing_provenance_verifiers_refuse_before_reservation(self):
        self.prepare()
        self.authority.book_verifier = None
        receipt = self.submit()
        self.assertEqual(ReceiptStatus.REJECTED, receipt.status)
        self.assertIn("BOOK_UNAVAILABLE", receipt.reason_codes)
        self.assertEqual(0, self.venue.calls)
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM order_intents").fetchone()[0])

    def test_unverified_market_capability_refuses_before_reservation(self):
        self.prepare()
        self.authority.market_capability_verifier = lambda market, now: False
        receipt = self.submit()
        self.assertEqual(ReceiptStatus.REJECTED, receipt.status)
        self.assertIn("VERIFIED_MARKET_CAPABILITY_UNAVAILABLE", receipt.reason_codes)
        self.assertEqual(0, self.venue.calls)
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM order_intents").fetchone()[0])

    def test_market_capability_lost_at_dispatch_releases_reservation(self):
        self.prepare()
        checks = 0

        def verify(market, now):
            nonlocal checks
            checks += 1
            return checks < 3

        self.authority.market_capability_verifier = verify
        receipt = self.submit()
        self.assertEqual(ReceiptStatus.REJECTED, receipt.status)
        self.assertIn("MARKET_CAPABILITY_CHANGED_AT_DISPATCH", receipt.reason_codes)
        self.assertEqual(0, self.venue.calls)
        self.assertEqual("rejected", self.intent_state())
        self.assertEqual(0, self.ledger.connection.execute(
            "SELECT cash_usd_u FROM reservations").fetchone()[0])

    def test_state_engine_lineage_must_be_committed_and_not_invalidated(self):
        from pathlib import Path
        from bot.autonomous.contracts import SourceEvent
        from bot.autonomous.state import SourceClockPolicy, StateEngine

        self.prepare()
        source, stream = "paper-feed", "book"
        policy = SourceClockPolicy("test-book", source, stream, 2_000_000, 0)
        engine = StateEngine(self.ledger, Path(self.directory.name) / "archive", (policy,))
        payload = BookPayload(((D("99.95"), D("100")),), ((D("100"), D("10")),))
        event = SourceEvent(event_id="engine-book", source=source, stream=stream,
            logical_key="book-key", revision=0, instrument=self.proposal.instrument,
            event_at_us=self.now, received_at_us=self.now, known_at_us=self.now,
            payload=payload, payload_hash=content_hash(payload), sequence=1)
        self.assertTrue(engine.ingest(event).accepted)
        committed = engine.executable_book(source, stream, self.proposal.instrument, "paper")
        self.assertTrue(engine.verify_executable_book(committed))

        venue = self.venue
        authority = execution().OrderAuthority(self.ledger, venue,
            book_source=lambda instrument: ExecutableBook("paper", instrument, self.now, payload,
                content_hash(("executable-book-v1", "not-committed", 1))),
            market_source=self.market, entry_verifier=lambda *args: True, clock_us=lambda: self.now,
            book_verifier=engine.verify_executable_book,
            market_capability_verifier=lambda market, now: True)
        uncommitted = authority.submit(self.proposal, decision=self.proposal.decision, request_id="uncommitted-book")
        self.assertEqual(ReceiptStatus.REJECTED, uncommitted.status)
        self.assertEqual(0, venue.calls)
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM order_intents").fetchone()[0])

        book_reads = 0

        def invalidate_before_dispatch(instrument):
            nonlocal book_reads
            book_reads += 1
            if book_reads == 2:
                engine.invalidate("invalidate-book", "engine-book", self.now, "source_reorg")
            return committed

        authority.book_source = invalidate_before_dispatch
        invalidated = authority.submit(self.proposal, decision=self.proposal.decision, request_id="invalidated-book")
        self.assertEqual(ReceiptStatus.REJECTED, invalidated.status)
        self.assertIn("BOOK_STALE_AT_DISPATCH", invalidated.reason_codes)
        self.assertFalse(engine.verify_executable_book(committed))
        self.assertEqual(0, venue.calls)
        self.assertEqual("rejected", self.intent_state())
        self.assertEqual(0, self.ledger.connection.execute(
            "SELECT cash_usd_u FROM reservations").fetchone()[0])

    def test_partial_ioc_fill_records_fill_and_cancels_remainder(self):
        self.prepare()
        receipt = self.submit()
        self.assertEqual(ReceiptStatus.COMPLETED, receipt.status, receipt.reason_codes)
        self.assertEqual(IntentState.CANCELED, receipt.state)
        self.assertEqual(["reserved", "dispatching", "partial", "canceled"], self.event_kinds())
        fill = self.ledger.connection.execute("SELECT base_qty,price,fee_qty FROM fills").fetchone()
        self.assertEqual(("0.5", "100", "0.05"), tuple(fill))
        self.assertEqual(1, self.venue.calls)

    def test_canceled_partial_entry_can_reduce_its_acquired_inventory(self):
        from bot.autonomous.contracts import Side
        self.prepare()
        entry = self.submit()
        self.assertEqual(IntentState.CANCELED, entry.state)
        self.assertEqual("0.5", self.balances()["AAVE"])
        self.mark("after-partial", books={"AAVE": {
            "at_us": self.now, "quote_asset": "USD", "bids": [["99.95", "100"]]}})
        reduction = dataclasses.replace(self.proposal, side=Side.SELL, reducing_only=True,
            decision=None, base_quantity=D("0.1"), limit_price=D("99.95"),
            action_slot=f"reduce:{entry.intent_id}:1", action_id="reduce-partial-entry")
        receipt = self.authority.submit(reduction, decision=None, request_id="reduce-partial")
        self.assertEqual(ReceiptStatus.COMPLETED, receipt.status, receipt.reason_codes)
        self.assertEqual(IntentState.FILLED, receipt.state)
        self.assertEqual("0.4", self.balances()["AAVE"])
        self.assertEqual(2, self.venue.calls)

    def test_final_book_must_reproduce_entry_before_dispatch(self):
        seen = []
        def verify(proposal, account, book, market, now):
            seen.append(book.book.asks)
            self.assertEqual((), account.reservations)
            return sum(qty for _, qty in book.book.asks) >= D("10")
        self.prepare(verifier=verify)
        books = iter((self.book_source(self.proposal.instrument), ExecutableBook(
            "paper", self.proposal.instrument, self.now,
            BookPayload(((D("99.95"), D("100")),), ((D("100"), D(".01")),)), "f" * 64)))
        self.book_source = lambda _: next(books)
        receipt = self.submit()
        self.assertEqual(ReceiptStatus.REJECTED, receipt.status, receipt.reason_codes)
        self.assertIn("ENTRY_SIZING_CHANGED_AT_DISPATCH", receipt.reason_codes)
        self.assertEqual(2, len(seen))
        self.assertEqual(0, self.venue.calls)
        self.assertEqual("rejected", self.intent_state())
        self.assertEqual(0, self.ledger.connection.execute(
            "SELECT cash_usd_u FROM reservations").fetchone()[0])

    def test_paper_multi_level_fee_rounding_never_exceeds_reserved_budget(self):
        self.prepare(asks=((D("100"), D(".01")), (D("100.01"), D(".01"))), fee_rate=None)
        limits, _, assessor, boot = self.ledger._risk_config
        rules = dataclasses.replace(self.market("AAVE-USD"), fee_bound=D(".000000001"))
        self.authority.market_source = lambda _: rules
        self.ledger.configure_risk_checks(limits=limits, market=lambda _: rules, assess=assessor,
            boot_id=boot, monotonic_ns=lambda: 1)
        self.proposal = dataclasses.replace(self.proposal, base_quantity=D(".02"),
            limit_price=D("100.01"), max_fee_usd_u=1)
        receipt = self.submit()
        self.assertEqual(ReceiptStatus.COMPLETED, receipt.status, receipt.reason_codes)
        self.assertNotIn("FEE_BOUND_EXCEEDED", receipt.reason_codes)
        fees = [D(row[0]) for row in self.ledger.connection.execute("SELECT fee_qty FROM fills")]
        self.assertEqual(D(".000001"), sum(fees))
        self.assertEqual("0.01", self.balances()["AAVE"])
        self.assertEqual(IntentState.CANCELED, receipt.state)

    def test_paper_skips_sub_lot_depth_and_fills_later_tradable_level(self):
        self.prepare(asks=((D("99.99"), D(".001")), (D("100"), D("10"))))
        receipt = self.submit()
        self.assertEqual(IntentState.FILLED, receipt.state)
        self.assertEqual("1", self.balances()["AAVE"])

    def test_paper_fee_budget_zero_below_one_lot_and_exact_equality(self):
        from bot.autonomous.contracts import Side
        self.prepare()
        account = self.ledger.account_view(self.proposal.account_id)
        generation = self.ledger.connection.execute("SELECT generation FROM accounts").fetchone()[0]
        prepared = self.ledger.reserve_intent(self.proposal, account.revision, generation, self.proposal.decision)
        book = self.book_source(self.proposal.instrument)
        for side in (Side.BUY, Side.SELL):
            for budget, count in ((0, 0), (999, 0), (1000, 1)):
                with self.subTest(side=side, budget=budget):
                    proposal = dataclasses.replace(self.proposal, side=side, reducing_only=side is Side.SELL,
                        limit_price=D("100") if side is Side.BUY else D("99.95"),
                        base_quantity=D(".01"), max_fee_usd_u=budget)
                    intent = dataclasses.replace(prepared, proposal=proposal)
                    result = self.paper.submit(intent, book, self.market(proposal.instrument))
                    self.assertEqual(count, len(result.fills))
                    fee_u = sum(D(fill["fee_quantity"]) * 1_000_000 for fill in result.fills)
                    self.assertLessEqual(fee_u, budget)

    def test_worker_dispatches_one_paper_fill_through_the_authority(self):
        from bot.autonomous.worker import PaperWorker
        self.prepare(asks=((D("100"), D("5")),))
        self.venue.venue = "paper"
        worker = PaperWorker(self.authority)
        first = worker.dispatch_proposal(self.now, self.proposal, request_id="worker-fill")
        second = worker.dispatch_proposal(self.now, self.proposal, request_id="worker-fill-again")
        self.assertTrue(first["dispatched"], first)
        self.assertEqual(second["status"], "duplicate_or_historical")
        self.assertEqual(1, self.venue.calls)
        self.assertEqual("filled", self.ledger.connection.execute("SELECT state FROM order_intents").fetchone()[0])

    def test_worker_kill_never_reaches_the_paper_venue(self):
        from bot.autonomous.worker import PaperWorker
        proposal = self.prepare(asks=((D("100"), D("5")),))
        self.ledger.connection.execute("UPDATE account_control SET hard_killed=1")
        self.venue.venue = "paper"
        worker = PaperWorker(self.authority)
        blocked = worker.dispatch_proposal(self.now, proposal, request_id="worker-killed")
        self.assertFalse(blocked["dispatched"])
        self.assertTrue(blocked["halted"])
        self.assertEqual(0, self.venue.calls)
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM fills").fetchone()[0])

    def test_paper_account_cannot_dispatch_to_a_live_venue(self):
        self.prepare()
        self.venue.venue = "coinbaseadvanced"
        receipt = self.submit()
        self.assertEqual(ReceiptStatus.REJECTED, receipt.status)
        self.assertIn("VENUE_MODE_MISMATCH", receipt.reason_codes)
        self.assertEqual(0, self.venue.calls)
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM fills").fetchone()[0])

    def test_acknowledgment_with_unknown_outcome_is_pending(self):
        from bot.autonomous.contracts import OrderReference, VenueOrderResult
        self.prepare()
        owner = self
        class AcknowledgmentOnly:
            def submit(self, intent, book, market):
                reference = OrderReference(intent.proposal.account_id, intent.intent_id,
                    intent.client_order_id, "acknowledged-order")
                result = VenueOrderResult(reference, IntentState.UNKNOWN, D(0),
                    intent.proposal.base_quantity, owner.now, "ack-only")
                return execution().VenueExecution((result,), ())
        self.venue.inner = AcknowledgmentOnly()
        receipt = self.submit()
        self.assertEqual(ReceiptStatus.PENDING, receipt.status)
        self.assertEqual(IntentState.UNKNOWN, receipt.state)
        self.assertGreater(self.ledger.account_view("paper-test").reservations[0].cash_usd_u, 0)

    def test_serve_loop_reviews_once_and_never_submits(self):
        from bot.autonomous.worker import PaperWorker
        self.prepare(asks=((D("100"), D("5")),))
        self.submit()
        calls_after_fill = self.venue.calls
        self.venue.venue = "paper"
        worker = PaperWorker(self.authority)
        account = self.ledger.connection.execute("SELECT account_id FROM fills").fetchone()[0]
        worker.attach_activity(self.ledger.connection, account, "f" * 64)
        day = 86_400 * 1_000_000
        midnight = self.now - (self.now % day)
        slot = midnight + 15 * 60 * 1_000_000
        times = iter((slot - 1, slot, slot + 1))

        def clock():
            return next(times)

        seen = {"n": 0}

        def stop_after_three():
            seen["n"] += 1
            return seen["n"] > 3

        turns = worker.serve(clock, lambda: None, stop_after_three)
        self.assertEqual(turns, 3)
        self.assertEqual(len(worker.activity_reports), 1)
        self.assertFalse(worker.activity_reports[0].approved)
        self.assertEqual(self.venue.calls, calls_after_fill)
        self.assertEqual(worker.considered, 0)

    def test_paper_fill_without_a_resolved_forecast_has_no_brier(self):
        from bot.autonomous.worker import PaperWorker
        self.prepare(asks=((D("100"), D("5")),))
        self.submit()
        calls = self.venue.calls
        self.venue.venue = "paper"
        account = self.ledger.connection.execute("SELECT account_id FROM fills").fetchone()[0]
        worker = PaperWorker(self.authority)
        worker.attach_activity(self.ledger.connection, account, "a" * 64)
        slot = 1728000000000 + 15 * 60 * 1_000_000
        result = worker.on_clock(slot)
        report = worker.activity_reports[0]
        self.assertTrue(result["reviewed"])
        self.assertFalse(result["dispatched"])
        self.assertFalse(report.approved)
        self.assertEqual(report.resolved, 0)
        self.assertIsNone(report.binary_brier)
        self.assertEqual(self.venue.calls, calls)

    def test_paper_venue_fill_scores_brier_without_approval(self):
        import dataclasses
        from bot.autonomous.calibration import HOUR_US, OutcomeEvidenceVerifier, VerifiedOutcomeEvidence
        from bot.autonomous.contracts import (AdvisoryRisk, CandidateDecision, DecisionBatch, Direction,
            DirectionDistribution, Regime, content_hash, strict_json)
        from bot.autonomous.worker import PaperWorker
        self.prepare(asks=((D("100"), D("5")),))
        close = self.now - 1000
        release = self.ledger.connection.execute("SELECT release_hash FROM snapshots").fetchone()[0]
        snapshot_id = "c" * 64
        state_json = "{}"
        judgment = CandidateDecision("aave", Regime.TRENDING, Direction.LONG, 2, AdvisoryRisk.SAFE,
            DirectionDistribution(.7, .2, .1), .7, .7, .7, .7, .1)
        batch = DecisionBatch("request-venue", content_hash(strict_json(state_json)), release, "test",
            100, 90, self.now, (judgment,), None, schema_hash="a" * 64)
        body = batch.to_json()
        self.ledger.connection.execute(
            "INSERT INTO snapshots(snapshot_id,account_id,release_hash,candle_close_us,knowledge_cutoff_us,field_hash,state_json,lineage_hash,account_revision,valid,tokenizer_hash,token_count,recorded_at_us) VALUES (?,'paper-test',?,?,?,?,'{}',?,'{}',1,?,?,?)",
            (snapshot_id, release, close, close, "a" * 64, "d" * 64, "a" * 64, 10, self.now))
        self.ledger.connection.execute(
            "INSERT INTO decisions(decision_id,snapshot_id,request_id,deadline_us,model_id,response_json,response_hash,status,received_at_us,recorded_at_us) VALUES (?,?,?,?,?,?,?,'completed',?,?)",
            ("decision-venue", snapshot_id, "request-venue", self.now + 100_000_000, "test", body, content_hash(strict_json(body)), self.now, self.now))
        self.ledger.connection.execute(
            "INSERT INTO candidate_results(decision_id,candidate_id,status,veto_codes_json,recorded_at_us) VALUES ('decision-venue',1,'eligible','[]',?)",
            (self.now,))
        self.ledger.connection.commit()
        self.proposal = dataclasses.replace(self.proposal, candle_close_us=close,
            decision=dataclasses.replace(self.proposal.decision, decision_id="decision-venue",
                request_id="request-venue", snapshot_hash=snapshot_id, release_hash=release))
        self.ledger.append_forecast(forecast_id="forecast-venue", decision_id="decision-venue", candidate_id=1,
            side="buy", execution_policy_hash="e" * 64, eligibility=False, recorded_at_us=self.now)
        self.submit()
        calls = self.venue.calls
        fill_id = self.ledger.connection.execute("SELECT fill_id FROM fills").fetchone()[0]

        class SyntheticVerifier(OutcomeEvidenceVerifier):
            def verify(self, request):
                quote = request.forecast_at_us + 100
                closes = tuple((quote // HOUR_US + i) * HOUR_US for i in range(1, 5))
                return VerifiedOutcomeEvidence(self, request.request_hash, quote, quote + 1, quote + 2,
                    closes, closes[-1], closes[-1] + 1, closes[-1] + 2, closes[-1] + 3,
                    D("2"), D("100"), D("101"), D(".5"), D(".5"), D(".25"), "f" * 64, ("e" * 64,), None)

        label = self.ledger.connection.execute("SELECT * FROM outcome_labels WHERE forecast_id='forecast-venue'").fetchone()
        forecast_at = self.ledger.connection.execute(
            "SELECT recorded_at_us FROM forecasts WHERE forecast_id='forecast-venue'").fetchone()[0]
        verified_at = (((forecast_at + 100) // HOUR_US) + 4) * HOUR_US + 3
        self.ledger.configure_outcome_verifier(SyntheticVerifier())
        resolve_at = max(label["maturity_us"], verified_at)
        self.now = resolve_at + 10
        self.ledger.revise_outcome_label(label["label_id"], "resolved", {"net_return": "999"}, recorded_at_us=resolve_at)
        self.venue.venue = "paper"
        account = self.ledger.connection.execute("SELECT account_id FROM fills").fetchone()[0]
        worker = PaperWorker(self.authority)
        worker.attach_activity(self.ledger.connection, account, "a" * 64)
        result = worker.on_clock(resolve_at)
        report = worker.activity_reports[0]
        self.assertTrue(result["reviewed"])
        self.assertFalse(result["dispatched"])
        self.assertFalse(report.approved)
        self.assertEqual(report.included_ids, (fill_id,))
        self.assertAlmostEqual(report.binary_brier, (0.7 - 1) ** 2)
        self.assertEqual(self.venue.calls, calls)

    def test_serve_loop_scores_only_the_0015_turn(self):
        import sqlite3
        from bot.autonomous.worker import PaperWorker
        self.prepare(asks=((D("100"), D("5")),))
        self.submit()
        calls = self.venue.calls
        self.venue.venue = "paper"
        account = self.ledger.connection.execute("SELECT account_id FROM fills").fetchone()[0]
        worker = PaperWorker(self.authority)
        worker.attach_activity(self.ledger.connection, account, "f" * 64)
        slots = sqlite3.connect(":memory:")
        slot = 1728000000000 + 15 * 60 * 1_000_000
        times = iter((slot - 1, slot, slot + 1))
        seen = {"n": 0}

        def stop():
            seen["n"] += 1
            return seen["n"] > 3

        turns = worker.serve(lambda: next(times), lambda: None, stop, slots)
        self.assertEqual(turns, 3)
        self.assertEqual(len(worker.activity_reports), 1)
        self.assertFalse(worker.activity_reports[0].approved)
        self.assertEqual(worker.activity_reports[0].resolved, 0)
        self.assertGreaterEqual(worker.activity_reports[0].pending, 1)
        self.assertEqual(self.venue.calls, calls)

    def test_clock_before_0015_does_not_score_and_0015_does(self):
        import sqlite3
        from bot.autonomous.worker import PaperWorker
        self.prepare(asks=((D("100"), D("5")),))
        self.submit()
        calls = self.venue.calls
        self.venue.venue = "paper"
        account = self.ledger.connection.execute("SELECT account_id FROM fills").fetchone()[0]
        worker = PaperWorker(self.authority)
        worker.attach_activity(self.ledger.connection, account, "e" * 64)
        slots = sqlite3.connect(":memory:")
        slot = 1728000000000 + 15 * 60 * 1_000_000
        self.assertFalse(worker.claim_review_slot(slots, slot - 1))
        early = worker.on_clock(slot - 1)
        self.assertFalse(early["reviewed"])
        self.assertFalse(early["dispatched"])
        self.assertEqual(worker.activity_reports, [])
        self.assertTrue(worker.claim_review_slot(slots, slot))
        due = worker.on_clock(slot)
        self.assertTrue(due["reviewed"])
        self.assertFalse(due["dispatched"])
        self.assertFalse(worker.activity_reports[0].approved)
        self.assertEqual(worker.activity_reports[0].resolved, 0)
        self.assertGreaterEqual(worker.activity_reports[0].pending, 1)
        self.assertEqual(self.venue.calls, calls)

    def test_clock_once_scores_the_open_ledger_without_approving(self):
        import json
        import tempfile
        from pathlib import Path
        from bot.autonomous.worker import main
        self.prepare(asks=((D("100"), D("5")),))
        self.submit()
        account = self.ledger.connection.execute("SELECT account_id FROM fills").fetchone()[0]
        slot_db = Path(tempfile.mkdtemp()) / "slots.sqlite"
        from io import StringIO
        from contextlib import redirect_stdout
        first_buf = StringIO()
        with redirect_stdout(first_buf):
            code = main(["--clock-once", "--ledger", str(self.path), "--account", account, "--slot-db", str(slot_db)])
        second_buf = StringIO()
        with redirect_stdout(second_buf):
            again = main(["--clock-once", "--ledger", str(self.path), "--account", account, "--slot-db", str(slot_db)])
        first = json.loads(first_buf.getvalue())
        second = json.loads(second_buf.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(again, 0)
        self.assertTrue(first["reviewed"])
        self.assertFalse(first["dispatched"])
        self.assertFalse(first["approved"])
        self.assertGreaterEqual(first["pending"], 1)
        self.assertEqual(first["resolved"], 0)
        self.assertFalse(second["reviewed"])
        self.assertFalse(second["dispatched"])
        self.assertEqual(self.venue.calls, 1)

    def test_clock_review_never_runs_authority_recovery_on_the_active_ledger(self):
        from bot.autonomous.worker import main
        from io import StringIO
        from contextlib import redirect_stdout
        self.prepare()
        def authority_state():
            return {table: [tuple(row) for row in self.ledger.connection.execute("SELECT * FROM " + table)]
                for table in ("accounts", "account_control", "account_readiness", "order_intents")}
        before = authority_state()
        with redirect_stdout(StringIO()):
            main(["--clock-once", "--ledger", str(self.path), "--account", "paper-test",
                "--slot-db", str(self.path.parent / "slots.sqlite")])
        self.assertEqual(before, authority_state())

    def test_clock_reviews_at_0015_without_submitting(self):
        from bot.autonomous.worker import PaperWorker
        self.prepare(asks=((D("100"), D("5")),))
        self.submit()
        calls_after_fill = self.venue.calls
        self.venue.venue = "paper"
        worker = PaperWorker(self.authority)
        account = self.ledger.connection.execute("SELECT account_id FROM fills").fetchone()[0]
        worker.attach_activity(self.ledger.connection, account, "e" * 64)
        day = 86_400 * 1_000_000
        midnight = self.now - (self.now % day)
        slot = midnight + 15 * 60 * 1_000_000
        if self.now >= slot:
            slot += day
        early = worker.on_clock(slot - 1)
        due = worker.on_clock(slot)
        again = worker.on_clock(slot + 1)
        self.assertFalse(early["reviewed"])
        self.assertTrue(due["reviewed"])
        self.assertFalse(due["dispatched"])
        self.assertFalse(again["reviewed"])
        self.assertEqual(self.venue.calls, calls_after_fill)
        self.assertEqual(len(worker.activity_reports), 1)
        self.assertFalse(worker.activity_reports[0].approved)

    def test_worker_reviews_ledger_activity_once_after_0015_utc(self):
        from bot.autonomous.paper_cycle import CycleOutcome
        from bot.autonomous.worker import PaperWorker
        self.prepare(asks=((D("100"), D("5")),))
        self.submit()
        self.venue.venue = "paper"
        worker = PaperWorker(self.authority)
        account = self.ledger.connection.execute("SELECT account_id FROM fills").fetchone()[0]
        worker.attach_activity(self.ledger.connection, account, "d" * 64)
        day = 86_400 * 1_000_000
        midnight = self.now - (self.now % day)
        slot = midnight + 15 * 60 * 1_000_000
        if self.now >= slot:
            slot += day
        hold = lambda cycle: CycleOutcome(False, ("HOLD",))
        worker.on_close(min(self.now, slot - 1), hold)
        self.assertEqual(worker.activity_reports, [])
        worker.on_close(slot, hold)
        worker.on_close(slot + 1, hold)
        self.assertEqual(len(worker.activity_reports), 1)
        report = worker.activity_reports[0]
        self.assertFalse(report.approved)
        self.assertEqual(dict(report.counts)["fill"], 1)
        self.assertEqual(report.resolved, 0)
        self.assertIsNone(report.binary_brier)

    def test_activity_review_counts_a_fill_without_scoring_it(self):
        from bot.autonomous.nightly import review_account_activity
        self.prepare(asks=((D("100"), D("5")),))
        self.submit()
        account = self.ledger.connection.execute("SELECT account_id FROM fills").fetchone()[0]
        filled = review_account_activity(self.ledger.connection, account, self.now, "b" * 64)
        self.assertEqual(dict(filled.counts)["fill"], 1)
        self.assertEqual(filled.resolved, 0)
        self.assertIsNone(filled.binary_brier)
        self.assertFalse(filled.approved)

    def test_unfilled_cancel_is_a_pending_miss(self):
        from bot.autonomous.nightly import review_account_activity
        self.prepare(asks=((D("100.5"), D("10")),))
        self.submit()
        account = self.ledger.connection.execute("SELECT account_id FROM order_intents").fetchone()[0]
        missed = review_account_activity(self.ledger.connection, account, self.now, "c" * 64)
        self.assertEqual(dict(missed.counts)["miss"], 1)
        self.assertEqual(dict(missed.counts)["fill"], 0)
        self.assertIsNone(missed.binary_brier)
        self.assertFalse(missed.approved)

    def test_paper_fill_enters_nightly_review_as_pending(self):
        from bot.autonomous.brain import DeterministicBrain
        from bot.autonomous.nightly import SessionView, events_from_fill_rows, review_session
        self.prepare(asks=((D("100"), D("5")),))
        self.submit()
        rows = list(self.ledger.connection.execute("SELECT fill_id, event_at_us FROM fills"))
        events = events_from_fill_rows(rows, cutoff_us=self.now)
        report = review_session(SessionView("paper-test", self.now, "a" * 64, events),
                                DeterministicBrain(), job_id="after-fill")
        self.assertEqual(dict(report.counts)["fill"], 1)
        self.assertEqual(report.resolved, 0)
        self.assertEqual(report.pending, 1)
        self.assertIsNone(report.binary_brier)
        self.assertFalse(report.approved)

    def test_full_fill(self):
        self.prepare(asks=((D("100"), D("5")),))
        receipt = self.submit()
        self.assertEqual(IntentState.FILLED, receipt.state, receipt.reason_codes)
        self.assertEqual(["reserved", "dispatching", "filled"], self.event_kinds())
        self.assertEqual(1, self.ledger.connection.execute("SELECT count(*) FROM fills").fetchone()[0])

    def test_nonfill_above_limit_cancels_without_fill(self):
        self.prepare(asks=((D("100.5"), D("10")),))
        receipt = self.submit()
        self.assertEqual(IntentState.CANCELED, receipt.state, receipt.reason_codes)
        self.assertEqual(["reserved", "dispatching", "open", "canceled"], self.event_kinds())
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM fills").fetchone()[0])

    def test_risk_veto_never_reaches_venue(self):
        self.prepare()
        self.ledger.connection.execute("UPDATE account_control SET hard_killed=1")
        receipt = self.submit()
        self.assertEqual(ReceiptStatus.REJECTED, receipt.status)
        self.assertEqual(0, self.venue.calls)
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM order_intents").fetchone()[0])

    def test_entry_size_not_reproduced_never_reaches_venue(self):
        self.prepare(verifier=lambda *args: False)
        receipt = self.submit()
        self.assertEqual(("ENTRY_SIZING_NOT_REPRODUCED",), receipt.reason_codes)
        self.assertEqual(0, self.venue.calls)

    def test_missing_stale_or_foreign_book_refuses(self):
        self.prepare()
        good = self.book_source
        for name, source in {"missing": lambda i: None,
                             "stale": lambda i: dataclasses.replace(good(i), observed_at_us=self.now - 2_000_001),
                             "future": lambda i: dataclasses.replace(good(i), observed_at_us=self.now + 1),
                             "foreign": lambda i: dataclasses.replace(good(i), instrument="ETH-USD")}.items():
            with self.subTest(name):
                self.book_source = source
                self.assertEqual(("BOOK_UNAVAILABLE",), self.submit().reason_codes)
        self.assertEqual(0, self.venue.calls)

    def test_book_gone_at_dispatch_rejects_and_releases(self):
        self.prepare()
        good, reads = self.book_source, []
        def flaky(instrument):
            reads.append(1)
            return good(instrument) if len(reads) == 1 else None
        self.book_source = flaky
        receipt = self.submit()
        self.assertEqual(("BOOK_STALE_AT_DISPATCH",), receipt.reason_codes)
        self.assertEqual(0, self.venue.calls)
        self.assertEqual((), self.ledger.account_view("paper-test").reservations)

    def test_final_check_race_with_another_dispatcher_never_releases(self):
        # Another authority instance commits DISPATCHING, then this one's final check fails.
        self.prepare()
        real = self.ledger.mark_dispatching
        def dispatched_elsewhere(intent_id, generation):
            real(intent_id, generation)
            raise ContractViolation("current risk recheck refused/unbound")
        self.ledger.mark_dispatching = dispatched_elsewhere
        receipt = self.submit()
        self.assertEqual(ReceiptStatus.PENDING, receipt.status)
        self.assertEqual(("DISPATCH_STATE_UNCERTAIN",), receipt.reason_codes)
        self.assertEqual("dispatching", self.intent_state())
        self.assertEqual(1, len(self.ledger.account_view("paper-test").reservations))
        self.assertEqual(0, self.venue.calls)

    def test_venue_rejection_after_dispatch_keeps_capacity_until_reconciled(self):
        self.prepare(fee_rate=D("0.5"))  # above the market's verified 1% fee bound
        receipt = self.submit()
        self.assertEqual(IntentState.REJECTED, receipt.state)
        self.assertEqual(1, len(self.ledger.account_view("paper-test").reservations))
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM fills").fetchone()[0])

    def test_recording_failure_or_unaccounted_fill_becomes_unknown(self):
        for name, behaviour in {"raises": "raise", "unaccounted": "false"}.items():
            with self.subTest(name):
                self.tearDown(); self.setUp()
                self.prepare(asks=((D("100"), D("5")),))
                def record(evidence_id, behaviour=behaviour):
                    if behaviour == "raise":
                        raise ContractViolation("disk full")
                    return False
                self.ledger.record_fill = record
                receipt = self.submit()
                self.assertEqual(ReceiptStatus.PENDING, receipt.status)
                self.assertEqual(("RECORDING_FAILED",), receipt.reason_codes)
                self.assertEqual("unknown", self.intent_state())
                self.assertFalse(self.ledger.account_view("paper-test").ready)

    def test_slippage_rounds_to_tick_against_order_and_fills_are_accounted(self):
        self.prepare(asks=((D("99.97"), D("5")),), slippage_bps=D("2.5"))
        receipt = self.submit()
        self.assertEqual(IntentState.FILLED, receipt.state, receipt.reason_codes)
        price, fee = self.ledger.connection.execute("SELECT price,fee_qty FROM fills").fetchone()
        self.assertEqual(D("100"), D(price))  # 99.97 * 1.00025 = 99.9949... rounds up to 100.00 (limit)
        self.assertLessEqual(D(fee) * 1_000_000, self.proposal.max_fee_usd_u)
        self.assertTrue(self.ledger.account_view("paper-test").reservations is not None)

    def test_final_check_failure_rejects_intent_and_releases_capacity(self):
        self.prepare()
        def refuse(*args, **kwargs):
            raise ContractViolation("current risk recheck refused/unbound")
        self.ledger.mark_dispatching = refuse
        receipt = self.submit()
        self.assertEqual(ReceiptStatus.REJECTED, receipt.status)
        self.assertEqual(0, self.venue.calls)
        self.assertEqual("rejected", self.intent_state())
        self.assertEqual((), self.ledger.account_view("paper-test").reservations)
        self.reopen()
        self.assertEqual((), self.ledger.account_view("paper-test").reservations)

    def reopen(self):
        self.ledger.close()
        self.ledger = test_ledger.load_ledger().open(self.path)
        self.ledger._clock_us = lambda: self.now

    def test_venue_failure_becomes_unknown_and_keeps_capacity(self):
        self.prepare(error=ConnectionError("timeout after send"))
        receipt = self.submit()
        self.assertEqual(IntentState.UNKNOWN, receipt.state)
        self.assertEqual("unknown", self.intent_state())
        view = self.ledger.account_view("paper-test")
        self.assertEqual(1, len(view.reservations))
        self.assertFalse(view.ready)
        self.reopen()
        self.assertEqual((self.ledger.connection.execute("SELECT intent_id FROM order_intents").fetchone()[0],),
            self.ledger.account_view("paper-test").unknown_intent_ids)

    def test_same_candle_under_second_release_cannot_create_second_entry(self):
        self.prepare(asks=((D("100.5"), D("10")),))
        self.submit()
        other = dataclasses.replace(self.proposal, release_hash=content_hash({"release": 2}),
            decision=dataclasses.replace(self.proposal.decision, release_hash=content_hash({"release": 2})))
        receipt = self.submit(other)
        self.assertEqual(ReceiptStatus.REJECTED, receipt.status)
        self.assertEqual(1, self.ledger.connection.execute("SELECT count(*) FROM order_intents").fetchone()[0])
        self.assertEqual(1, self.venue.calls)

    def test_venue_called_outside_any_sql_transaction(self):
        self.prepare()
        self.venue.ledger = self.ledger
        self.submit()
        self.assertEqual(1, self.venue.calls)
        self.assertFalse(self.venue.in_transaction)

    def test_stale_epoch_forged_decision_and_invented_slot_never_reach_venue(self):
        p = self.prepare()
        cases = {
            "stale_epoch": (dataclasses.replace(p, control_epoch=99), p.decision),
            "forged_decision": (p, dataclasses.replace(p.decision, request_id="forged")),
            "invented_slot": (dataclasses.replace(p, action_slot="random-7"), p.decision),
        }
        for name, (proposal, decision) in cases.items():
            with self.subTest(name):
                receipt = self.authority.submit(proposal, decision=decision, request_id="r-" + name)
                self.assertEqual(ReceiptStatus.REJECTED, receipt.status)
        self.assertEqual(0, self.venue.calls)
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM order_intents").fetchone()[0])

    def test_retry_of_same_proposal_is_idempotent(self):
        self.prepare(asks=((D("100.5"), D("10")),))
        first = self.submit()
        again = self.submit()
        self.assertEqual(first.intent_id, again.intent_id)
        self.assertEqual(1, self.venue.calls)

    def test_authority_submits_one_latched_reduction_through_the_paper_venue(self):
        from bot.autonomous.contracts import MarketRules, Side
        from bot.autonomous.risk import assess as real_assess
        self.initialize()
        test_ledger.AccountingTests.intent(self, "parent", quantity="1", price="100")
        test_ledger.AccountingTests.fill(self, "parent-fill", "parent", "1", "100", "USD", "0")
        self.ledger.connection.execute("UPDATE order_intents SET state='filled' WHERE intent_id='parent'")
        self.ledger.connection.commit()
        self.mark("held", books={"AAVE": {"at_us": self.now, "quote_asset": "USD", "bids": [["100", "1"]]}})
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET permit='entry',permit_epoch=1,lease_boot_id='test-boot',lease_deadline_mono_ns=100000000000,active_release_hash=?,version=version+1", (test_ledger.RELEASE_HASH,))
        from bot.autonomous.contracts import RiskLimits
        rules = MarketRules("paper", "AAVE-USD", "AAVE", "USD", D(".01"), D(".01"), D(".01"), D("1"), D(".01"), 2, test_ledger.HASH_A, self.now + 100_000_000, True, True, self.now, 10_000_000_000)
        limits = RiskLimits(armed=True, candidate_eligible=True, tokenizer_config_hash=test_ledger.HASH_A, calibration_config_hash=test_ledger.HASH_A)
        self.ledger.configure_risk_checks(limits=limits, market=lambda _: rules, assess=real_assess, boot_id="test-boot", monotonic_ns=lambda: 1)
        self.ledger.connection.execute("UPDATE account_control SET drawdown_latched=1, daily_halted=1")
        self.ledger.connection.commit()
        from tests.autonomous.support import order_proposal
        sell = dataclasses.replace(order_proposal(), side=Side.SELL, reducing_only=True, decision=None,
            release_hash=test_ledger.RELEASE_HASH, control_epoch=1, deadline_mono_ns=100_000_000_000,
            base_quantity=D("0.1"), action_slot="reduce:parent:1", action_id="reduce-parent")
        x = execution()
        venue = RecordingVenue(x.PaperVenue(fee_rate=D("0.001"), slippage_bps=D("0"), clock_us=lambda: self.now))
        venue.ledger = self.ledger
        authority = x.OrderAuthority(self.ledger, venue, book_source=lambda instrument: ExecutableBook(
            "paper", instrument, self.now, BookPayload(((D("100"), D("1")),), ((D("101"), D("1")),)), "e" * 64),
            market_source=lambda instrument: rules, entry_verifier=lambda *args: False, clock_us=lambda: self.now,
            book_verifier=lambda book: True, market_capability_verifier=lambda market, now: True)
        receipt = authority.submit(sell, decision=None, request_id="request-reduce-parent")
        self.assertEqual(ReceiptStatus.COMPLETED, receipt.status, receipt.reason_codes)
        self.assertEqual(IntentState.FILLED, receipt.state)
        self.assertEqual(1, venue.calls)
        self.assertFalse(venue.in_transaction)
        self.assertEqual("0.9", self.balances()["AAVE"])

    def test_hard_kill_stops_a_reduction_before_the_venue(self):
        from bot.autonomous.contracts import MarketRules, RiskLimits, Side
        from bot.autonomous.risk import assess as real_assess
        from tests.autonomous.support import order_proposal
        self.initialize()
        test_ledger.AccountingTests.intent(self, "parent", quantity="1", price="100")
        test_ledger.AccountingTests.fill(self, "parent-fill", "parent", "1", "100", "USD", "0")
        self.ledger.connection.execute("UPDATE order_intents SET state='filled' WHERE intent_id='parent'")
        self.ledger.connection.commit()
        self.mark("held", books={"AAVE": {"at_us": self.now, "quote_asset": "USD", "bids": [["100", "1"]]}})
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET permit='entry',permit_epoch=1,lease_boot_id='test-boot',lease_deadline_mono_ns=100000000000,active_release_hash=?,hard_killed=1,version=version+1", (test_ledger.RELEASE_HASH,))
        rules = MarketRules("paper", "AAVE-USD", "AAVE", "USD", D(".01"), D(".01"), D(".01"), D("1"), D(".01"), 2, test_ledger.HASH_A, self.now + 100_000_000, True, True, self.now, 10_000_000_000)
        limits = RiskLimits(armed=False, candidate_eligible=True, tokenizer_config_hash=test_ledger.HASH_A, calibration_config_hash=test_ledger.HASH_A)
        self.ledger.configure_risk_checks(limits=limits, market=lambda _: rules, assess=real_assess, boot_id="test-boot", monotonic_ns=lambda: 1)
        sell = dataclasses.replace(order_proposal(), side=Side.SELL, reducing_only=True, decision=None,
            release_hash=test_ledger.RELEASE_HASH, control_epoch=1, deadline_mono_ns=100_000_000_000,
            base_quantity=D("0.1"), action_slot="reduce:parent:1", action_id="reduce-parent")
        x = execution()
        venue = RecordingVenue(x.PaperVenue(fee_rate=D("0.001"), slippage_bps=D("0"), clock_us=lambda: self.now))
        venue.ledger = self.ledger
        authority = x.OrderAuthority(self.ledger, venue, book_source=lambda instrument: ExecutableBook(
            "paper", instrument, self.now, BookPayload(((D("100"), D("1")),), ((D("101"), D("1")),)), "e" * 64),
            market_source=lambda instrument: rules, entry_verifier=lambda *args: True, clock_us=lambda: self.now,
            book_verifier=lambda book: True, market_capability_verifier=lambda market, now: True)
        receipt = authority.submit(sell, decision=None, request_id="request-killed-reduce")
        orders = self.ledger.connection.execute("SELECT count(*) FROM order_intents").fetchone()[0]
        self.assertEqual(ReceiptStatus.REJECTED, receipt.status)
        self.assertIn("HARD_KILL", receipt.reason_codes)
        self.assertEqual(0, venue.calls)
        self.assertEqual(1, orders)
        self.assertEqual("1", self.balances()["AAVE"])

    def test_crisis_latch_stops_a_reduction_before_the_venue(self):
        from bot.autonomous.contracts import MarketRules, RiskLimits, Side
        from bot.autonomous.risk import assess as real_assess
        from tests.autonomous.support import order_proposal
        self.initialize()
        test_ledger.AccountingTests.intent(self, "parent", quantity="1", price="100")
        test_ledger.AccountingTests.fill(self, "parent-fill", "parent", "1", "100", "USD", "0")
        self.ledger.connection.execute("UPDATE order_intents SET state='filled' WHERE intent_id='parent'")
        self.ledger.connection.commit()
        self.mark("held", books={"AAVE": {"at_us": self.now, "quote_asset": "USD", "bids": [["100", "1"]]}})
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET permit='entry',permit_epoch=1,lease_boot_id='test-boot',lease_deadline_mono_ns=100000000000,active_release_hash=?,version=version+1", (test_ledger.RELEASE_HASH,))
        rules = MarketRules("paper", "AAVE-USD", "AAVE", "USD", D(".01"), D(".01"), D(".01"), D("1"), D(".01"), 2, test_ledger.HASH_A, self.now + 100_000_000, True, True, self.now, 10_000_000_000)
        limits = RiskLimits(armed=True, candidate_eligible=True, tokenizer_config_hash=test_ledger.HASH_A, calibration_config_hash=test_ledger.HASH_A)
        self.ledger.configure_risk_checks(limits=limits, market=lambda _: rules, assess=real_assess, boot_id="test-boot", monotonic_ns=lambda: 1)
        latched = self.ledger.latch_account_crisis("paper-test", "incident-1", "regime crisis")
        control = self.ledger.connection.execute(
            "SELECT permit, crisis_halted, lease_boot_id FROM account_control").fetchone()
        sell = dataclasses.replace(order_proposal(), side=Side.SELL, reducing_only=True, decision=None,
            release_hash=test_ledger.RELEASE_HASH, control_epoch=1, deadline_mono_ns=100_000_000_000,
            base_quantity=D("0.1"), action_slot="reduce:parent:1", action_id="reduce-parent")
        x = execution()
        venue = RecordingVenue(x.PaperVenue(fee_rate=D("0.001"), slippage_bps=D("0"), clock_us=lambda: self.now))
        venue.ledger = self.ledger
        authority = x.OrderAuthority(self.ledger, venue, book_source=lambda instrument: ExecutableBook(
            "paper", instrument, self.now, BookPayload(((D("100"), D("1")),), ((D("101"), D("1")),)), "e" * 64),
            market_source=lambda instrument: rules, entry_verifier=lambda *args: True, clock_us=lambda: self.now,
            book_verifier=lambda book: True, market_capability_verifier=lambda market, now: True)
        receipt = authority.submit(sell, decision=None, request_id="request-crisis-reduce")
        self.assertTrue(latched)
        self.assertEqual("none", control[0])
        self.assertEqual(1, control[1])
        self.assertIsNone(control[2])
        self.assertEqual(ReceiptStatus.REJECTED, receipt.status)
        self.assertEqual(("CRISIS_HALT",), receipt.reason_codes)
        self.assertEqual(0, venue.calls)
        self.assertEqual("1", self.balances()["AAVE"])


class EntryVerifierTests(unittest.TestCase):
    """Production verifier: recompute size_entry from persisted evidence and the authority's own book."""

    def setUp(self):
        from tests.autonomous.test_policy_boundaries import EntryCompositionTests
        self.c = EntryCompositionTests("test_all_gates_pass_into_bounded_entry_proposal")
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)

    def verifier(self):
        s = self.c.s
        evidence = {"decision-live": (s.batch, s.decision, s.snapshot, s.artifact, s.edge)}
        return execution().sizing_entry_verifier(s.ledger, evidence.get, self.c.context.limits)

    def test_verifier_is_bound_to_the_proposals_own_decision(self):
        s = self.c.s
        proposal = self.c.evaluate().proposal
        verify = self.verifier()
        for name, changed in {
                "other_decision": dataclasses.replace(proposal, decision=dataclasses.replace(proposal.decision, decision_id="decision-other")),
                "other_request": dataclasses.replace(proposal, decision=dataclasses.replace(proposal.decision, request_id="request-other")),
                "shifted_candle": dataclasses.replace(proposal, candle_close_us=proposal.candle_close_us + 3_600_000_000),
                "other_action": dataclasses.replace(proposal, action_id="action-other"),
                "no_decision": dataclasses.replace(proposal, decision=None, reducing_only=True)}.items():
            with self.subTest(name):
                try:
                    self.assertFalse(verify(changed, self.c.account, s.book, s.market, s.now))
                except ContractViolation:
                    pass  # contract itself refuses the shape

    def test_reproduced_entry_passes_and_any_altered_economics_fail(self):
        s = self.c.s
        proposal = self.c.evaluate().proposal
        verify = self.verifier()
        self.assertTrue(verify(proposal, self.c.account, s.book, s.market, s.now))
        for change in ({"base_quantity": D("5")}, {"limit_price": D("100.11")},
                       {"max_fee_usd_u": 400_000}, {"loss_budget_usd_u": 24_000_000}):
            with self.subTest(change):
                self.assertFalse(verify(dataclasses.replace(proposal, **change), self.c.account, s.book, s.market, s.now))
        # A thinner live book than the one the proposal was sized on changes the size.
        thin = dataclasses.replace(s.book, book=BookPayload(s.book.book.bids, ((D("100"), D("100")),)))
        self.assertFalse(verify(proposal, self.c.account, thin, s.market, s.now))


if __name__ == "__main__":
    unittest.main()
