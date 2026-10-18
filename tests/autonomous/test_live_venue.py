"""Task 13 acquisition with deterministic fake authenticated CCXT endpoints."""
import dataclasses
import importlib.util
import unittest
from decimal import Decimal as D
from tests.test_live_adapter import ManagedTransportTests


class FakeCoinbase:
    timeout = 30_000
    maxRetriesOnFailure = 0
    def __init__(self):
        self.calls = []
        self.orders = []
        self.fills = []
        self.account_pages = []
        self.order_pages = []
        self.failure = None
    def create_order(self, *args):
        self.calls.append(('create', args, self.timeout))
        if self.failure:
            raise self.failure
        return {'id': 'order-1', 'clientOrderId': 'stable-action-1', 'symbol': 'BTC/USD', 'side': 'sell'}
    def v3PrivateGetBrokerageOrdersHistoricalOrderId(self, params):
        self.calls.append(('order', params, self.timeout))
        item = self.orders.pop(0)
        if isinstance(item, Exception):
            raise item
        return {'order': item}
    def v3PrivateGetBrokerageOrdersHistoricalBatch(self, params):
        self.calls.append(('orders', params, self.timeout))
        return self.order_pages.pop(0)
    def v3PrivateGetBrokerageOrdersHistoricalFills(self, params):
        self.calls.append(('fills', params, self.timeout))
        return self.fills.pop(0)
    def v3PrivateGetBrokerageAccounts(self, params):
        self.calls.append(('accounts', params, self.timeout))
        return self.account_pages.pop(0)
    def v3PrivatePostBrokerageOrdersBatchCancel(self, params):
        self.calls.append(('cancel', params, self.timeout))
        return {'results': [{'success': True, 'order_id': 'order-1'}]}


class LiveVenueTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('bot.autonomous.live_venue'), 'live acquisition missing')
        from bot.autonomous import live_venue
        from bot.autonomous.contracts import OrderReference
        self.m = live_venue
        # Reuse only the explicit inert fixture construction, not its test suite.
        self.helper = ManagedTransportTests()
        self.helper.setUp()
        self.client = FakeCoinbase()
        self.now = 10
        self.mono = 10
        self.transport = self.helper.transport(client=self.client)
        self.intent, self.market = self.helper.intent, self.helper.market
        self.venue = live_venue.LiveVenue(self.transport, portfolio_id='portfolio-1',
            market_source=lambda _: self.market, clock_us=lambda: self.now,
            monotonic_ns=lambda: self.mono, max_pages=5, page_size=2)
        self.reference = OrderReference('managed-1', 'intent-1', 'stable-action-1', 'order-1')

    def order(self, **changes):
        raw = dict(order_id='order-1', client_order_id='stable-action-1', product_id='BTC-USD',
            retail_portfolio_id='portfolio-1', side='SELL', status='CANCELLED',
            order_configuration={'sor_limit_ioc': {'base_size': '.01001', 'limit_price': '100.01'}},
            filled_size='0', total_fees='0', settled=True, pending_cancel=False,
            created_time='2026-01-01T00:00:00Z', product_type='SPOT')
        raw.update(changes)
        return raw

    def fill(self, trade='trade-1', **changes):
        raw = dict(trade_id=trade, entry_id='entry-' + trade, order_id='order-1',
            product_id='BTC-USD', retail_portfolio_id='portfolio-1', side='SELL',
            trade_time='2026-01-01T00:00:00.123456789Z', sequence_timestamp='2026-01-01T00:00:00Z',
            trade_type='FILL', size='0.001', size_in_quote=False, price='100.01', commission='0.01')
        raw.update(changes)
        return raw

    def test_submit_ack_is_unknown_single_attempt_and_no_unverified_fills(self):
        from bot.autonomous.contracts import IntentState
        result = self.venue.submit(self.intent, None, self.market)
        self.assertEqual(IntentState.UNKNOWN, result.results[0].state)
        self.assertEqual((), result.fills)
        self.assertEqual('order-1', result.order_evidence.reference.venue_order_id)
        self.assertIs(self.venue, result.order_evidence.sources[0].issuer)
        self.assertEqual('BTC/USD', self.client.calls[0][1][0])
        # A second dispatch of the same client order ID never reaches the venue.
        from bot.live_coinbase_adapter import LiveTradingRejected
        with self.assertRaises(LiveTradingRejected):
            self.venue.submit(self.intent, None, self.market)
        self.assertEqual(1, len(self.client.calls))

    def fresh_venue(self):
        return self.m.LiveVenue(self.transport, portfolio_id='portfolio-1',
            market_source=lambda _: self.market, clock_us=lambda: self.now,
            monotonic_ns=lambda: self.mono, max_pages=5, page_size=2)

    def test_submit_timeout_after_acceptance_propagates_once_and_blocks_resubmit(self):
        from bot.live_coinbase_adapter import LiveTradingRejected
        self.client.failure = TimeoutError('accepted then timed out')
        with self.assertRaises(TimeoutError):
            self.venue.submit(self.intent, None, self.market)
        self.assertEqual(1, len(self.client.calls))
        # The outcome is UNKNOWN: a retry of the same intent is refused locally.
        self.client.failure = None
        with self.assertRaises(LiveTradingRejected):
            self.venue.submit(self.intent, None, self.market)
        self.assertEqual(1, len(self.client.calls))

    def test_submit_disables_sdk_retries_and_bounds_timeout_by_intent_deadline(self):
        seen = []
        original = FakeCoinbase.create_order
        def create(*args):
            seen.append((self.client.timeout, self.client.maxRetriesOnFailure))
            return original(self.client, *args)
        self.client.create_order = create
        self.client.maxRetriesOnFailure = 3
        self.venue.submit(self.intent, None, self.market)
        timeout, retries = seen[0]
        self.assertEqual(0, retries)
        remaining_ms = (self.intent.proposal.deadline_mono_ns - self.mono) // 1_000_000
        self.assertTrue(1 <= timeout <= max(1, min(10_000, remaining_ms)), timeout)
        self.assertEqual((30_000, 3), (self.client.timeout, self.client.maxRetriesOnFailure))

    def test_submit_restores_sdk_settings_after_failure(self):
        self.client.maxRetriesOnFailure = 2
        self.client.failure = OSError('reset after send')
        with self.assertRaises(OSError):
            self.fresh_venue().submit(self.intent, None, self.market)
        self.assertEqual((30_000, 2), (self.client.timeout, self.client.maxRetriesOnFailure))
        self.assertEqual(1, len(self.client.calls))

    def test_submit_mismatched_client_id_is_not_acknowledged(self):
        self.client.create_order = lambda *a: {'id': 'foreign', 'clientOrderId': 'foreign'}
        result = self.venue.submit(self.intent, None, self.market)
        self.assertIsNone(result.order_evidence.reference.venue_order_id)
        self.assertIn('ACK_IDENTITY_MISMATCH', result.order_evidence.reason_codes)

    def test_order_lookup_exact_identity_terminal_and_missing_fees(self):
        self.client.orders = [self.order(total_fees=None)]
        result = self.venue.lookup(self.reference, 'BTC-USD', deadline_mono_ns=100)
        self.assertTrue(result.terminal)
        self.assertIsNone(result.total_fee)
        self.assertIsNone(result.fees_final)
        self.assertEqual(D('0'), result.filled_quantity)
        self.assertIs(self.venue, result.sources[0].issuer)
        self.client.orders = [self.order(client_order_id='foreign')]
        result = self.venue.lookup(self.reference, 'BTC-USD', deadline_mono_ns=100)
        self.assertFalse(result.complete)
        self.assertIn('ORDER_IDENTITY_MISMATCH', result.reason_codes)

    def test_client_id_lookup_pages_and_not_found_never_proves_absence(self):
        ref = dataclasses.replace(self.reference, venue_order_id=None)
        self.client.order_pages = [{'orders': [self.order(client_order_id='other')], 'has_next': True, 'cursor': 'c1'},
            {'orders': [self.order()], 'has_next': False, 'cursor': ''}]
        found = self.venue.lookup(ref, 'BTC-USD', deadline_mono_ns=100)
        self.assertEqual('order-1', found.reference.venue_order_id)
        self.client.order_pages = [{'orders': [], 'has_next': False, 'cursor': ''}]
        absent = self.venue.lookup(ref, 'BTC-USD', deadline_mono_ns=100)
        self.assertFalse(absent.absence_proven)
        self.assertIn('NOT_FOUND_NOT_ABSENCE_PROOF', absent.reason_codes)

    def test_cancel_ack_does_not_prove_terminal_then_fill_can_win_race(self):
        cancel = self.venue.cancel(self.reference, 'BTC-USD', deadline_mono_ns=100)
        self.assertFalse(cancel.terminal)
        self.assertIsNone(cancel.filled_quantity)
        self.client.orders = [self.order(status='FILLED', filled_size='.01001', total_fees='.001')]
        later = self.venue.lookup(self.reference, 'BTC-USD', deadline_mono_ns=100)
        self.assertTrue(later.terminal)
        self.assertEqual(D('.01001'), later.filled_quantity)

    def test_reads_retry_at_most_three_and_restore_timeout(self):
        self.client.orders = [TimeoutError(), TimeoutError(), self.order()]
        result = self.venue.lookup(self.reference, 'BTC-USD', deadline_mono_ns=100_000_000)
        self.assertTrue(result.complete)
        self.assertEqual(3, len(self.client.calls))
        self.assertTrue(all(0 < call[2] <= 99 for call in self.client.calls))
        self.assertEqual(30_000, self.client.timeout)
        self.client.calls.clear()
        self.client.orders = [TimeoutError()] * 4
        result = self.venue.lookup(self.reference, 'BTC-USD', deadline_mono_ns=100_000_000)
        self.assertFalse(result.complete)
        self.assertEqual(3, len(self.client.calls))

    def test_deadline_prevents_reads_and_late_response_remains_incomplete(self):
        result = self.venue.lookup(self.reference, 'BTC-USD', deadline_mono_ns=10)
        self.assertFalse(result.complete)
        self.assertEqual([], self.client.calls)
        original = self.client.v3PrivateGetBrokerageOrdersHistoricalOrderId
        self.client.orders = [self.order()]
        def late(params):
            result = original(params)
            self.mono = 101
            return result
        self.client.v3PrivateGetBrokerageOrdersHistoricalOrderId = late
        result = self.venue.lookup(self.reference, 'BTC-USD', deadline_mono_ns=100)
        self.assertFalse(result.complete)

    def test_fill_pagination_overlap_dedup_conflict_and_missing_fee(self):
        a, b = self.fill(), self.fill('trade-2', commission=None)
        self.client.fills = [{'fills': [a], 'cursor': 'next'},
            {'fills': [a, b], 'cursor': ''}]
        result = self.venue.read_fills(since_us=0, until_us=2_000_000_000_000_000,
            deadline_mono_ns=100)
        self.assertTrue(result.complete)
        self.assertEqual(2, len(result.fills))
        self.assertIsNone(result.fills[1].fee_quantity)
        self.assertIsNone(result.fills[0].fees_final)
        self.assertEqual('next', self.client.calls[1][1]['cursor'])
        known = {result.fills[0].dedupe_key: result.fills[0].payload_hash}
        self.client.fills = [{'fills': [self.fill(commission='.02')], 'cursor': ''}]
        changed = self.venue.read_fills(since_us=0, until_us=2_000_000_000_000_000,
            deadline_mono_ns=100, known_hashes=known)
        self.assertEqual(1, len(changed.conflicts))
        self.assertFalse(changed.complete)
        self.assertEqual(known[result.fills[0].dedupe_key], changed.conflicts[0].previous_hash)

    def test_fill_bad_portfolio_float_and_repeated_cursor_are_incomplete(self):
        for fill in (self.fill(retail_portfolio_id='other'), self.fill(price=100.01)):
            self.client.fills = [{'fills': [fill], 'cursor': ''}]
            result = self.venue.read_fills(since_us=0, until_us=2_000_000_000_000_000, deadline_mono_ns=100)
            self.assertFalse(result.complete)
            self.assertEqual((), result.fills)
        self.client.fills = [{'fills': [self.fill()], 'cursor': 'same'}] * 2
        result = self.venue.read_fills(since_us=0, until_us=2_000_000_000_000_000, deadline_mono_ns=100)
        self.assertFalse(result.complete)
        self.assertIn('CURSOR_STALLED', result.reason_codes)

    def test_account_reads_all_pages_open_orders_and_rejects_foreign_portfolio(self):
        account = dict(uuid='wallet-usd', currency='USD', retail_portfolio_id='portfolio-1',
            available_balance={'value': '10', 'currency': 'USD'}, hold={'value': '2', 'currency': 'USD'})
        self.client.account_pages = [{'accounts': [account], 'has_next': False, 'cursor': ''}]
        self.client.order_pages = [{'orders': [self.order(status='OPEN')], 'has_next': False, 'cursor': ''}]
        result = self.venue.read_account(deadline_mono_ns=100)
        self.assertTrue(result.complete)
        self.assertEqual(D('10'), result.balances[0].available)
        self.assertEqual(D('2'), result.balances[0].hold)
        self.assertEqual(1, len(result.open_orders))
        self.assertFalse(result.reconciliation_ready)
        self.client.account_pages = [{'accounts': [dict(account, retail_portfolio_id='foreign')],
            'has_next': False, 'cursor': ''}]
        self.client.order_pages = [{'orders': [], 'has_next': False, 'cursor': ''}]
        self.assertFalse(self.venue.read_account(deadline_mono_ns=100).complete)

    def test_unsupported_endpoint_and_unverified_submit_capabilities_fail_closed(self):
        self.client.v3PrivateGetBrokerageOrdersHistoricalOrderId = None
        self.assertFalse(self.venue.lookup(self.reference, 'BTC-USD', deadline_mono_ns=100).complete)
        from bot.live_coinbase_adapter import LiveTradingRejected
        self.transport.capability_verifier = None
        with self.assertRaises(LiveTradingRejected):
            self.venue.submit(self.intent, None, self.market)
        self.assertEqual([], self.client.calls)
