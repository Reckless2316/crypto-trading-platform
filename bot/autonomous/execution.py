"""One order authority for paper (and later live) execution.

The authority alone turns a proposal into venue traffic: it builds the executable
book from its own source, re-verifies the entry size, reserves durably, passes the
ledger's final committed check, and only then calls the venue - never inside a SQL
transaction. Every outcome is recorded as order events and fill evidence. Paper
execution emits venue-like results; it never edits positions or cash directly.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from fractions import Fraction
from typing import Any, Callable, Protocol

from .contracts import (
    AccountView, ContractViolation, DecisionReference, ExecutableBook, IntentReceipt, IntentState,
    MarketRules, Mode, OrderEvent, OrderProposal, OrderReference, PersistenceUnavailable, PreparedIntent,
    ReceiptStatus, ReconcileReason, ReconcileReport, Side, SizedProposal, StateUnavailable, VenueOrderResult, content_hash, decimal_text,
)

_REFUSALS = (ContractViolation, StateUnavailable, PersistenceUnavailable, ValueError)


def reservation_releasable(terminal, filled, accounted, fees_final) -> bool:
    """Necessary terminal proof, never a substitute for account reconciliation.

    A cancel acknowledgment or an incomplete fee report cannot free capacity.
    Unknown, nonfinite and untyped quantities conservatively retain it.
    """
    return (terminal is True and fees_final is True
        and type(filled) is Decimal and type(accounted) is Decimal
        and filled.is_finite() and accounted.is_finite()
        and filled >= 0 and accounted >= 0 and filled == accounted)


def economic_action_key(account_id: str, candle_close_us: int, candidate_id: int, action_slot: str) -> str:
    """Stable identity of one economic action; independent of release, retry or restart."""
    if (type(account_id) is not str or not account_id or type(candle_close_us) is not int or candle_close_us < 0
            or type(candidate_id) is not int or candidate_id <= 0 or type(action_slot) is not str or not action_slot):
        raise ValueError("invalid economic action")
    return content_hash([account_id, candle_close_us, candidate_id, action_slot])


@dataclass(frozen=True)
class VenueExecution:
    """Ordered venue results (non-terminal first) and the fills they produced."""
    results: tuple[VenueOrderResult, ...]
    fills: tuple[dict, ...]


class VenuePort(Protocol):
    def submit(self, intent: PreparedIntent, book: ExecutableBook, market: MarketRules) -> VenueExecution: ...


def _round(value: Decimal, step: Decimal, rounding) -> Decimal:
    return (value / step).to_integral_value(rounding=rounding) * step


class PaperVenue:
    """Bounded IOC limit orders filled against the supplied causal book with adverse costs.

    Buys walk asks and sells walk bids, only at prices within the limit. Each level
    fills at its price moved against the order by `slippage_bps` and rounded to the
    tick against the order (never past the limit); quantities are floored to the lot.
    Fees are `fee_rate` of quote value in USD, rounded up to a whole microdollar; the
    rate defaults to the market's verified fee bound and may never exceed it. Any
    remainder is cancelled.
    """

    venue = "paper"

    def __init__(self, *, fee_rate: Decimal | None = None, slippage_bps: Decimal, clock_us: Callable[[], int]):
        if (fee_rate is not None and (type(fee_rate) is not Decimal or fee_rate <= 0)) or type(slippage_bps) is not Decimal or slippage_bps < 0:
            raise ValueError("paper venue needs a positive fee rate and non-negative slippage")
        self.fee_rate, self.slippage_bps, self.clock_us = fee_rate, slippage_bps, clock_us

    def submit(self, intent: PreparedIntent, book: ExecutableBook, market: MarketRules) -> VenueExecution:
        p = intent.proposal
        buy = p.side is Side.BUY
        order_id = "paper-" + intent.intent_id
        reference = OrderReference(p.account_id, intent.intent_id, intent.client_order_id, order_id)
        now = self.clock_us()
        fee_rate = market.fee_bound if self.fee_rate is None else self.fee_rate
        if fee_rate > market.fee_bound or fee_rate <= 0:
            rejected = VenueOrderResult(reference, IntentState.REJECTED, Decimal(0), p.base_quantity, now,
                f"{order_id}-rejected", "FEE_RATE_ABOVE_MARKET_BOUND")
            return VenueExecution((rejected,), ())
        levels = book.book.asks if buy else book.book.bids
        remaining, fills = p.base_quantity, []
        fee_budget_u = p.max_fee_usd_u
        with localcontext() as context:
            context.prec = 50
            for index, (price, available) in enumerate(levels):
                if remaining <= 0 or (price > p.limit_price if buy else price < p.limit_price):
                    break
                move = price * self.slippage_bps / 10_000
                if buy:
                    executed = min(p.limit_price, _round(price + move, market.tick_size, ROUND_CEILING))
                else:
                    executed = max(p.limit_price, _round(price - move, market.tick_size, ROUND_FLOOR))
                # Every fill rounds its fee upward. Reserve that rounding before
                # emitting a fill, since many level fees can exceed the ceiling
                # on one aggregate notional. Cancel any unaffordable remainder.
                fee_per_lot_u = Fraction(market.lot_size) * Fraction(executed) * Fraction(fee_rate) * 1_000_000
                affordable_lots = Fraction(fee_budget_u) // fee_per_lot_u
                quantity = _round(min(available, remaining), market.lot_size, ROUND_FLOOR)
                quantity = min(quantity, market.lot_size * affordable_lots)
                if quantity <= 0:
                    continue
                quote = quantity * executed
                fee = _round(quote * fee_rate, Decimal("0.000001"), ROUND_CEILING)
                fee_budget_u -= int(fee * 1_000_000)
                fills.append({"fill_id": f"{order_id}-{index}", "intent_id": intent.intent_id, "venue": self.venue,
                    "trade_id": f"{order_id}-{index}", "order_id": order_id, "instrument": p.instrument,
                    "side": p.side.value, "base_quantity": decimal_text(quantity), "quote_quantity": decimal_text(quote),
                    "price": decimal_text(executed), "fee_asset": "USD", "fee_quantity": decimal_text(fee),
                    "base_semantics": "gross", "fee_final": True})
                remaining -= quantity
        filled = p.base_quantity - remaining
        result = lambda state, key: VenueOrderResult(reference, state, filled, remaining, now, f"{order_id}-{key}")
        if remaining == 0:
            results = (result(IntentState.FILLED, "filled"),)
        elif filled > 0:
            results = (result(IntentState.PARTIAL, "partial"), result(IntentState.CANCELED, "ioc-cancel"))
        else:
            results = (result(IntentState.OPEN, "open"), result(IntentState.CANCELED, "ioc-cancel"))
        return VenueExecution(results, tuple(fills))


def sizing_entry_verifier(ledger, evidence_for: Callable[[str], tuple | None], limits):
    """Production entry verifier (T10-4), bound to the proposal's own persisted decision.

    `evidence_for(decision_id)` returns the `(batch, decision, snapshot, artifact, edge)`
    the policy used for that decision. The decision must verify against the ledger,
    the proposal's full identity must match it, and a fresh `size_entry` on the
    authority's book must reproduce the proposal's economics exactly.
    """
    from .sizing import size_entry

    def verify(proposal: OrderProposal, account: AccountView, book: ExecutableBook,
               market: MarketRules, now_us: int) -> bool:
        reference = proposal.decision
        evidence = evidence_for(reference.decision_id) if reference is not None else None
        if evidence is None:
            return False
        batch, decision, snapshot, artifact, edge = evidence
        persisted = ledger.verified_decision_snapshot(batch, snapshot)
        if (persisted is None or persisted["decision_id"] != reference.decision_id
                or batch.request_id != reference.request_id
                or not reference.release_hash == batch.bundle_hash == proposal.release_hash
                or reference.snapshot_hash != snapshot.snapshot_hash
                or proposal.candle_close_us != snapshot.cutoffs.candle_close_us
                or not proposal.candidate_id == decision.candidate_id == snapshot.candidate_id
                or proposal.action_id != content_hash(("entry-v1", reference.decision_id, decision.candidate_id))
                or proposal.side is not Side.BUY or proposal.action_slot != "entry" or proposal.reducing_only):
            return False
        sized = size_entry(edge, account, market, limits, book, now_us=now_us,
            edge_inputs=(batch, decision, snapshot, artifact), ledger=ledger)
        return (type(sized) is SizedProposal and
            (sized.instrument, sized.base_quantity, sized.limit_price, sized.max_fee_usd_u, sized.loss_budget_usd_u)
            == (proposal.instrument, proposal.base_quantity, proposal.limit_price, proposal.max_fee_usd_u,
                proposal.loss_budget_usd_u))
    return verify


class _Unaccounted(Exception):
    """A venue fill the ledger did not account; the outcome is unresolved."""


class OrderAuthority:
    """The only path from proposal to venue; one dispatch at a time per account.

    `book_source` and `market_source` are trusted wiring (production: the state
    engine's `executable_book` and a verified venue capability snapshot); callers of
    `submit` can supply neither.
    """

    BOOK_MAX_AGE_US = 2_000_000

    def __init__(self, ledger, venue: VenuePort, *, book_source: Callable[[str], ExecutableBook | None],
                 market_source: Callable[[str], MarketRules],
                 entry_verifier: Callable[[OrderProposal, AccountView, ExecutableBook, MarketRules, int], bool],
                 clock_us: Callable[[], int], reconciliation_source: Callable[[str], str] | None = None):
        self.ledger, self.venue, self.book_source, self.market_source = ledger, venue, book_source, market_source
        self.entry_verifier, self.clock_us = entry_verifier, clock_us
        self.reconciliation_source = reconciliation_source
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, account_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(account_id, threading.Lock())

    def _receipt(self, request_id, status, intent_id=None, state=None, *reasons):
        return IntentReceipt(request_id, status, intent_id, state, tuple(reasons))

    def reconcile(self, account_id: str, reason: ReconcileReason = ReconcileReason.PERIODIC) -> ReconcileReport:
        """Reconcile from authority-owned observations, never caller-supplied balances.

        Acquisition holds the account mutex but no SQL transaction. Mark admission
        and all terminal settlements then commit together, or retain all capacity.
        The paper evidence boundary is implemented; live requires its authenticated
        accounting importer before it can use this path.
        """
        if type(reason) is not ReconcileReason or type(account_id) is not str or not account_id:
            raise ContractViolation('typed reconciliation account/reason required')
        with self._lock(account_id):
            started = self.clock_us()
            self.ledger.invalidate_account(account_id)
            differences = ()
            if self.reconciliation_source is None:
                differences = ('RECONCILIATION_SOURCE_UNAVAILABLE',)
            else:
                try:
                    evidence_id = self.reconciliation_source(account_id)
                    with self.ledger.transaction():
                        evidence, _ = self.ledger._evidence(evidence_id, 'mark')
                        if evidence['account_id'] != account_id or evidence['event_at_us'] < started:
                            raise ContractViolation('reconciliation observation is stale or misbound')
                        self.ledger.record_mark(evidence_id)
                        account = self.ledger.account_view(account_id)
                        if not account.ready or account.unknown_intent_ids:
                            raise ContractViolation('account remains unreconciled')
                        for reservation in account.reservations:
                            if reservation.state in (IntentState.FILLED, IntentState.CANCELED, IntentState.REJECTED):
                                self.ledger.settle_reconciled_intent(evidence_id, reservation.intent_id)
                except _REFUSALS:
                    differences = ('RECONCILIATION_INCOMPLETE',)
                    self.ledger.invalidate_account(account_id)
            account = self.ledger.account_view(account_id)
            finished = self.clock_us()
            key = account.reconcile_id if not differences else 'reconcile-failed-' + content_hash(
                (account_id, started, finished, reason.value, differences))
            return ReconcileReport(key, account_id, reason, started, finished,
                not differences and account.ready, differences, account.revision)

    def _event(self, intent: PreparedIntent, result: VenueOrderResult) -> OrderEvent:
        now = self.ledger._clock_us()
        event_id = "evt-" + content_hash((intent.intent_id, result.state.value, result.venue_event_key))
        return OrderEvent(event_id, intent.intent_id, result.state, result, now, now, result.venue_event_key)

    def _mark_unknown(self, intent: PreparedIntent, reason: str) -> None:
        """Make an unresolved venue outcome durable; at minimum the account becomes unready."""
        p = intent.proposal
        reference = OrderReference(p.account_id, intent.intent_id, intent.client_order_id, None)
        state = self.ledger._prepared(intent.intent_id)
        result = VenueOrderResult(reference, IntentState.UNKNOWN, Decimal(0), p.base_quantity,
            self.ledger._clock_us(), f"authority-unknown-{state.state.value}", reason)
        try:
            self.ledger.append_order_event(self._event(intent, result))
        except Exception:
            self.ledger.invalidate_account(p.account_id)

    def _fresh_book(self, instrument: str, now: int) -> ExecutableBook | None:
        book = self.book_source(instrument)
        if (type(book) is not ExecutableBook or book.instrument != instrument
                or not 0 <= now - book.observed_at_us <= self.BOOK_MAX_AGE_US):
            return None
        return book

    def submit(self, proposal: OrderProposal, *, decision: DecisionReference | None, request_id: str) -> IntentReceipt:
        """Admit, reserve, final-check and dispatch one proposal; never relaxes a refusal."""
        if type(proposal) is not OrderProposal or type(request_id) is not str or not request_id:
            return self._receipt(request_id, ReceiptStatus.REJECTED, None, None, "INVALID_REQUEST")
        with self._lock(proposal.account_id):
            return self._submit_locked(proposal, decision, request_id)

    def _submit_locked(self, proposal, decision, request_id):
        book = self._fresh_book(proposal.instrument, self.clock_us())
        if book is None:
            return self._receipt(request_id, ReceiptStatus.REJECTED, None, None, "BOOK_UNAVAILABLE")
        try:
            account = self.ledger.account_view(proposal.account_id)
            market = self.market_source(proposal.instrument)
            generation = self.ledger.connection.execute('SELECT generation FROM accounts WHERE account_id=?',
                (proposal.account_id,)).fetchone()[0]
        except _REFUSALS + (TypeError, IndexError):
            return self._receipt(request_id, ReceiptStatus.REJECTED, None, None, "ACCOUNT_OR_MARKET_UNAVAILABLE")
        if type(market) is not MarketRules or market.instrument != proposal.instrument:
            return self._receipt(request_id, ReceiptStatus.REJECTED, None, None, "ACCOUNT_OR_MARKET_UNAVAILABLE")
        venue = getattr(self.venue, 'venue', None)
        expected_venue = self.ledger.connection.execute('SELECT venue FROM accounts WHERE account_id=?',
            (proposal.account_id,)).fetchone()[0]
        if ((account.mode is Mode.PAPER and venue != 'paper')
                or (account.mode is Mode.LIVE and (venue != expected_venue or venue == 'paper'))
                or account.mode is Mode.SHADOW):
            return self._receipt(request_id, ReceiptStatus.REJECTED, None, None, "VENUE_MODE_MISMATCH")
        if not proposal.reducing_only and not self.entry_verifier(proposal, account, book, market, self.clock_us()):
            return self._receipt(request_id, ReceiptStatus.REJECTED, None, None, "ENTRY_SIZING_NOT_REPRODUCED")
        try:
            intent = self.ledger.reserve_intent(proposal, account.revision, generation, decision)
        except _REFUSALS as exc:
            reason = "RESERVATION_REFUSED"
            if str(exc) == "disabled/stale control or deadline":
                row = self.ledger.connection.execute(
                    "SELECT hard_killed, crisis_halted FROM account_control WHERE account_id=?",
                    (proposal.account_id,)).fetchone()
                if row is not None and row[0]:
                    reason = "HARD_KILL"
                elif row is not None and row[1]:
                    reason = "CRISIS_HALT"
            return self._receipt(request_id, ReceiptStatus.REJECTED, None, None, reason)
        if intent.state is not IntentState.RESERVED:
            # A retry of an action that already left reservation is never re-dispatched.
            status = ReceiptStatus.PENDING if intent.state in (IntentState.DISPATCHING, IntentState.UNKNOWN) else ReceiptStatus.COMPLETED
            return self._receipt(request_id, status, intent.intent_id, intent.state, "ALREADY_SUBMITTED")
        # The book the venue sees is re-read immediately before the committed dispatch.
        book = self._fresh_book(proposal.instrument, self.clock_us())
        if book is None:
            return self._refuse_undispatched(request_id, intent, "BOOK_STALE_AT_DISPATCH")
        if not proposal.reducing_only:
            try:
                # Reproduce economics against the book actually sent to the venue.
                # The current intent already owns capacity; exclude only its own
                # reservation so verification does not charge that capacity twice.
                current = self.ledger.account_view(proposal.account_id)
                current = replace(current,
                    reservations=tuple(r for r in current.reservations if r.intent_id != intent.intent_id),
                    open_intent_ids=tuple(i for i in current.open_intent_ids if i != intent.intent_id))
                market = self.market_source(proposal.instrument)
                verified = (type(market) is MarketRules and market.instrument == proposal.instrument
                    and self.entry_verifier(proposal, current, book, market, self.clock_us()))
            except _REFUSALS:
                verified = False
            if not verified:
                return self._refuse_undispatched(request_id, intent, "ENTRY_SIZING_CHANGED_AT_DISPATCH")
        try:
            intent = self.ledger.mark_dispatching(intent.intent_id, generation)
        except _REFUSALS:
            return self._refuse_undispatched(request_id, intent, "FINAL_CHECK_REFUSED")
        # Committed DISPATCHING; the venue call holds no SQL transaction.
        try:
            execution = self.venue.submit(intent, book, market)
        except Exception:
            self._mark_unknown(intent, "VENUE_OUTCOME_UNKNOWN")
            return self._receipt(request_id, ReceiptStatus.PENDING, intent.intent_id, IntentState.UNKNOWN, "VENUE_OUTCOME_UNKNOWN")
        try:
            return self._record(request_id, intent, execution)
        except Exception:
            self._mark_unknown(intent, "RECORDING_FAILED")
            return self._receipt(request_id, ReceiptStatus.PENDING, intent.intent_id, IntentState.UNKNOWN, "RECORDING_FAILED")

    def _refuse_undispatched(self, request_id, intent, reason):
        if self.ledger.reject_undispatched(intent.intent_id, reason):
            return self._receipt(request_id, ReceiptStatus.REJECTED, intent.intent_id, IntentState.REJECTED, reason)
        # Someone else moved the intent past RESERVED: only venue evidence can resolve it.
        return self._receipt(request_id, ReceiptStatus.PENDING, intent.intent_id, None, "DISPATCH_STATE_UNCERTAIN")

    def _record(self, request_id, intent: PreparedIntent, execution: VenueExecution) -> IntentReceipt:
        results = execution.results
        for result in results[:-1]:
            self.ledger.append_order_event(self._event(intent, result))
        fee_u = 0
        for fill in execution.fills:
            now = self.ledger._clock_us()
            evidence = self.ledger.import_local_evidence(intent.proposal.account_id, "fill-" + fill["fill_id"], "fill",
                fill, event_at_us=now, received_at_us=now, known_at_us=now)
            if not self.ledger.record_fill(evidence):
                raise _Unaccounted(fill["fill_id"])
            fee_u += int((Decimal(fill["fee_quantity"]) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
        self.ledger.append_order_event(self._event(intent, results[-1]))
        if fee_u > intent.proposal.max_fee_usd_u:
            self.ledger.invalidate_account(intent.proposal.account_id)
            return self._receipt(request_id, ReceiptStatus.COMPLETED, intent.intent_id, results[-1].state, "FEE_BOUND_EXCEEDED")
        status = ReceiptStatus.PENDING if results[-1].state is IntentState.UNKNOWN else ReceiptStatus.COMPLETED
        return self._receipt(request_id, status, intent.intent_id, results[-1].state)
