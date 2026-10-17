"""Offline causal source and durable archive acceptance tests."""
import dataclasses
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from bot.autonomous.contracts import (BookPayload, CandlePayload, CausalCutoffs,
    ContractViolation, MetricPayload, SourceEvent, content_hash)
from bot.autonomous.ledger import Ledger


def state_module():
    try:
        from bot.autonomous import state
        return state
    except ImportError:
        raise AssertionError('causal state implementation absent') from None


def event(name='s', seq=1, *, payload=None, **kw):
    payload = payload or BookPayload(((D('99'), D('2')),), ((D('101'), D('3')),))
    args=dict(event_id=name,source='venue',stream='book',instrument='BTC-USD',
        logical_key=name,revision=0,event_at_us=90,received_at_us=100,
        known_at_us=100,payload=payload,payload_hash=content_hash(payload),sequence=seq)
    args.update(kw)
    return SourceEvent(**args)


class CausalityTests(unittest.TestCase):
    def setUp(self):
        self.s=state_module()
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.db=Ledger.open(self.root/'db')
        self.addCleanup(lambda:self.db.close())
        self.db._clock_us=lambda:200
        self.policy=self.s.SourceClockPolicy('release-test','venue','book',1000,10)
        self.engine=self.s.StateEngine(self.db,self.root/'archive',(self.policy,))

    def test_late_receipt_is_excluded(self):
        visible=self.s.causal_visible
        self.assertFalse(visible(90,111,111,100,110))
        self.assertFalse(visible(101,109,109,100,110))
        self.assertTrue(visible(90,109,109,100,110))
        self.assertFalse(visible(90,109,108,100,110))
        for bad in (True,-1,1.1,'1',None):
            for i in range(5):
                args=[90,100,100,100,110];args[i]=bad
                self.assertFalse(visible(*args))

    def test_immutable_revisions_invalidations_and_duplicates(self):
        a=event(seq=None,payload=MetricPayload('rate',D('1'),'ratio'))
        self.assertTrue(self.engine.ingest(a).accepted)
        self.assertTrue(self.engine.ingest(a).accepted)
        self.assertFalse(self.engine.ingest(dataclasses.replace(a,received_at_us=99)).accepted)
        b=dataclasses.replace(a,event_id='r',revision=1,known_at_us=120,supersedes_id='s')
        self.assertTrue(self.engine.ingest(b).accepted)
        self.assertEqual((a,),self.engine.select(CausalCutoffs(100,110,0,'boot')))
        self.assertEqual((b,),self.engine.select(CausalCutoffs(100,130,0,'boot')))
        self.db._clock_us=lambda:140
        self.engine.invalidate('inv','r',140,'reorg')
        self.assertEqual((b,),self.engine.select(CausalCutoffs(100,130,0,'boot')))
        self.assertEqual((a,),self.engine.select(CausalCutoffs(100,150,0,'boot')))
        self.assertEqual(2,self.db.connection.execute('SELECT count(*) FROM source_events').fetchone()[0])

    def test_source_policy_required_and_drift_future_rejected(self):
        other=event(source='unknown')
        self.assertFalse(self.engine.ingest(other).accepted)
        self.assertFalse(self.engine.ingest(event(event_at_us=111)).accepted)
        self.assertFalse(self.engine.ingest(event(known_at_us=201)).accepted)
        self.assertEqual((self.policy,),self.engine.policies)

    def test_sequences_and_recovery(self):
        self.assertTrue(self.engine.ingest(event()).accepted)
        delta=event('d',2,payload=BookPayload(((D('99'),D('0')),(D('98'),D('4'))),(),False))
        self.assertTrue(self.engine.ingest(delta).accepted)
        self.assertEqual(D('98'),self.engine.book('venue','book','BTC-USD').bids[0][0])
        self.assertTrue(self.engine.ingest(delta).accepted)
        expected=self.engine.book('venue','book','BTC-USD')
        reopened=self.s.StateEngine(self.db,self.root/'archive',(self.policy,))
        self.assertEqual(expected,reopened.book('venue','book','BTC-USD'))
        self.engine=reopened
        self.assertEqual(2,self.db.connection.execute('SELECT count(*) FROM event_segments').fetchone()[0])
        for bad in (event('gap',4,payload=delta.payload),event('old',1,payload=delta.payload)):
            self.assertFalse(self.engine.ingest(bad).accepted)
            self.assertFalse(self.engine.entries_ready)
        self.db._clock_us=lambda:300
        self.assertTrue(self.engine.ingest(event('reset',10,received_at_us=201,known_at_us=201)).accepted)
        self.assertTrue(self.engine.entries_ready)

    def test_conflicting_sequence_and_invalid_result(self):
        self.engine.ingest(event())
        self.assertFalse(self.engine.ingest(event('collision',1)).accepted)
        self.assertFalse(self.engine.ingest(event('no-snapshot',2,payload=BookPayload((),(),False))).accepted)
        self.db._clock_us=lambda:300
        self.engine.ingest(event('reset',10,received_at_us=201,known_at_us=201))
        empty=BookPayload(((D('99'),D('0')),),(),False)
        self.assertFalse(self.engine.ingest(event('empty',11,payload=empty)).accepted)
        self.db._clock_us=lambda:400
        self.engine.ingest(event('reset2',20,received_at_us=301,known_at_us=301))
        crossed=BookPayload(((D('102'),D('1')),),(),False)
        self.assertFalse(self.engine.ingest(event('cross',21,payload=crossed)).accepted)

    def test_archive_failures_never_publish_partial_manifest(self):
        for operation in ('write','flush','fsync_file','rename','fsync_directory'):
            with self.subTest(operation=operation):
                with patch.object(self.engine.archive,operation,side_effect=OSError(operation)):
                    self.assertFalse(self.engine.ingest(event(operation)).accepted)
                self.assertFalse(self.engine.entries_ready)
                self.assertEqual(0,self.db.connection.execute('SELECT count(*) FROM event_segments').fetchone()[0])
                self.assertEqual(0,self.db.connection.execute('SELECT count(*) FROM source_events').fetchone()[0])

    def test_commit_failure_creates_verified_unadopted_orphan(self):
        with patch.object(self.db,'_record_revisions',side_effect=OSError('commit')):
            self.assertFalse(self.engine.ingest(event()).accepted)
        self.assertEqual(0,self.db.connection.execute('SELECT count(*) FROM event_segments').fetchone()[0])
        recovered=self.s.StateEngine(self.db,self.root/'archive',(self.policy,))
        self.assertEqual(1,len(recovered.orphans))
        self.assertIsNone(recovered.book('venue','book','BTC-USD'))
        path=self.root/'archive'/recovered.orphans[0]
        path.write_bytes(b'corrupt')
        broken=self.s.StateEngine(self.db,self.root/'archive',(self.policy,))
        self.assertFalse(broken.entries_ready)

    def test_corruption_missing_and_unsafe_manifest_fail_closed(self):
        self.engine.ingest(event())
        row=self.db.connection.execute('SELECT * FROM event_segments').fetchone()
        path=self.root/'archive'/row['storage_key']
        original=path.read_bytes()
        path.write_bytes(b'corrupt')
        self.assertFalse(self.s.StateEngine(self.db,self.root/'archive',(self.policy,)).entries_ready)
        path.write_bytes(original);path.unlink()
        self.assertFalse(self.s.StateEngine(self.db,self.root/'archive',(self.policy,)).entries_ready)
        for unsafe in ('../outside','/tmp/outside','x/../../oops'):
            with self.assertRaises((ContractViolation,OSError)):
                self.engine.archive.read(unsafe,row['segment_hash'])
        outside=self.root/'outside';outside.write_bytes(original)
        path.symlink_to(outside)
        with self.assertRaises((ContractViolation,OSError)):
            self.engine.archive.read(row['storage_key'],row['segment_hash'])

    def test_bounds_and_format_checkpoint_verification(self):
        self.engine.ingest(event())
        row=dict(self.db.connection.execute('SELECT * FROM event_segments').fetchone())
        for field,value in [('checkpoint_hash','0'*64),('last_sequence',99),('byte_length',1),('event_to_us',91)]:
            changed=dict(row);changed[field]=value
            with self.assertRaises(ContractViolation):
                self.engine.archive.verify(changed)
        small=self.s.SegmentArchive(self.root/'archive',max_compressed_bytes=1,max_decoded_bytes=100)
        with self.assertRaises(ContractViolation):small.read(row['storage_key'],row['segment_hash'])
        small=self.s.SegmentArchive(self.root/'archive',max_decoded_bytes=1)
        with self.assertRaises(ContractViolation):small.read(row['storage_key'],row['segment_hash'])

    def test_legacy_candle_import_is_known_at_import(self):
        candle=CandlePayload(D('1'),D('1'),D('1'),D('1'),D('0'))
        imported=self.engine.import_candle(event(seq=None,payload=candle,event_at_us=1))
        self.assertTrue(imported.accepted)
        self.assertEqual((),self.engine.select(CausalCutoffs(100,199,0,'boot')))
        selected=self.engine.select(CausalCutoffs(100,200,0,'boot'))
        self.assertEqual(200,selected[0].received_at_us)
        self.assertEqual(200,selected[0].known_at_us)

    def test_nested_ingest_rejected_before_archive_io(self):
        with self.db.transaction():
            with self.assertRaises(ContractViolation):self.engine.ingest(event())
        self.assertEqual([],list((self.root/'archive').iterdir()))

    def test_source_health_blocks_reserve_and_cannot_be_overwritten(self):
        from bot.autonomous.contracts import AccountRevision, StateUnavailable
        from tests.autonomous.support import order_proposal
        with self.assertRaises(StateUnavailable):
            self.db.reserve_intent(order_proposal(),AccountRevision(),0,None)
        with self.assertRaises(AttributeError):self.db.source_entries_ready=True
        self.engine.ingest(event())
        with patch.object(self.engine.archive,'write',side_effect=OSError('disk')):
            self.engine.ingest(event('next',2))
        self.assertFalse(self.db.source_entries_ready)
        with self.assertRaises(StateUnavailable):
            self.db.reserve_intent(order_proposal(),AccountRevision(),0,None)

    def test_invalidation_preserves_pending_rows_and_derives_eligibility(self):
        self.engine.ingest(event())
        c=self.db.connection
        self.db.create_paper_account('paper-test')
        h=content_hash({})
        with self.db.transaction():
            c.execute("INSERT INTO release_bundles VALUES (?,'{}',NULL,300,200)",(h,))
            c.execute("INSERT INTO snapshots VALUES ('snap','paper-test',?,100,110,?,'{}',?,1,?,'{}',1,NULL,200)",(h,h,h,h))
            c.execute("INSERT INTO snapshot_inputs VALUES ('snap',0,'s',NULL,NULL,NULL,200)")
            c.execute("INSERT INTO decisions VALUES ('decision','snap','request',300,'model',NULL,NULL,'pending',NULL,NULL,200)")
        before=tuple(c.execute('SELECT * FROM decisions').fetchone())
        self.engine.invalidate('invalidation','s',150,'source reorg')
        self.assertEqual(before,tuple(c.execute('SELECT * FROM decisions').fetchone()))
        self.assertEqual(1,c.execute('SELECT valid FROM snapshots').fetchone()[0])
        self.assertEqual(('snap',),self.engine.invalidated_snapshots())
        self.assertTrue(self.db._source_lineage_invalid('snap'))
        self.assertFalse(self.engine.entries_ready)

    def test_equal_knowledge_revision_order_and_recorded_invalidation_cutoff(self):
        a=event('a',None,payload=MetricPayload('rate',D('1'),'ratio'),logical_key='same')
        b=dataclasses.replace(a,event_id='b',revision=1)
        self.engine.ingest(b);self.engine.ingest(a)
        cutoff=CausalCutoffs(100,110,0,'boot')
        self.assertEqual((b,),self.engine.select(cutoff))
        self.engine.invalidate('late-record','b',105,'late report')
        self.assertEqual((b,),self.engine.select(cutoff))
        self.assertEqual((a,),self.engine.select(CausalCutoffs(100,200,0,'boot')))

    def test_source_and_segment_committed_prefix_exports(self):
        from tests.autonomous.test_ledger import LedgerTests
        from bot.autonomous.contracts import SessionCutoff
        helper=LedgerTests();helper.ledger=self.db
        self.db.create_paper_account('paper-test')
        now=helper.seed()
        self.db._clock_us=lambda:now+10
        self.engine.ingest(event())
        self.db._clock_us=lambda:now+30
        self.engine.ingest(event('after',2))
        archive,payload=helper.export_payload(SessionCutoff('paper-test',now+20,'reconcile'))
        self.assertEqual(['s'],[e['event_id'] for e in payload['source_events']])
        self.assertEqual(1,len(payload['event_segments']))
        self.assertEqual(2,len([m for m in archive.event_manifest if m.record_id.startswith(('source_events:', 'event_segments:'))]))

    def test_sequence_gap_remains_unhealthy_after_recovery(self):
        self.engine.ingest(event())
        delta=BookPayload((),(),False)
        self.assertFalse(self.engine.ingest(event('gap',3,payload=delta)).accepted)
        recovered=self.s.StateEngine(self.db,self.root/'archive',(self.policy,))
        self.assertFalse(recovered.entries_ready)
        self.db._clock_us=lambda:210
        self.assertTrue(recovered.ingest(event('reset',10,received_at_us=201,known_at_us=201)).accepted)
        again=self.s.StateEngine(self.db,self.root/'archive',(self.policy,))
        self.assertTrue(again.entries_ready)

    def test_reopen_without_archive_recovery_is_unready(self):
        self.engine.ingest(event())
        self.db.close();self.db=Ledger.open(self.root/'db')
        self.assertFalse(self.db.source_entries_ready)
        recovered=self.s.StateEngine(self.db,self.root/'archive',(self.policy,))
        self.assertTrue(recovered.entries_ready)

    def test_selection_rejects_uncommitted_transaction(self):
        with self.db.transaction():
            with self.assertRaises(ContractViolation):self.engine.select(CausalCutoffs(100,110,0,'boot'))

    def test_archive_constructor_failure_cannot_leave_entries_enabled(self):
        other=Ledger.open(self.root/'other-db')
        self.addCleanup(other.close)
        bad=self.root/'bad-root';bad.symlink_to(self.root/'archive',target_is_directory=True)
        with self.assertRaises(ContractViolation):self.s.StateEngine(other,bad,(self.policy,))
        self.assertFalse(other.source_entries_ready)

    def test_fixture_replays_original_delta_bytes(self):
        fixture=Path(__file__).parent/'fixtures'/'causal_book.jsonl'
        self.assertTrue(fixture.exists())
        for line in fixture.read_text().splitlines():
            self.assertTrue(self.engine.ingest(SourceEvent.from_json(line)).accepted)
        self.assertEqual(D('98'),self.engine.book('venue','book','BTC-USD').bids[0][0])

    def test_bounded_reader_rejects_bombs_trailing_bytes_and_unknown_version(self):
        import zlib
        from bot.autonomous.contracts import canonical_bytes
        self.engine.ingest(event())
        row=dict(self.db.connection.execute('SELECT * FROM event_segments').fetchone())
        original=(self.root/'archive'/row['storage_key']).read_bytes()
        body=json.loads(zlib.decompress(original))
        body['format']='causal-book-v2'
        for raw in (zlib.compress(b'x'*1_048_577),original+b'extra',original[:-1],zlib.compress(canonical_bytes(body))):
            digest=hashlib.sha256(raw).hexdigest();name=digest+'.zlib'
            (self.root/'archive'/name).write_bytes(raw)
            with self.assertRaises(ContractViolation):self.engine.archive.read(name,digest)

    def test_commit_uncertainty_disables_dispatch_and_leaves_no_manifest(self):
        import sqlite3
        original_connect=sqlite3.connect
        class FailCommit(sqlite3.Connection):
            fail=False
            def execute(self,sql,*args,**kwargs):
                if sql=='COMMIT' and self.fail:
                    self.fail=False
                    raise sqlite3.OperationalError('injected commit failure')
                return super().execute(sql,*args,**kwargs)
        with patch('sqlite3.connect',side_effect=lambda *a,**kw:original_connect(*a,**kw,factory=FailCommit)):
            other=Ledger.open(self.root/'commit-db')
        self.addCleanup(other.close)
        other._clock_us=lambda:200
        engine=self.s.StateEngine(other,self.root/'commit-archive',(self.policy,))
        other.connection.fail=True
        self.assertFalse(engine.ingest(event()).accepted)
        self.assertFalse(other.dispatch_enabled)
        self.assertEqual(0,other.connection.execute('SELECT count(*) FROM event_segments').fetchone()[0])
        self.assertEqual(1,len(list((self.root/'commit-archive').glob('*.zlib'))))

    def test_dispatch_rechecks_current_source_health_and_invalidation(self):
        from tests.autonomous.test_ledger import LedgerTests
        from tests.autonomous.support import HASH_B
        from bot.autonomous.contracts import StateUnavailable
        self.db.create_paper_account('paper-test')
        helper=LedgerTests();helper.ledger=self.db
        proposal=helper.ready()
        self.db._clock_us=lambda:200
        self.engine.ingest(event())
        with self.db.transaction() as tx:
            tx.execute('INSERT INTO snapshot_inputs VALUES (?,0,\'s\',NULL,NULL,NULL,200)',(HASH_B,))
        # Fixture accounting/reconciliation time is independent of synthetic source time.
        import time
        self.db._clock_us=lambda:time.time_ns()//1000
        prepared=helper.reserve(proposal)
        self.engine.invalidate('reorg','s',self.db._clock_us(),'reorg')
        self.assertTrue(self.db._source_lineage_invalid(HASH_B))
        with self.assertRaises(StateUnavailable):self.db.mark_dispatching(prepared.intent_id,prepared.generation)
        self.assertEqual('reserved',self.db.connection.execute('SELECT state FROM order_intents').fetchone()[0])

    def test_replaced_engine_cannot_admit_from_stale_projection(self):
        self.engine.ingest(event())
        replacement=self.s.StateEngine(self.db,self.root/'archive',(self.policy,))
        delta=BookPayload(((D('99'),D('4')),),(),False)
        self.assertTrue(replacement.ingest(event('replacement-delta',2,payload=delta)).accepted)
        with self.assertRaises(ContractViolation):self.engine.ingest(event('stale-delta',2,payload=delta))
        with self.assertRaises(ContractViolation):self.engine.recover()
        self.assertEqual(2,self.db.connection.execute('SELECT count(*) FROM event_segments').fetchone()[0])

    def test_archive_failure_survives_explicit_recovery_until_fresh_snapshot(self):
        self.engine.ingest(event())
        with patch.object(self.engine.archive,'write',side_effect=OSError('disk')):
            self.assertFalse(self.engine.ingest(event('lost',2)).accepted)
        self.engine.recover()
        self.assertFalse(self.engine.entries_ready)
        replacement=self.s.StateEngine(self.db,self.root/'archive',(self.policy,))
        self.assertFalse(replacement.entries_ready)
        self.db._clock_us=lambda:300
        self.assertTrue(replacement.ingest(event('fresh',10,received_at_us=201,known_at_us=201)).accepted)
        self.assertTrue(replacement.entries_ready)

    def test_delta_checkpoint_lineage_includes_base_until_full_reset(self):
        self.engine.ingest(event())
        delta=BookPayload(((D('99'),D('4')),),(),False)
        self.engine.ingest(event('delta',2,payload=delta))
        c=self.db.connection
        self.db.create_paper_account('paper-test')
        h=content_hash({})
        segment=c.execute('SELECT segment_hash FROM event_segments WHERE first_sequence=2').fetchone()[0]
        with self.db.transaction():
            c.execute("INSERT INTO release_bundles VALUES (?,'{}',NULL,300,200)",(h,))
            for name in ('segment-snap','event-snap'):
                c.execute("INSERT INTO snapshots VALUES (?,'paper-test',?,100,110,?,'{}',?,1,?,'{}',1,NULL,200)",(name,h,h,h,h))
            c.execute("INSERT INTO snapshot_inputs VALUES ('segment-snap',0,NULL,?,2,2,200)",(segment,))
            c.execute("INSERT INTO snapshot_inputs VALUES ('event-snap',0,'delta',NULL,NULL,NULL,200)")
        self.engine.invalidate('bad-base','s',200,'base invalid')
        self.assertTrue(self.db._source_lineage_invalid('segment-snap'))
        self.assertTrue(self.db._source_lineage_invalid('event-snap'))
        self.db._clock_us=lambda:300
        self.engine.ingest(event('fresh',10,received_at_us=201,known_at_us=201))
        self.assertTrue(self.engine.entries_ready)
        self.assertTrue(self.db._source_lineage_invalid('segment-snap'))
        self.assertTrue(self.db._source_lineage_invalid('event-snap'))

    def test_new_revision_invalidates_pending_without_destroying_source_fallback(self):
        a=event('a',None,payload=MetricPayload('rate',D('1'),'ratio'),logical_key='same')
        b=dataclasses.replace(a,event_id='b',revision=1,known_at_us=120,supersedes_id='a')
        self.engine.ingest(a)
        c=self.db.connection
        self.db.create_paper_account('paper-test')
        h=content_hash({})
        with self.db.transaction():
            c.execute("INSERT INTO release_bundles VALUES (?,'{}',NULL,300,200)",(h,))
            c.execute("INSERT INTO snapshots VALUES ('pending','paper-test',?,100,110,?,'{}',?,1,?,'{}',1,NULL,200)",(h,h,h,h))
            c.execute("INSERT INTO snapshot_inputs VALUES ('pending',0,'a',NULL,NULL,NULL,200)")
        self.assertTrue(self.engine.ingest(b).accepted)
        self.assertTrue(self.db._source_lineage_invalid('pending'))
        self.assertEqual(1,c.execute("SELECT count(*) FROM source_invalidations WHERE reason='source_revision_superseded'").fetchone()[0])
        self.assertEqual((a,),self.engine.select(CausalCutoffs(100,110,0,'boot')))
        self.assertEqual((b,),self.engine.select(CausalCutoffs(100,200,0,'boot')))
        self.engine.invalidate('bad-new','b',200,'new correction invalid')
        self.assertEqual((a,),self.engine.select(CausalCutoffs(100,200,0,'boot')))
        with self.db.transaction():
            c.execute("INSERT INTO snapshots VALUES ('fresh-fallback','paper-test',?,100,200,?,'{}',?,1,?,'{}',1,NULL,200)",(h,h,h,h))
            c.execute("INSERT INTO snapshot_inputs VALUES ('fresh-fallback',0,'a',NULL,NULL,NULL,200)")
        self.assertFalse(self.db._source_lineage_invalid('fresh-fallback'))

    def test_late_older_rank_never_invalidates_newer_revision(self):
        a=event('a',None,payload=MetricPayload('rate',D('1'),'ratio'),logical_key='same')
        b=dataclasses.replace(a,event_id='b',revision=1,known_at_us=120)
        self.engine.ingest(b);self.engine.ingest(a)
        invalid_ids={r[0] for r in self.db.connection.execute("SELECT event_id FROM source_invalidations WHERE reason='source_revision_superseded'")}
        self.assertNotIn('b',invalid_ids)
        self.assertEqual((b,),self.engine.select(CausalCutoffs(100,200,0,'boot')))

    def test_public_invalidation_cannot_forge_informational_reason(self):
        self.engine.ingest(event())
        with self.assertRaises(ContractViolation):
            self.engine.invalidate('forged','s',150,'source_revision_superseded')
        self.assertEqual(0,self.db.connection.execute('SELECT count(*) FROM source_invalidations').fetchone()[0])

    def test_verified_orphan_later_than_committed_tip_inhibits_recovery(self):
        self.engine.ingest(event())
        with patch.object(self.db,'_record_revisions',side_effect=OSError('commit capture')):
            self.assertFalse(self.engine.ingest(event('uncommitted',2)).accepted)
        # Simulate inability to persist a finding during a disabled/uncertain DB
        # by exercising the published-but-uncommitted evidence directly.
        self.engine.archive.publish(event('orphan',3),event('orphan',3).payload,self.policy,200)
        recovered=self.s.StateEngine(self.db,self.root/'archive',(self.policy,))
        self.assertFalse(recovered.entries_ready)
        self.db._clock_us=lambda:300
        self.assertTrue(recovered.ingest(event('fresh',10,received_at_us=201,known_at_us=201)).accepted)
        again=self.s.StateEngine(self.db,self.root/'archive',(self.policy,))
        self.assertTrue(again.entries_ready)

    def test_orphan_without_durable_finding_cannot_restore_old_book(self):
        self.engine.ingest(event())
        self.engine.archive.publish(event('orphan',2),event('orphan',2).payload,self.policy,200)
        recovered=self.s.StateEngine(self.db,self.root/'archive',(self.policy,))
        self.assertFalse(recovered.entries_ready)
