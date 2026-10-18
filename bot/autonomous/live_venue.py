"""Coinbase Advanced acquisition through an exclusively owned injected CCXT client.

Official semantics consulted 2026-10-17:
https://docs.ccxt.com/docs/exchanges/coinbase
https://docs.ccxt.com/docs/manual#order-structure
https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/get-order
https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/list-orders
https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/list-fills
https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/accounts/list-accounts
https://github.com/ccxt/ccxt/blob/master/python/ccxt/coinbase.py

The current CCXT class for Advanced Trade is coinbase. The logical venue identity
here remains coinbaseadvanced; construction never selects a class or credentials.
Raw authenticated read endpoints preserve decimal text and cursor metadata that
unified parsed results can lose. Coinbase documents unstable history pagination.
`complete` means this bounded acquisition traversed to the advertised end, not
snapshot consistency, absence proof, accounting readiness, or final fee proof.
Every response is issuer-bound source evidence for a future authenticated ledger
importer. That importer must check issuer identity, source-account binding and raw
hashes, and verify terminal quantities/fees against fills/balances/transfers.
No record in this module authorizes reservation release or creates a journal.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Callable, Mapping

from bot.live_coinbase_adapter import ManagedBoundedTransport, LiveTradingRejected
from .contracts import (IntentState, MarketRules, OrderReference, VenueOrderResult,
    content_hash, decimal_text, decimal_value)
from .execution import VenueExecution

_ORDER = 'v3PrivateGetBrokerageOrdersHistoricalOrderId'
_ORDERS = 'v3PrivateGetBrokerageOrdersHistoricalBatch'
_FILLS = 'v3PrivateGetBrokerageOrdersHistoricalFills'
_ACCOUNTS = 'v3PrivateGetBrokerageAccounts'
_CANCEL = 'v3PrivatePostBrokerageOrdersBatchCancel'
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class AcquisitionUnavailable(ValueError):
    pass


def _id(value):
    if type(value) is not str or not 0 < len(value) <= 256 or any(ord(c) < 32 for c in value):
        raise AcquisitionUnavailable('INVALID_SOURCE_ID')
    return value


def _instrument(value):
    if type(value) is not str or not re.fullmatch(r'[A-Z0-9]+-USD', value):
        raise AcquisitionUnavailable('UNSUPPORTED_INSTRUMENT')
    return value


def _decimal(raw, *, positive=False):
    # Raw Coinbase financial fields must be strings. Never turn float into proof.
    if type(raw) is not str:
        raise AcquisitionUnavailable('INEXACT_SOURCE_NUMBER')
    try:
        value = decimal_value(format(Decimal(raw), 'f'))
    except (ValueError, ArithmeticError) as exc:
        raise AcquisitionUnavailable('INVALID_SOURCE_NUMBER') from exc
    if value < 0 or positive and value <= 0:
        raise AcquisitionUnavailable('INVALID_SOURCE_NUMBER')
    return value


def _optional_decimal(raw):
    return None if raw is None or raw == '' else _decimal(raw)


def _timestamp(raw):
    if type(raw) is not str or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z', raw):
        raise AcquisitionUnavailable('INVALID_SOURCE_TIME')
    try:
        delta = datetime.fromisoformat(raw.replace('Z', '+00:00')) - _EPOCH
        value = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    except ValueError as exc:
        raise AcquisitionUnavailable('INVALID_SOURCE_TIME') from exc
    if not 0 <= value <= 2**63 - 1:
        raise AcquisitionUnavailable('INVALID_SOURCE_TIME')
    # Nanosecond source times are floored to the declared microsecond contract;
    # original text, including sub-microsecond digits, stays in raw evidence.
    return value


def _iso(value):
    if type(value) is not int or value < 0:
        raise AcquisitionUnavailable('INVALID_WINDOW')
    try:
        return (_EPOCH + timedelta(microseconds=value)).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    except (OverflowError, ValueError) as exc:
        raise AcquisitionUnavailable('INVALID_WINDOW') from exc


def _exact_product(a, b, *, divide=False):
    with localcontext() as context:
        context.prec = 100
        value = a / b if divide else a * b
    expected = Fraction(a) / Fraction(b) if divide else Fraction(a) * Fraction(b)
    if Fraction(value) != expected:
        raise AcquisitionUnavailable('INEXACT_BASE_QUANTITY')
    decimal_text(value)
    return value


@dataclass(frozen=True)
class SourceEvidence:
    issuer: LiveVenue
    account_id: str
    portfolio_id: str
    venue: str
    endpoint: str
    requested_at_us: int
    received_at_us: int
    payload_hash: str
    payload_json: str


@dataclass(frozen=True)
class OrderEvidence:
    reference: OrderReference
    instrument: str
    state: IntentState = IntentState.UNKNOWN
    filled_quantity: Decimal | None = None
    remaining_quantity: Decimal | None = None
    total_fee: Decimal | None = None
    fee_asset: str | None = None
    fees_final: bool | None = None
    terminal: bool = False
    settled: bool | None = None
    complete: bool = False
    absence_proven: bool = False
    sources: tuple[SourceEvidence, ...] = ()
    reason_codes: tuple[str, ...] = ()
    event_at_us: int | None = None
    side: str | None = None
    base_quantity: Decimal | None = None
    limit_price: Decimal | None = None


@dataclass(frozen=True)
class FillObservation:
    account_id: str
    venue: str
    portfolio_id: str
    trade_id: str
    order_id: str
    instrument: str
    side: str
    base_quantity: Decimal
    quote_quantity: Decimal
    price: Decimal
    fee_asset: str | None
    fee_quantity: Decimal | None
    fees_final: bool | None
    event_at_us: int
    payload_hash: str
    source: SourceEvidence

    @property
    def dedupe_key(self):
        return self.account_id, self.venue, self.trade_id


@dataclass(frozen=True)
class FillConflict:
    previous_hash: str
    observation: FillObservation


@dataclass(frozen=True)
class FillEvidenceBatch:
    fills: tuple[FillObservation, ...]
    conflicts: tuple[FillConflict, ...]
    sources: tuple[SourceEvidence, ...]
    since_us: int
    until_us: int
    complete: bool
    reason_codes: tuple[str, ...]
    next_cursor: str | None = None
    absence_proven: bool = False


@dataclass(frozen=True)
class BalanceObservation:
    wallet_id: str
    asset: str
    available: Decimal
    hold: Decimal
    source: SourceEvidence


@dataclass(frozen=True)
class AccountEvidence:
    account_id: str
    portfolio_id: str
    balances: tuple[BalanceObservation, ...]
    open_orders: tuple[OrderEvidence, ...]
    sources: tuple[SourceEvidence, ...]
    complete: bool
    reason_codes: tuple[str, ...]
    reconciliation_ready: bool = False


@dataclass(frozen=True)
class LiveSubmission(VenueExecution):
    order_evidence: OrderEvidence


class LiveVenue:
    venue = 'coinbaseadvanced'

    def __init__(self, transport: ManagedBoundedTransport, *, portfolio_id: str,
            market_source: Callable[[str], MarketRules], clock_us: Callable[[], int],
            monotonic_ns: Callable[[], int], max_pages: int = 50, page_size: int = 100):
        if (type(transport) is not ManagedBoundedTransport or type(max_pages) is not int
                or not 1 <= max_pages <= 50 or type(page_size) is not int or not 1 <= page_size <= 250):
            raise ValueError('invalid live acquisition dependencies/limits')
        self.transport, self.client = transport, transport.client
        self.account_id, self.portfolio_id = transport.account_id, _id(portfolio_id)
        self.market_source, self.clock_us, self.monotonic_ns = market_source, clock_us, monotonic_ns
        self.max_pages, self.page_size = max_pages, page_size

    def market_rules(self, instrument):
        rules = self.market_source(_instrument(instrument))
        if (type(rules) is not MarketRules or rules.venue != self.venue or rules.instrument != instrument
                or rules.expires_at_us <= self.clock_us() or not rules.spot_allowed
                or not rules.limit_ioc_verified):
            raise LiveTradingRejected('verified current market rules unavailable')
        return rules

    def _budget(self, deadline):
        now = self.monotonic_ns()
        if type(deadline) is not int or type(now) is not int or now < 0 or deadline <= now:
            raise AcquisitionUnavailable('READ_DEADLINE')
        return min(deadline, now + 10_000_000_000)

    def _source(self, endpoint, raw, requested):
        try:
            encoded = json.dumps(raw, sort_keys=True, separators=(',', ':'), allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise AcquisitionUnavailable('INVALID_SOURCE_PAYLOAD') from exc
        if len(encoded.encode()) > 1_048_576:
            raise AcquisitionUnavailable('SOURCE_PAYLOAD_SIZE')
        received = self.clock_us()
        if (type(requested) is not int or type(received) is not int or not 0 <= requested <= received):
            raise AcquisitionUnavailable('INVALID_RECEIPT_TIME')
        return SourceEvidence(self, self.account_id, self.portfolio_id, self.venue,
            endpoint, requested, received, content_hash(raw), encoded)

    def _call(self, method, params, deadline, *, retry=True):
        function = getattr(self.client, method, None)
        if not callable(function):
            raise AcquisitionUnavailable('UNSUPPORTED_ENDPOINT')
        for attempt in range(3 if retry else 1):
            remaining = deadline - self.monotonic_ns()
            if remaining < 1_000_000:
                # Permit sub-ms fake deadlines but configure at least a 1ms SDK
                # timeout; the post-call check still enforces the exact cutoff.
                if remaining <= 0:
                    raise AcquisitionUnavailable('READ_DEADLINE')
            if not self.transport.client_lock.acquire(timeout=max(0, remaining / 1e9)):
                raise AcquisitionUnavailable('READ_DEADLINE')
            old_timeout = getattr(self.client, 'timeout', 10_000)
            old_retries = getattr(self.client, 'maxRetriesOnFailure', 0)
            try:
                remaining = deadline - self.monotonic_ns()
                if remaining <= 0:
                    raise AcquisitionUnavailable('READ_DEADLINE')
                self.client.timeout = max(1, min(10_000, remaining // 1_000_000))
                self.client.maxRetriesOnFailure = 0
                requested = self.clock_us()
                raw = function(dict(params))
                source = self._source(method, raw, requested)
                if self.monotonic_ns() >= deadline:
                    raise AcquisitionUnavailable('READ_DEADLINE')
                if type(raw) is not dict or raw.get('proof_token_required') is True:
                    raise AcquisitionUnavailable('INCOMPLETE_SOURCE_RESPONSE')
                return raw, source
            except Exception as exc:
                network = isinstance(exc, (TimeoutError, OSError)) or any(
                    kind.__name__ in {'RequestTimeout', 'NetworkError', 'ExchangeNotAvailable',
                        'RateLimitExceeded', 'DDoSProtection'} for kind in type(exc).__mro__)
                if not network or attempt == (2 if retry else 0):
                    if isinstance(exc, AcquisitionUnavailable):
                        raise
                    raise AcquisitionUnavailable('READ_FAILED') from exc
            finally:
                self.client.timeout, self.client.maxRetriesOnFailure = old_timeout, old_retries
                self.transport.client_lock.release()
        raise AcquisitionUnavailable('READ_FAILED')

    def _portfolio(self, raw):
        if raw.get('retail_portfolio_id') != self.portfolio_id:
            raise AcquisitionUnavailable('SOURCE_ACCOUNT_MISMATCH')

    def _reference(self, reference, instrument):
        if type(reference) is not OrderReference or reference.account_id != self.account_id:
            raise AcquisitionUnavailable('SOURCE_ACCOUNT_MISMATCH')
        _id(reference.client_order_id)
        return _instrument(instrument)

    def _order(self, raw, reference, instrument, sources):
        self._portfolio(raw)
        if (raw.get('client_order_id') != reference.client_order_id or raw.get('product_id') != instrument
                or reference.venue_order_id is not None and raw.get('order_id') != reference.venue_order_id):
            raise AcquisitionUnavailable('ORDER_IDENTITY_MISMATCH')
        if raw.get('product_type') != 'SPOT' or raw.get('side') not in {'BUY', 'SELL'}:
            raise AcquisitionUnavailable('UNSUPPORTED_ORDER_SEMANTICS')
        configuration = raw.get('order_configuration', {}).get('sor_limit_ioc')
        if type(configuration) is not dict:
            raise AcquisitionUnavailable('UNSUPPORTED_ORDER_SEMANTICS')
        quantity = _decimal(configuration.get('base_size'), positive=True)
        price = _decimal(configuration.get('limit_price'), positive=True)
        filled = _decimal(raw.get('filled_size'))
        if filled > quantity:
            raise AcquisitionUnavailable('CUMULATIVE_QUANTITY_CONFLICT')
        states = {'OPEN': IntentState.OPEN, 'PENDING': IntentState.OPEN,
            'FILLED': IntentState.FILLED, 'CANCELLED': IntentState.CANCELED,
            'EXPIRED': IntentState.CANCELED, 'REJECTED': IntentState.REJECTED}
        state = states.get(raw.get('status'), IntentState.UNKNOWN)
        if state is IntentState.FILLED and filled != quantity:
            raise AcquisitionUnavailable('CUMULATIVE_QUANTITY_CONFLICT')
        if type(raw.get('pending_cancel')) is not bool or type(raw.get('settled')) is not bool:
            raise AcquisitionUnavailable('INCOMPLETE_ORDER_FINALITY')
        terminal = state in {IntentState.FILLED, IntentState.CANCELED, IntentState.REJECTED} and not raw['pending_cancel']
        fee = _optional_decimal(raw.get('total_fees'))
        with localcontext() as context:
            context.prec = 100
            remaining = quantity - filled
        return OrderEvidence(replace(reference, venue_order_id=_id(raw.get('order_id'))), instrument,
            state=state, filled_quantity=filled, remaining_quantity=remaining, total_fee=fee,
            fee_asset='USD' if fee is not None else None, terminal=terminal, settled=raw['settled'],
            complete=state is not IntentState.UNKNOWN, sources=tuple(sources),
            event_at_us=_timestamp(raw.get('created_time')), side=raw['side'].lower(),
            base_quantity=quantity, limit_price=price)

    def submit(self, intent, book, market):
        # Existing authority protocol compatibility. The causal book/risk checks
        # are owned by OrderAuthority; this boundary cannot substitute a book.
        requested = self.clock_us()
        raw = self.transport.submit_prepared(intent, market)
        source = self._source('create_order', raw, requested)
        reference = OrderReference(self.account_id, intent.intent_id, intent.client_order_id)
        raw_info = raw.get('info') if type(raw) is dict else None
        acknowledgment = raw_info.get('success_response') if type(raw_info) is dict else None
        candidate_id = raw.get('id') if type(raw) is dict else None
        client_id = raw.get('clientOrderId') if type(raw) is dict else None
        symbol = raw.get('symbol') if type(raw) is dict else None
        side = raw.get('side') if type(raw) is dict else None
        if type(acknowledgment) is dict:
            candidate_id, client_id = acknowledgment.get('order_id'), acknowledgment.get('client_order_id')
            symbol = acknowledgment.get('product_id', '').replace('-', '/')
            side = acknowledgment.get('side', '').lower()
        valid = (client_id == intent.client_order_id and symbol == intent.proposal.instrument.replace('-', '/')
            and side == intent.proposal.side.value and type(candidate_id) is str and bool(candidate_id))
        if valid:
            reference = replace(reference, venue_order_id=_id(candidate_id))
        evidence = OrderEvidence(reference, intent.proposal.instrument, sources=(source,),
            reason_codes=('ACK_PENDING_RECONCILIATION' if valid else 'ACK_IDENTITY_MISMATCH',))
        result = VenueOrderResult(reference, IntentState.UNKNOWN, Decimal(0), intent.proposal.base_quantity,
            source.received_at_us, 'live-ack-' + source.payload_hash, evidence.reason_codes[0])
        return LiveSubmission((result,), (), evidence)

    def _pages(self, endpoint, field, params, deadline, *, fill_mode=False):
        sources, items, seen = [], [], set()
        cursor = None
        for _ in range(self.max_pages):
            request = dict(params, limit=self.page_size)
            if cursor:
                request['cursor'] = cursor
            try:
                raw, source = self._call(endpoint, request, deadline)
                sources.append(source)
                page = raw.get(field)
                if type(page) is not list or len(page) > self.page_size:
                    raise AcquisitionUnavailable('INVALID_SOURCE_PAGE')
                if any(type(item) is not dict for item in page):
                    raise AcquisitionUnavailable('INVALID_SOURCE_PAGE')
                items.extend((item, source) for item in page)
                next_cursor = raw.get('cursor')
                if next_cursor is not None and (type(next_cursor) is not str or len(next_cursor) > 1024):
                    raise AcquisitionUnavailable('INVALID_SOURCE_CURSOR')
                if fill_mode:
                    done = next_cursor in (None, '')
                else:
                    if type(raw.get('has_next')) is not bool:
                        raise AcquisitionUnavailable('INCOMPLETE_SOURCE_RESPONSE')
                    done = not raw['has_next']
                if done:
                    return items, tuple(sources), True, (), None
                if not next_cursor or next_cursor in seen:
                    raise AcquisitionUnavailable('CURSOR_STALLED')
                seen.add(next_cursor)
                cursor = next_cursor
            except AcquisitionUnavailable as exc:
                return items, tuple(sources), False, (str(exc),), cursor
        return items, tuple(sources), False, ('PAGE_LIMIT',), cursor

    def lookup(self, reference, instrument, *, deadline_mono_ns):
        instrument = self._reference(reference, instrument)
        unknown = OrderEvidence(reference, instrument)
        sources = ()
        try:
            deadline = self._budget(deadline_mono_ns)
            if reference.venue_order_id is not None:
                raw, source = self._call(_ORDER, {'order_id': reference.venue_order_id}, deadline)
                sources = (source,)
                return self._order(raw.get('order', {}), reference, instrument, sources)
            items, sources, complete, reasons, _ = self._pages(_ORDERS, 'orders',
                {'product_ids': [instrument], 'retail_portfolio_id': self.portfolio_id}, deadline)
            matches = [raw for raw, _ in items if raw.get('client_order_id') == reference.client_order_id]
            ids = {raw.get('order_id') for raw in matches}
            if len(ids) > 1 or len({content_hash(raw) for raw in matches}) > 1:
                raise AcquisitionUnavailable('CLIENT_ID_COLLISION')
            if not matches:
                return replace(unknown, sources=sources,
                    reason_codes=reasons + ('NOT_FOUND_NOT_ABSENCE_PROOF',))
            evidence = self._order(matches[0], reference, instrument, sources)
            return replace(evidence, complete=evidence.complete and complete,
                reason_codes=evidence.reason_codes + reasons)
        except (AcquisitionUnavailable, ValueError, TypeError, AttributeError) as exc:
            return replace(unknown, sources=sources, reason_codes=(str(exc),))

    def cancel(self, reference, instrument, *, deadline_mono_ns):
        instrument = self._reference(reference, instrument)
        unknown = OrderEvidence(reference, instrument)
        if reference.venue_order_id is None:
            return replace(unknown, reason_codes=('ORDER_ID_REQUIRED',))
        try:
            deadline = self._budget(deadline_mono_ns)
            raw, source = self._call(_CANCEL, {'order_ids': [reference.venue_order_id]}, deadline, retry=False)
            rows = raw.get('results')
            valid = (type(rows) is list and len(rows) == 1 and type(rows[0]) is dict
                and rows[0].get('order_id') == reference.venue_order_id and rows[0].get('success') is True)
            return replace(unknown, sources=(source,), reason_codes=(
                'CANCEL_ACK_RECONCILIATION_REQUIRED' if valid else 'CANCEL_UNCONFIRMED',))
        except AcquisitionUnavailable as exc:
            return replace(unknown, reason_codes=(str(exc),))

    def _fill(self, raw, source):
        self._portfolio(raw)
        instrument = _instrument(raw.get('product_id'))
        if raw.get('trade_type') != 'FILL' or raw.get('side') not in {'BUY', 'SELL'}:
            raise AcquisitionUnavailable('UNSUPPORTED_FILL_SEMANTICS')
        if type(raw.get('size_in_quote')) is not bool:
            raise AcquisitionUnavailable('UNSUPPORTED_FILL_SEMANTICS')
        size, price = _decimal(raw.get('size'), positive=True), _decimal(raw.get('price'), positive=True)
        if raw['size_in_quote']:
            quantity, quote = _exact_product(size, price, divide=True), size
        else:
            quantity, quote = size, _exact_product(size, price)
        fee = _optional_decimal(raw.get('commission'))
        return FillObservation(self.account_id, self.venue, self.portfolio_id,
            _id(raw.get('trade_id')), _id(raw.get('order_id')), instrument, raw['side'].lower(),
            quantity, quote, price, 'USD' if fee is not None else None, fee, None,
            _timestamp(raw.get('trade_time')), content_hash(raw), source)

    def read_fills(self, *, since_us, until_us, deadline_mono_ns, overlap_us=1_000_000,
            known_hashes: Mapping[tuple[str, str, str], str] | None = None):
        if (type(since_us) is not int or type(until_us) is not int or not 0 <= since_us < until_us
                or type(overlap_us) is not int or not 0 <= overlap_us <= 60_000_000):
            raise AcquisitionUnavailable('INVALID_WINDOW')
        start = max(0, since_us - overlap_us)
        fills, conflicts, sources, reasons, cursor = [], [], (), (), None
        seen = dict(known_hashes or {})
        try:
            deadline = self._budget(deadline_mono_ns)
            items, sources, complete, reasons, cursor = self._pages(_FILLS, 'fills', {
                'start_sequence_timestamp': _iso(start), 'end_sequence_timestamp': _iso(until_us),
                'retail_portfolio_id': self.portfolio_id}, deadline, fill_mode=True)
            for raw, source in items:
                try:
                    fill = self._fill(raw, source)
                    if not start <= fill.event_at_us <= until_us:
                        raise AcquisitionUnavailable('FILL_OUTSIDE_WINDOW')
                    previous = seen.get(fill.dedupe_key)
                    if previous is None:
                        fills.append(fill)
                        seen[fill.dedupe_key] = fill.payload_hash
                    elif previous != fill.payload_hash:
                        conflicts.append(FillConflict(previous, fill))
                        complete = False
                        reasons += ('TRADE_PAYLOAD_CONFLICT',)
                except (ValueError, TypeError, ArithmeticError) as exc:
                    complete = False
                    reasons += (str(exc),)
            return FillEvidenceBatch(tuple(fills), tuple(conflicts), sources, start, until_us,
                complete, tuple(dict.fromkeys(reasons)), cursor)
        except AcquisitionUnavailable as exc:
            return FillEvidenceBatch(tuple(fills), tuple(conflicts), sources, start, until_us,
                False, reasons + (str(exc),), cursor)

    def read_account(self, *, deadline_mono_ns):
        balances, orders, sources, reasons, complete = [], [], (), (), False
        try:
            deadline = self._budget(deadline_mono_ns)
            items, sources, complete, reasons, _ = self._pages(_ACCOUNTS, 'accounts',
                {'retail_portfolio_id': self.portfolio_id}, deadline)
            wallets = set()
            for raw, source in items:
                try:
                    self._portfolio(raw)
                    wallet, asset = _id(raw.get('uuid')), _id(raw.get('currency'))
                    if wallet in wallets:
                        raise AcquisitionUnavailable('DUPLICATE_WALLET')
                    available, hold = raw.get('available_balance', {}), raw.get('hold', {})
                    if available.get('currency') != asset or hold.get('currency') != asset:
                        raise AcquisitionUnavailable('BALANCE_CURRENCY_MISMATCH')
                    balances.append(BalanceObservation(wallet, asset, _decimal(available.get('value')),
                        _decimal(hold.get('value')), source))
                    wallets.add(wallet)
                except (ValueError, TypeError, AttributeError) as exc:
                    complete = False
                    reasons += (str(exc),)
            order_items, order_sources, order_complete, order_reasons, _ = self._pages(_ORDERS, 'orders',
                {'order_status': ['OPEN', 'PENDING'], 'retail_portfolio_id': self.portfolio_id}, deadline)
            sources += order_sources
            complete = complete and order_complete
            reasons += order_reasons
            for raw, source in order_items:
                try:
                    ref = OrderReference(self.account_id, 'external-order', _id(raw.get('client_order_id')),
                        _id(raw.get('order_id')))
                    evidence = self._order(raw, ref, _instrument(raw.get('product_id')), (source,))
                    orders.append(evidence)
                    complete = complete and evidence.complete
                except (ValueError, TypeError, AttributeError) as exc:
                    complete = False
                    reasons += (str(exc),)
        except AcquisitionUnavailable as exc:
            complete = False
            reasons += (str(exc),)
        return AccountEvidence(self.account_id, self.portfolio_id, tuple(balances), tuple(orders),
            sources, complete, tuple(dict.fromkeys(reasons)))
