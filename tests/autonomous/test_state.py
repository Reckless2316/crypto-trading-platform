"""Offline deterministic numeric state checks; no provider accounting claims."""
import math
import json
import unittest
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from unittest.mock import Mock

from bot.autonomous import state
from bot.autonomous.contracts import (
    AccountRevision, CausalCutoffs, Feature, NumericSnapshot, SharedContext,
    TokenAccountingUnavailable, TokenCount, EncodedState, canonical_bytes,
    default_field_dictionary,
    AccountView, Balance, BookPayload, CandlePayload, SourceEvent, content_hash,
    StateUnavailable,
    ContractViolation, HistoricalInventory,
    ApprovedBundle, CandidateBundle, CandidateDefinition, GateApproval,
)


class BookMetricsTests(unittest.TestCase):
    def metrics(self, bids, asks, band_bps=10.):
        self.assertTrue(hasattr(state, 'book_metrics'), 'book metrics absent')
        return state.book_metrics(bids, asks, band_bps)

    def test_mid_spread_and_symmetric_band_depth(self):
        mid, spread, imbalance = self.metrics(
            ((99.99, 2.), (99.9, 4.), (99., 1000.)),
            ((100.01, 1.), (100.1, 1.), (101., 1000.)))
        self.assertEqual(mid, 100.)
        self.assertAlmostEqual(spread, 2.)
        self.assertAlmostEqual(imbalance, .5)

    def test_invalid_books_fail_closed(self):
        for bids, asks, band in (
            ((), ((101., 1.),), 10.),
            (((100., 1.),), ((100., 1.),), 10.),
            (((99., 1.),), ((101., 1.),), 10.),
            (((99.99, math.inf),), ((100.01, 1.),), 10.),
            (((99.99, 0.),), ((100.01, 1.),), 10.),
            (((99.99, 1.),), ((100.01, 1.),), math.nan),
        ):
            with self.subTest(bids=bids, asks=asks, band=band):
                self.assertTrue(hasattr(state, 'book_metrics'), 'book metrics absent')
                with self.assertRaises(ValueError):
                    state.book_metrics(bids, asks, band)

    def test_entire_state_token_boundary_is_strict_and_exact_integer(self):
        self.assertTrue(hasattr(state, 'require_token_budget'), 'budget check absent')
        for value in (0, 399):
            state.require_token_budget(value)
        for value in (-1, 400, 401, 399., True, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                state.require_token_budget(value)


class StateEncodingHelpers:
    def setUp(self):
        self.dictionary = default_field_dictionary()
        self.cutoffs = CausalCutoffs(60_000_000, 60_000_000, 1, 'test-boot')

    def snapshot(self, candidate_id, eligible=True, cutoffs=None, account_id='paper', release_hash=None):
        features = []
        for field in self.dictionary.candidate_fields:
            value = 1. if field.name == 'eligible' and eligible else 0.
            features.append(Feature(field.name, value, field.name == 'eligible'))
        mask = 1 << next(i for i, f in enumerate(self.dictionary.candidate_fields) if f.name == 'eligible')
        return NumericSnapshot(candidate_id, candidate_id.upper() + '-USD', 'a' * 64,
            cutoffs or self.cutoffs, tuple(features), mask, 'b' * 64, AccountRevision(),
            account_id=account_id, release_hash=release_hash)

    def ranked_bundle(self, order, eligible_ids=None):
        eligible_ids = set(order if eligible_ids is None else eligible_ids)
        candidates = tuple(CandidateDefinition(candidate_id, candidate_id.upper()+'-USD',
            candidate_id in eligible_ids, False, (), 'e'*64 if candidate_id in eligible_ids else None)
            for candidate_id in order)
        bundle = CandidateBundle('model', 'a'*64, self.dictionary.dictionary_hash, 'b'*64,
            'c'*64, 'd'*64, 'e'*64, 'f'*64, '1'*64, 10**15, candidates, ())
        approval = GateApproval('evaluation', 'test', bundle.bundle_hash, '2'*64, 1)
        return ApprovedBundle(bundle, (approval,), 2, '3'*64)

    def encode(self, order, eligible_ids=None, counter=None, snapshots=None, context=None):
        bundle = self.ranked_bundle(order, eligible_ids)
        snapshots = snapshots or tuple(self.snapshot(cid, cid in set(order if eligible_ids is None else eligible_ids),
            release_hash=bundle.bundle_hash) for cid in order)
        return state.encode_state(snapshots, context or self.context(), self.dictionary,
            counter or self.Counter(), candidate_order=tuple(order), ranked_bundle=bundle)

    def context(self):
        features = tuple(Feature(field.name, 0., False) for field in self.dictionary.shared_fields)
        return SharedContext(features, 0)

    class Counter:
        def __init__(self, count=399, fail=False, proof_override=None):
            self.count = count
            self.fail = fail
            self.proof_override=proof_override
            self.inputs = []
        def count_state(self, state_json):
            self.inputs.append(state_json)
            if self.fail:
                raise TokenAccountingUnavailable('counter unavailable')
            if self.proof_override is not None:
                return self.proof_override
            count = (500 if len(json.loads(state_json)[1]) > 1 else 399) if self.count == 500 else self.count
            return TokenCount(count, 'provider-tokenizer-v1', 'provider',
                'pinned_provider_tokenizer', 'c' * 64)

class StateEncodingTests(StateEncodingHelpers, unittest.TestCase):
    def test_encoder_is_canonical_complete_and_uses_provider_proof(self):
        counter = self.Counter()
        encoded = self.encode(('aave',), counter=counter)
        self.assertEqual(encoded.candidate_ids, ('aave',))
        self.assertEqual(counter.inputs, [encoded.canonical_state_json])
        self.assertEqual(len(encoded.snapshots), 1)
        wire=json.loads(encoded.canonical_state_json)
        self.assertEqual(len(wire[0]),17)
        self.assertEqual(len(wire[1][0]),16)
        self.assertEqual(wire[0][1], self.cutoffs.candle_close_us // 1000)

    def test_encoder_prunes_whole_lowest_ranked_candidate_then_recounts(self):
        counter = self.Counter(count=500)
        encoded = self.encode(('hype', 'aave'), counter=counter)
        self.assertEqual(encoded.candidate_ids, ('hype',))
        self.assertEqual(encoded.excluded_candidate_ids, ('aave',))
        self.assertEqual(len(counter.inputs), 2)

    def test_encoder_rejects_mixed_frozen_batch_identity(self):
        counter = self.Counter()
        bundle = self.ranked_bundle(('aave','hype'))
        first = self.snapshot('aave', release_hash=bundle.bundle_hash)
        other_cutoffs = CausalCutoffs(120_000_000, 120_000_000, 1, 'other-boot')
        second = self.snapshot('hype', cutoffs=other_cutoffs, release_hash=bundle.bundle_hash)
        with self.assertRaises(ContractViolation):
            state.encode_state((first, second), self.context(), self.dictionary, counter,
                candidate_order=('aave','hype'), ranked_bundle=bundle)
        self.assertEqual(counter.inputs, [])

    def test_encoder_requires_complete_approved_rank_order(self):
        counter=self.Counter()
        bundle=self.ranked_bundle(('aave','hype'))
        snapshots=tuple(self.snapshot(cid,release_hash=bundle.bundle_hash) for cid in ('aave','hype'))
        with self.assertRaises(ContractViolation):
            state.encode_state(snapshots,self.context(),self.dictionary,counter,
                candidate_order=('hype','aave'),ranked_bundle=bundle)
        self.assertEqual(counter.inputs,[])

    def test_encoded_candidate_state_binds_exact_rank_policy(self):
        encoded=self.encode(('aave',))
        self.assertEqual(encoded.ranked_bundle.bundle_hash,encoded.rank_bundle_hash)
        self.assertEqual(encoded.ranked_bundle.bundle.policy_hash,encoded.rank_policy_hash)
        with self.assertRaises(ContractViolation):
            replace(encoded,rank_policy_hash='9'*64)

    def test_encoder_fails_closed_without_tokenizer_proof(self):
        counter = self.Counter(fail=True)
        with self.assertRaises(TokenAccountingUnavailable):
            bundle=self.ranked_bundle(('aave',))
            snap=self.snapshot('aave', release_hash=bundle.bundle_hash)
            state.encode_state((snap,), self.context(), self.dictionary, counter,
                candidate_order=('aave',), ranked_bundle=bundle)

    def test_encoder_rejects_untyped_tokenizer_proof(self):
        bundle=self.ranked_bundle(('aave',))
        snapshot=self.snapshot('aave',release_hash=bundle.bundle_hash)
        counter=self.Counter(proof_override=object())
        with self.assertRaises(TokenAccountingUnavailable):
            state.encode_state((snapshot,),self.context(),self.dictionary,counter,
                candidate_order=('aave',),ranked_bundle=bundle)

    def test_no_eligible_candidates_does_not_call_provider_counter(self):
        counter = self.Counter()
        outcome = self.encode(('aave',), eligible_ids=(), counter=counter)
        self.assertIsInstance(outcome, state.StateHoldDraft)
        self.assertEqual(outcome.reason, 'no_eligible_candidates')
        self.assertEqual(outcome.excluded_candidate_ids, ('aave',))
        self.assertEqual(outcome.ranked_bundle.bundle_hash,outcome.rank_bundle_hash)
        self.assertEqual(outcome.ranked_bundle.bundle.policy_hash,outcome.rank_policy_hash)
        with self.assertRaises(ContractViolation):
            replace(outcome,rank_policy_hash='9'*64)
        self.assertEqual(counter.inputs, [])

    def test_mandatory_state_and_one_candidate_must_fit(self):
        counter = self.Counter(count=400)
        outcome = self.encode(('aave',), counter=counter)
        self.assertIsInstance(outcome, state.StateHoldDraft)
        self.assertEqual(outcome.reason, 'mandatory_state_over_budget')
        self.assertEqual(len(counter.inputs), 1)


class SnapshotMathTests(unittest.TestCase):
    def make_event(self, event_id, instrument, event_at, payload, sequence=None):
        return SourceEvent(event_id, 'test', 'market', instrument, event_id, 0,
            event_at, event_at, event_at, payload, content_hash(payload), sequence)

    def engine(self, events):
        engine = object.__new__(state.StateEngine)
        engine.ledger = Mock()
        engine.ledger._source_engine = engine
        engine._snapshot_inputs = {}
        engine._snapshot_evidence = {}
        engine._snapshot_inventory = {}
        engine.account_id = 'paper'
        engine.select = lambda cutoffs, **window: tuple(events)
        engine.ledger.inventory_as_of.return_value = HistoricalInventory(
            CausalCutoffs(61*60_000_000, 61*60_000_000, 1, 'boot'), 'paper', AccountRevision(3, 4),
            (('AAVE', Decimal('2')),), 'c'*64)
        engine.ledger.account_view.side_effect = AssertionError('snapshot must not read current inventory')
        engine.ledger.connection.execute.return_value.fetchone.return_value = ('d'*64,)
        return engine

    def test_realized_volatility_uses_61_causal_book_mids_not_candle_close(self):
        books = [self.make_event(f'b{i}', 'AAVE-USD', i * 60_000_000,
            BookPayload(((Decimal(i)+Decimal('99.99'), Decimal('2')),), ((Decimal(i)+Decimal('100.01'), Decimal('1')),)), i)
            for i in range(1, 62)]
        books.append(self.make_event('future', 'AAVE-USD', 62 * 60_000_000,
            BookPayload(((Decimal(199), Decimal('2')),), ((Decimal(201), Decimal('1')),)), 62))
        # A candle with the same close is deliberately irrelevant to proven mid features.
        candle = self.make_event('candle', 'AAVE-USD', 61 * 60_000_000,
            CandlePayload(Decimal(100), Decimal(100), Decimal(100), Decimal(100), Decimal(1)))
        engine = self.engine(books + [candle])
        snapshot = engine.snapshot('AAVE-USD', CausalCutoffs(61*60_000_000, 61*60_000_000, 1, 'boot'))
        values = {feature.name: feature for feature in snapshot.features}
        self.assertTrue(values['rv_60m'].valid)
        self.assertEqual(values['rv_samples'].value, 60)
        self.assertAlmostEqual(values['mid'].value, 161.)
        self.assertAlmostEqual(values['base_inventory'].value, 2.)
        engine.ledger.inventory_as_of.assert_called_once_with('paper', snapshot.cutoffs)
        self.assertEqual(snapshot.account_revision, AccountRevision(3, 4))

    def test_historical_inventory_account_mismatch_fails_closed(self):
        books = [self.make_event(f'b{i}', 'AAVE-USD', i * 60_000_000,
            BookPayload(((Decimal('99.99'), Decimal('2')),), ((Decimal('100.01'), Decimal('1')),)), i)
            for i in range(1, 62)]
        engine=self.engine(books)
        engine.ledger.inventory_as_of.return_value=replace(
            engine.ledger.inventory_as_of.return_value, account_id='other')
        with self.assertRaises(StateUnavailable):
            engine.snapshot('AAVE-USD', CausalCutoffs(61*60_000_000,61*60_000_000,1,'boot'))

    def test_negative_historical_quantity_is_preserved_but_never_eligible(self):
        books = [self.make_event(f'b{i}', 'AAVE-USD', i * 60_000_000,
            BookPayload(((Decimal('99.99'), Decimal('2')),), ((Decimal('100.01'), Decimal('1')),)), i)
            for i in range(1, 62)]
        engine=self.engine(books)
        engine.ledger.inventory_as_of.return_value=replace(
            engine.ledger.inventory_as_of.return_value, quantities=(('AAVE',Decimal('-2')),))
        snapshot=engine.snapshot('AAVE-USD',CausalCutoffs(61*60_000_000,61*60_000_000,1,'boot'))
        values={feature.name:feature for feature in snapshot.features}
        self.assertTrue(values['base_inventory'].valid)
        self.assertEqual(values['base_inventory'].value,-2.)
        self.assertFalse(values['eligible'].valid)

    def test_gap_invalidates_realized_volatility_instead_of_filling_warmup(self):
        books = [self.make_event(f'b{i}', 'AAVE-USD', i * 60_000_000,
            BookPayload(((Decimal('99.99'), Decimal('2')),), ((Decimal('100.01'), Decimal('1')),)), i)
            for i in range(1, 62) if i != 30]
        engine = self.engine(books)
        snapshot = engine.snapshot('AAVE-USD', CausalCutoffs(61*60_000_000, 61*60_000_000, 1, 'boot'))
        values = {feature.name: feature for feature in snapshot.features}
        self.assertFalse(values['rv_60m'].valid)
        self.assertFalse(values['rv_samples'].valid)
        self.assertEqual(values['rv_samples'].value,0.)
        self.assertTrue(values['mid'].valid)
        self.assertTrue(values['spread_bps'].valid)
        self.assertTrue(values['imbalance'].valid)


class BoundarySamplerTests(unittest.TestCase):
    CUT = CausalCutoffs(61*60_000_000, 61*60_000_000, 1, 'boot')
    make_event = SnapshotMathTests.make_event

    def book(self, mid):
        return BookPayload(((Decimal(mid)-Decimal('0.01'), Decimal('2')),), ((Decimal(mid)+Decimal('0.01'), Decimal('1')),))

    def continuous_feed(self, before_us=100_000, after_us=100_000):
        events, seq = [], 0
        for i in range(1, 62):
            seq += 1
            events.append(self.make_event(f'pre{i}', 'AAVE-USD', i*60_000_000-before_us, self.book(100+i), seq))
            seq += 1
            events.append(self.make_event(f'post{i}', 'AAVE-USD', i*60_000_000+after_us, self.book(500+i), seq))
        return events

    def test_continuous_feed_samples_latest_book_at_or_before_each_boundary(self):
        events = self.continuous_feed()
        snapshot = self.engine(events).snapshot('AAVE-USD', self.CUT)
        values = {feature.name: feature for feature in snapshot.features}
        self.assertTrue(values['rv_60m'].valid)
        self.assertEqual(values['rv_samples'].value, 60)
        # The close boundary uses the 59.9s book, never the post-boundary 500+ book.
        self.assertAlmostEqual(values['mid'].value, 161.)
        self.assertAlmostEqual(values['return_1h'].value, 161/101-1)
        # Exchange timestamps are preserved as evidence, not rewritten onto the boundary.
        self.assertIn('pre61', engine_inputs := self._inputs(snapshot))
        self.assertTrue(all(e.event_at_us % 60_000_000 for e in events))
        self.assertNotIn('post61', engine_inputs)

    def _inputs(self, snapshot):
        return self._engine._snapshot_inputs[snapshot.snapshot_hash]

    def engine(self, events):
        self._engine = SnapshotMathTests.engine(self, events)
        return self._engine

    def test_exchange_time_regression_breaks_stream_until_next_snapshot(self):
        events = self.continuous_feed()
        index = next(i for i, e in enumerate(events) if e.event_id == 'pre30')
        events[index] = replace(events[index], event_at_us=events[index].event_at_us - 120_000_000,
            received_at_us=events[index].event_at_us - 120_000_000, known_at_us=events[index].event_at_us - 120_000_000)
        values = {f.name: f for f in self.engine(events).snapshot('AAVE-USD', self.CUT).features}
        self.assertFalse(values['rv_60m'].valid)
        self.assertTrue(values['mid'].valid)

    def test_replay_window_requires_recent_full_book_checkpoint(self):
        window = state.StateEngine.BOOK_REPLAY_WINDOW_US
        cut = CausalCutoffs(71*60_000_000, 71*60_000_000, 1, 'boot')
        first = cut.candle_close_us - 60*60_000_000
        # Only full book is older than the replay window; later deltas cannot rebuild it.
        events = [self.make_event('old', 'AAVE-USD', first - window - 1_000_000, self.book(100), 1)]
        for i in range(11, 72):
            events.append(self.make_event(f'd{i}', 'AAVE-USD', i*60_000_000-100_000,
                BookPayload((), (), False), i-9))
        engine = self.engine(events)
        engine.ledger.inventory_as_of.return_value = replace(engine.ledger.inventory_as_of.return_value, cutoffs=cut)
        snapshot = engine.snapshot('AAVE-USD', cut)
        values = {f.name: f for f in snapshot.features}
        self.assertFalse(values['mid'].valid)
        self.assertFalse(values['rv_60m'].valid)
        self.assertNotIn('old', self._inputs(snapshot))

    def test_two_book_streams_for_one_instrument_are_ambiguous_everywhere(self):
        # Venue A fresh at odd minutes, venue B at even minutes: never mix them into one series.
        events = []
        for i in range(1, 62):
            source = 'a' if i % 2 else 'b'
            mid = 100 if source == 'a' else 110
            events.append(SourceEvent(f'{source}{i}', source, 'market', 'AAVE-USD', f'{source}{i}', 0,
                i*60_000_000-100_000, i*60_000_000-100_000, i*60_000_000-100_000,
                self.book(mid), content_hash(self.book(mid)), i))
        values = {f.name: f for f in self.engine(events).snapshot('AAVE-USD', self.CUT).features}
        self.assertFalse(values['rv_60m'].valid)
        self.assertFalse(values['mid'].valid)

    def test_sample_exactly_at_age_bound_is_used(self):
        age = state.StateEngine.BOUNDARY_MAX_AGE_US
        events = [self.make_event(f'b{i}', 'AAVE-USD', i*60_000_000-age, self.book(100+i), i) for i in range(1, 62)]
        values = {f.name: f for f in self.engine(events).snapshot('AAVE-USD', self.CUT).features}
        self.assertTrue(values['rv_60m'].valid)

    def test_evicted_snapshot_evidence_refuses_encoding(self):
        engine = self.engine(self.continuous_feed())
        engine._snapshot_evidence = state._BoundedCache(1)
        snapshot = engine.snapshot('AAVE-USD', self.CUT)
        engine._snapshot_evidence['other'] = object()
        with self.assertRaises(StateUnavailable):
            engine.encode((snapshot,), None, None, None, candidate_order=(), ranked_bundle=None)

    def test_sample_older_than_bound_is_missing(self):
        stale = state.StateEngine.BOUNDARY_MAX_AGE_US + 1
        events = [self.make_event(f'b{i}', 'AAVE-USD', i*60_000_000-stale, self.book(100+i), i) for i in range(1, 62)]
        values = {f.name: f for f in self.engine(events).snapshot('AAVE-USD', self.CUT).features}
        self.assertFalse(values['rv_60m'].valid)
        self.assertFalse(values['mid'].valid)

    def test_sequence_break_before_boundary_never_falls_back_to_older_book(self):
        events = self.continuous_feed()
        # Replace minute 30's pre-boundary snapshot with a delta that skips a sequence number.
        index = next(i for i, e in enumerate(events) if e.event_id == 'pre30')
        broken = events[index]
        events[index] = self.make_event('pre30', 'AAVE-USD', broken.event_at_us,
            BookPayload((), (), False), broken.sequence + 1000)
        for later in range(index+1, len(events)):
            e = events[later]
            events[later] = replace(e, sequence=e.sequence + 1000)
        values = {f.name: f for f in self.engine(events).snapshot('AAVE-USD', self.CUT).features}
        self.assertFalse(values['rv_60m'].valid)
        self.assertTrue(values['mid'].valid)


class SnapshotPersistenceTests(StateEncodingHelpers, unittest.TestCase):
    def persistence_fixture(self, nested=False):
        ranked_bundle = self.ranked_bundle(('aave',))
        snap = self.snapshot('aave', release_hash=ranked_bundle.bundle_hash)
        engine = object.__new__(state.StateEngine)
        engine.ledger = Mock()
        engine.ledger._source_engine = engine
        engine._snapshot_inputs = {snap.snapshot_hash: ('source-event',)}
        engine._snapshot_evidence = {snap.snapshot_hash: snap}
        engine._snapshot_inventory = {snap.snapshot_hash: HistoricalInventory(
            snap.cutoffs, snap.account_id, snap.account_revision, (), 'c'*64)}
        engine._encoded_evidence={}
        engine._hold_evidence={}
        engine._persisted_encoded_results={}
        engine._persisted_holds={}
        engine._persisted_encoded_results={}
        engine._persisted_holds={}
        encoded=engine.encode((snap,),self.context(),self.dictionary,self.Counter(),
            candidate_order=('aave',),ranked_bundle=ranked_bundle)
        engine.ledger.connection.in_transaction = nested
        engine.ledger._clock_us.return_value = 999
        engine.ledger._account_view.return_value = Mock(revision=snap.account_revision)
        tx = Mock()
        stored_snapshots={};stored_inputs={}
        def execute(sql, args=()):
            cursor = Mock()
            if 'SELECT active_release_hash' in sql:
                cursor.fetchone.return_value = (ranked_bundle.bundle_hash,)
            elif 'SELECT * FROM snapshots' in sql:
                cursor.fetchone.return_value = stored_snapshots.get(args[0])
            elif 'SELECT event_id FROM snapshot_inputs' in sql:
                cursor.__iter__=Mock(return_value=iter(stored_inputs.get(args[0],())))
            elif sql.startswith('INSERT INTO snapshots'):
                stored_snapshots[args[0]]=args
                cursor.fetchone.return_value=None
            elif sql.startswith('INSERT INTO snapshot_inputs'):
                stored_inputs.setdefault(args[0],[]).append((args[2],))
                cursor.fetchone.return_value=None
            else:
                cursor.fetchone.return_value = None
                cursor.__iter__ = Mock(return_value=iter(()))
            return cursor
        tx.execute.side_effect = execute
        engine._state_test_tx=tx
        @contextmanager
        def transaction():
            yield tx
            engine.ledger.connection.in_transaction = False
        engine.ledger.transaction.side_effect = transaction
        return engine, snap, encoded

    def test_persist_rejects_wire_row_that_does_not_match_registered_snapshot(self):
        engine, _, encoded = self.persistence_fixture()
        wire = json.loads(encoded.canonical_state_json)
        wire[1][0][9] = 0  # Schema-valid, but not the registered eligible value.
        changed = replace(encoded, canonical_state_json=canonical_bytes(wire).decode('ascii'),
            state_hash=content_hash(wire))
        with self.assertRaises(StateUnavailable):
            engine.persist_encoded_state(changed)

    def test_persist_rejects_snapshot_object_that_differs_from_registered_evidence(self):
        engine,snap,encoded=self.persistence_fixture()
        altered_features=tuple(Feature(f.name,0.0,f.valid) if f.name=='eligible' else f for f in snap.features)
        altered=replace(snap,features=altered_features)
        changed=replace(encoded,snapshots=(altered,))
        with self.assertRaises(StateUnavailable):
            engine.persist_encoded_state(changed)

    def test_persist_rejects_shared_context_tampering_after_token_proof(self):
        engine,snap,_=self.persistence_fixture()
        bundle=self.ranked_bundle(('aave',))
        shared=list(self.context().features);shared[2]=Feature(shared[2].name,0.1,True)
        context=SharedContext(tuple(shared),1<<2)
        encoded=engine.encode((snap,),context,self.dictionary,self.Counter(),
            candidate_order=('aave',),ranked_bundle=bundle)
        wire=json.loads(encoded.canonical_state_json)
        wire[0][2]=0.2
        changed=replace(encoded,canonical_state_json=canonical_bytes(wire).decode('ascii'),
            state_hash=content_hash(wire))
        with self.assertRaises(StateUnavailable):
            engine.persist_encoded_state(changed)

    def test_persist_rejects_nested_transaction_before_opening_savepoint(self):
        engine, _, encoded = self.persistence_fixture(nested=True)
        with self.assertRaises(ContractViolation):
            engine.persist_encoded_state(encoded)
        engine.ledger.transaction.assert_not_called()

    def test_persist_rejects_legacy_encoded_state_without_rank_proof(self):
        engine,_,encoded=self.persistence_fixture()
        legacy=EncodedState(encoded.canonical_state_json,encoded.state_hash,
            encoded.field_dictionary_hash,encoded.candidate_ids,encoded.token_count,encoded.snapshots)
        with self.assertRaises(StateUnavailable):
            engine.persist_encoded_state(legacy)
        engine.ledger.transaction.assert_not_called()

    def test_persist_retry_returns_same_ids_for_original_or_returned_encoding(self):
        engine,_,encoded=self.persistence_fixture()
        first=engine.persist_encoded_state(encoded)
        retry_original=engine.persist_encoded_state(encoded)
        retry_result=engine.persist_encoded_state(first)
        self.assertEqual(retry_original,first)
        self.assertEqual(retry_result,first)
        snapshot_inserts=[call for call in engine._state_test_tx.execute.call_args_list
            if call.args and call.args[0].startswith('INSERT INTO snapshots')]
        self.assertEqual(len(snapshot_inserts),1)

    def test_hold_is_returned_only_after_exclusion_audit_commits_without_counter(self):
        counter=self.Counter()
        bundle=self.ranked_bundle(('aave',),eligible_ids=())
        snap=self.snapshot('aave',eligible=False,release_hash=bundle.bundle_hash)
        engine=object.__new__(state.StateEngine)
        engine.ledger=Mock();engine.ledger._source_engine=engine
        engine._snapshot_inputs={snap.snapshot_hash:('source-event',)}
        engine._snapshot_evidence={snap.snapshot_hash:snap}
        engine._snapshot_inventory={snap.snapshot_hash:HistoricalInventory(
            snap.cutoffs,snap.account_id,snap.account_revision,(),'c'*64)}
        engine._encoded_evidence={};engine._hold_evidence={}
        engine._persisted_encoded_results={};engine._persisted_holds={}
        draft=engine.encode((snap,),self.context(),self.dictionary,counter,
            candidate_order=('aave',),ranked_bundle=bundle)
        self.assertIsInstance(draft,state.StateHoldDraft)
        self.assertEqual(counter.inputs,[])
        engine.ledger.connection.in_transaction=False;engine.ledger._clock_us.return_value=999
        tx=Mock()
        def execute(sql,args=()):
            cursor=Mock()
            cursor.fetchone.return_value=(bundle.bundle_hash,) if 'SELECT active_release_hash' in sql else None
            cursor.__iter__=Mock(return_value=iter(()))
            return cursor
        tx.execute.side_effect=execute
        @contextmanager
        def transaction():
            yield tx
            engine.ledger.connection.in_transaction=False
        engine.ledger.transaction.side_effect=transaction
        outcome=engine.persist_encoded_state(draft)
        self.assertIsInstance(outcome,state.StateHold)
        self.assertEqual(outcome.draft.audit_hash,draft.audit_hash)
        inserts=[call for call in tx.execute.call_args_list if 'INSERT INTO snapshots' in call.args[0]]
        self.assertEqual(len(inserts),1)
        self.assertEqual(inserts[0].args[1][11],0)
