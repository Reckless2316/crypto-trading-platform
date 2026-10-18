"""Durability, idempotency, and fail-closed authority boundaries (no network)."""
import dataclasses
import sqlite3
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from bot.autonomous.contracts import (AccountRevision, ContractViolation, IntentState,
    CausalCutoffs, OrderEvent, OrderReference, PersistenceUnavailable, ReceiptStatus, ReviewJob,
    RiskLimits, RiskVerdict, SessionCutoff, VenueOrderResult, canonical_bytes, content_hash)
from tests.autonomous.support import HASH_A, HASH_B, order_proposal

RELEASE_HASH = content_hash({})


def load_ledger():
    # Keep the initial RED a direct assertion of the absent implementation.
    try:
        from bot.autonomous.ledger import Ledger
    except ImportError:
        raise AssertionError("durable Ledger has not been implemented") from None
    return Ledger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "ledger.sqlite3"
        self.ledger = load_ledger().open(self.path)
        self.ledger.create_paper_account("paper-test")

    def tearDown(self):
        if hasattr(self, "ledger"):
            self.ledger.close()
        self.directory.cleanup()

    def test_unbalanced_journal_is_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.post("paper-test", "bad", (("cash", 100), ("capital", -99)))
        self.assertTrue(self.ledger.post("paper-test", "ok", (("cash", 100), ("capital", -100))))
        self.assertFalse(self.ledger.post("paper-test", "ok", (("cash", 100), ("capital", -100))))

    def test_transaction_identity_is_content_bound(self):
        self.ledger.post("paper-test", "ok", (("cash", 100), ("capital", -100)))
        with self.assertRaises(ContractViolation):
            self.ledger.post("paper-test", "ok", (("cash", 101), ("capital", -101)))

    def test_invalid_money_and_missing_account_rejected(self):
        for value in (True, 2**63, 1.1):
            with self.assertRaises(ValueError):
                self.ledger.post("paper-test", "invalid", (("cash", value), ("capital", -value)))
        with self.assertRaises(ValueError):
            self.ledger.post("unknown", "missing", (("cash", 1), ("capital", -1)))

    def test_failed_posting_rolls_back_entire_journal(self):
        self.ledger.connection.execute("CREATE TRIGGER inject_failure BEFORE INSERT ON journal_postings WHEN NEW.line_no=1 BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(PersistenceUnavailable):
            self.ledger.post("paper-test", "rollback", (("cash", 1), ("capital", -1)))
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM journal_transactions").fetchone()[0])
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM journal_postings").fetchone()[0])
        self.assertFalse(self.ledger.dispatch_enabled)

    def test_schema_settings_and_append_only_protection(self):
        self.assertEqual(1, self.ledger.connection.execute("PRAGMA foreign_keys").fetchone()[0])
        self.assertEqual("wal", self.ledger.connection.execute("PRAGMA journal_mode").fetchone()[0])
        self.assertEqual(2, self.ledger.connection.execute("PRAGMA synchronous").fetchone()[0])
        required = {"accounts", "account_control", "candidate_control", "control_events", "source_events", "source_invalidations", "event_segments", "snapshots", "snapshot_inputs", "decisions", "candidate_results", "order_intents", "order_events", "reservations", "fills", "fill_corrections", "journal_transactions", "journal_postings", "balance_positions", "equity_marks", "external_flows", "reconciliations", "forecasts", "outcome_labels", "label_events", "evidence_manifests", "review_sessions", "review_jobs", "release_bundles", "release_events", "evaluation_runs"}
        present = {r[0] for r in self.ledger.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(required - present)
        self.ledger.post("paper-test", "ok", (("cash", 100), ("capital", -100)))
        for sql in ("DELETE FROM journal_transactions", "UPDATE journal_postings SET usd_delta_u=0"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.ledger.connection.execute(sql)

    def test_open_never_adopts_or_arms_account(self):
        self.assertFalse(self.ledger.account_view("paper-test").ready)
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM accounts WHERE mode='live'").fetchone()[0])
        with self.assertRaises(ValueError):
            self.ledger.account_view("live-default")
        self.assertEqual("none", self.ledger.connection.execute("SELECT permit FROM account_control").fetchone()[0])

    def test_reopen_preserves_journal_and_disarms(self):
        self.ledger.post("paper-test", "ok", (("cash", 100), ("capital", -100)))
        self.ledger.connection.execute("UPDATE account_control SET hard_killed=1,drawdown_latched=1,permit='entry'")
        self.ledger.close()
        self.ledger = load_ledger().open(self.path)
        self.assertFalse(self.ledger.post("paper-test", "ok", (("cash", 100), ("capital", -100))))
        row = self.ledger.connection.execute("SELECT permit,hard_killed,drawdown_latched FROM account_control").fetchone()
        self.assertEqual(("none", 1, 1), tuple(row))

    def test_migration_checksum_tampering_refuses_open(self):
        self.ledger.connection.execute("UPDATE schema_migrations SET checksum='tampered'")
        self.ledger.close()
        with self.assertRaises(PersistenceUnavailable):
            load_ledger().open(self.path)

    def test_foreign_keys_and_snapshot_exactly_one_reference(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("INSERT INTO source_invalidations VALUES ('missing','absent',1,'test',1)")
        self.seed()
        for refs in ((None, None), ("event", "segment")):
            with self.assertRaises(sqlite3.IntegrityError):
                self.ledger.connection.execute("INSERT INTO snapshot_inputs(snapshot_id,line_no,event_id,segment_hash,recorded_at_us) VALUES ('snapshot',0,?,?,1)", refs)

    def test_reservation_unready_fails_closed(self):
        p = order_proposal()
        with self.assertRaises(ContractViolation):
            self.ledger.reserve_intent(p, self.ledger.account_view(p.account_id).revision, 0, p.decision)
        self.assertEqual(0, self.ledger.connection.execute("SELECT count(*) FROM order_intents").fetchone()[0])

    def seed(self, *, valid=False):
        """Persist inert fixtures through the supported atomic import seam."""
        now = time.time_ns() // 1000
        original_clock=self.ledger._clock_us
        self.ledger._clock_us=lambda:now
        try:
            with self.ledger.transaction() as c:
                c.execute("INSERT INTO release_bundles(release_hash,manifest_json,expires_at_us,recorded_at_us) VALUES (?, '{}', ?, ?)", (RELEASE_HASH, now + 100_000_000, now))
                c.execute("INSERT INTO snapshots(snapshot_id,account_id,release_hash,candle_close_us,knowledge_cutoff_us,field_hash,state_json,lineage_hash,account_revision,valid,tokenizer_hash,token_count,recorded_at_us) VALUES (?,'paper-test',?,1000000,1000000,?,'{}',?,'{}',?,?,?,?)", (HASH_B, RELEASE_HASH, HASH_A, HASH_B, int(valid), HASH_A if valid else None, 10 if valid else None, now))
                c.execute("INSERT INTO decisions(decision_id,snapshot_id,request_id,deadline_us,model_id,status,recorded_at_us) VALUES ('decision-test',?,'request-test',?,'test','completed',?)", (HASH_B, now + 100_000_000, now))
                c.execute("INSERT INTO reconciliations(reconcile_id,account_id,started_at_us,finished_at_us,status,balances_json,open_orders_json,differences_json,recorded_at_us) VALUES ('reconcile','paper-test',?,?,'completed','[]','[]','[]',?)", (now, now, now))
        finally:
            self.ledger._clock_us=original_clock
        return now


    def ready(self, high_water='1'):
        now = self.seed(valid=True)
        from bot.autonomous.contracts import MarketRules
        c = self.ledger.connection
        c.execute("INSERT INTO candidate_results(decision_id,candidate_id,status,veto_codes_json,recorded_at_us) VALUES ('decision-test',1,'eligible','[]',?)", (now,))
        c.execute("INSERT INTO equity_marks(mark_id,account_id,lineage_hash,equity_usd_u,units,nav,high_water_nav,day_open_nav,drawdown,daily_loss,valuation_version,marked_at_us,recorded_at_us) VALUES ('mark','paper-test',?,10000000000,'10000','1',?,'1','0','0',0,?,?)", (HASH_A, high_water, now, now))
        c.execute("INSERT INTO balance_positions(account_id,asset,native_qty,cost_basis_usd_u,journal_sequence,version) VALUES ('paper-test','USD','10000',10000000000,0,0)")
        lease = time.monotonic_ns() + 100_000_000_000
        c.execute("UPDATE account_control SET permit='entry',permit_epoch=1,lease_boot_id='test-boot',lease_deadline_mono_ns=?,active_release_hash=?,reconcile_id='reconcile',version=version+1", (lease, RELEASE_HASH))
        c.execute("UPDATE account_readiness SET ready=1,private_heartbeat_us=?,version=version+1 WHERE account_id='paper-test'", (now,))
        from bot.autonomous.contracts import ExactRatio, ExactNavEvidence
        exact=ExactNavEvidence('paper-test','mark',self.ledger._revision('paper-test'),0,10000000000,
            ExactRatio('10000','1'),ExactRatio('1','1'),ExactRatio(*{'1':('1','1'),'1.2':('6','5')}[high_water]),ExactRatio('1','1'),HASH_A)
        fixture_observation={'heartbeat_at_us':now}
        c.execute("INSERT INTO accounting_evidence VALUES ('fixture-mark','paper-test','mark','local-paper',?,?,?,?,?,?)",
            (canonical_bytes(fixture_observation).decode(),content_hash(fixture_observation),now,now,now,now))
        c.execute("INSERT INTO exact_marks VALUES ('mark','paper-test','fixture-mark',?,'[]',0,'reconcile',1,?)",(exact.to_json(),now))
        limits = RiskLimits(armed=True,candidate_eligible=True,tokenizer_config_hash=HASH_A,calibration_config_hash=HASH_A)
        market = MarketRules('paper','AAVE-USD','AAVE','USD',Decimal('0.01'),Decimal('0.01'),Decimal('0.01'),Decimal('1'),Decimal('0.01'),2,HASH_A,now+100_000_000,True,True,now,10_000_000_000)
        self.ledger.configure_risk_checks(limits=limits, market=lambda _: market,
            assess=lambda p,a,c,l,m,n,**kw: RiskVerdict(True,(),a.revision,content_hash(p)), boot_id='test-boot')
        p=order_proposal()
        # The proposal deadline must sit inside the control lease.
        return dataclasses.replace(p, release_hash=RELEASE_HASH, decision=dataclasses.replace(p.decision,release_hash=RELEASE_HASH),control_epoch=1,deadline_mono_ns=lease-1_000_000_000)

    def reserve(self, p=None):
        p = self.ready() if p is None else p
        a = self.ledger.account_view(p.account_id)
        g = self.ledger.connection.execute("SELECT generation FROM accounts").fetchone()[0]
        return self.ledger.reserve_intent(p,a.revision,g,p.decision)

    def test_reservation_commits_and_cross_release_action_is_unique(self):
        p = self.ready()
        prepared = self.reserve(p)
        self.assertEqual(IntentState.RESERVED, prepared.state)
        self.assertEqual(101_000_000, prepared.reservation.cash_usd_u)
        self.assertEqual(1, self.ledger.connection.execute("SELECT count(*) FROM reservations").fetchone()[0])
        other = dataclasses.replace(p,release_hash=HASH_B,decision=dataclasses.replace(p.decision,release_hash=HASH_B))
        with self.assertRaises(ContractViolation):
            self.reserve(other)
        self.assertEqual(1, self.ledger.connection.execute("SELECT count(*) FROM order_intents").fetchone()[0])

    def test_reservation_duplicate_is_content_bound(self):
        p = self.ready()
        first = self.reserve(p)
        self.assertEqual(first, self.reserve(p))
        with self.assertRaises(ContractViolation):
            self.reserve(dataclasses.replace(p,base_quantity=Decimal('2')))

    def test_reservation_rechecks_revision_and_capacity(self):
        p = self.ready()
        old = self.ledger.account_view(p.account_id).revision
        self.ledger.post('paper-test','revision',(('cash',1),('capital',-1)))
        with self.assertRaises(ContractViolation):
            self.ledger.reserve_intent(p,old,0,p.decision)
        with self.assertRaises(ContractViolation):
            self.reserve(dataclasses.replace(p,base_quantity=Decimal('100')))

    def test_failed_reservation_rolls_back_intent(self):
        p = self.ready()
        self.ledger.connection.execute("CREATE TRIGGER inject_reservation BEFORE INSERT ON reservations BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(PersistenceUnavailable):
            self.reserve(p)
        self.assertEqual(0,self.ledger.connection.execute("SELECT count(*) FROM order_intents").fetchone()[0])

    def test_dispatch_marker_commits_and_rechecks_control(self):
        intent = self.reserve()
        marked = self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        self.assertEqual(IntentState.DISPATCHING, marked.state)
        self.assertEqual('dispatching',self.ledger.connection.execute('SELECT state FROM order_intents').fetchone()[0])
        self.assertFalse(self.ledger.connection.in_transaction)
        self.ledger.connection.execute("UPDATE account_control SET hard_killed=1,version=version+1")
        with self.assertRaises(ContractViolation):
            self.ledger.mark_dispatching(intent.intent_id,intent.generation)

    def test_crash_after_dispatch_keeps_reservation_and_becomes_unknown(self):
        intent = self.reserve()
        self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        self.ledger.close()
        self.ledger = load_ledger().open(self.path)
        view = self.ledger.account_view('paper-test')
        self.assertEqual((intent.intent_id,),view.unknown_intent_ids)
        self.assertEqual(101_000_000,view.reservations[0].cash_usd_u)
        self.assertFalse(view.ready)

    def test_order_events_are_typed_idempotent_and_keep_capacity(self):
        intent = self.reserve()
        self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        result = VenueOrderResult(OrderReference('paper-test',intent.intent_id,intent.client_order_id,'venue-order'),IntentState.OPEN,Decimal('0'),Decimal('1'),time.time_ns()//1000)
        event = OrderEvent('event-open',intent.intent_id,IntentState.OPEN,result,time.time_ns()//1000,venue_event_key='venue-open')
        self.assertTrue(self.ledger.append_order_event(event).appended)
        self.assertFalse(self.ledger.append_order_event(event).appended)
        with self.assertRaises(ContractViolation):
            self.ledger.append_order_event(dataclasses.replace(event,received_at_us=event.received_at_us+1))
        self.assertEqual(101_000_000,self.ledger.account_view('paper-test').reservations[0].cash_usd_u)

    def test_review_job_dedupe_and_missing_reference(self):
        job = ReviewJob('job','dedupe','session',HASH_A)
        self.assertTrue(self.ledger.append_review_job(job).appended)
        self.assertFalse(self.ledger.append_review_job(job).appended)
        with self.assertRaises(ContractViolation):
            self.ledger.append_review_job(dataclasses.replace(job,payload_hash=HASH_B))
        with self.assertRaises(ContractViolation):
            self.ledger.append_review_job(dataclasses.replace(job,job_id='job2',dedupe_key='new',session_id='missing'))

    def test_session_export_is_immutable_and_idempotent(self):
        now = self.seed()
        cutoff = SessionCutoff('paper-test',now,'reconcile')
        first = self.ledger.export_session(cutoff)
        self.ledger.post('paper-test','later',(('cash',100),('capital',-100)))
        self.assertEqual(first,self.ledger.export_session(cutoff))
        self.assertEqual(1,self.ledger.connection.execute('SELECT count(*) FROM review_sessions').fetchone()[0])
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute("UPDATE review_sessions SET cutoff_us=cutoff_us+1")

    def test_cash_competition_rechecks_pending_reservations(self):
        p = self.ready()
        # Both 1-unit orders individually fit $150; the second must include the
        # first committed worst-case cash requirement under BEGIN IMMEDIATE.
        self.ledger.connection.execute("UPDATE balance_positions SET native_qty='150'")
        self.reserve(p)
        other = dataclasses.replace(p, action_id='second', candle_close_us=p.candle_close_us+1)
        # A fresh authority-issued decision for the next candle.
        c = self.ledger.connection
        now = time.time_ns()//1000
        c.execute("INSERT INTO snapshots SELECT ?,account_id,release_hash,candle_close_us+1,knowledge_cutoff_us+1,field_hash,state_json,tokenizer_hash,token_count,lineage_hash,account_revision,valid,invalid_reason,recorded_at_us FROM snapshots", ('c'*64,))
        c.execute("INSERT INTO decisions SELECT 'decision-2',?,'request-2',deadline_us,model_id,response_json,response_hash,status,reason,received_at_us,recorded_at_us FROM decisions", ('c'*64,))
        c.execute("INSERT INTO candidate_results VALUES ('decision-2',1,'eligible',NULL,'[]',?)", (now,))
        other = dataclasses.replace(other,decision=dataclasses.replace(other.decision,decision_id='decision-2',request_id='request-2',snapshot_hash='c'*64))
        with self.assertRaises(ContractViolation):
            self.reserve(other)
        self.assertEqual(1,c.execute('SELECT count(*) FROM reservations').fetchone()[0])

    def test_reopen_rejects_removed_evidence_protection(self):
        self.ledger.connection.execute('DROP TRIGGER fills_no_update')
        self.ledger.close()
        with self.assertRaises(PersistenceUnavailable):
            load_ledger().open(self.path)

    def test_nested_transaction_failure_cannot_commit_partial_post(self):
        c = self.ledger.connection
        c.execute("CREATE TRIGGER inject_failure BEFORE INSERT ON journal_postings WHEN NEW.line_no=1 BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(PersistenceUnavailable):
            with self.ledger.transaction():
                try:
                    self.ledger.post('paper-test','nested',(('cash',1),('capital',-1)))
                except PersistenceUnavailable:
                    pass
        self.assertEqual(0,c.execute('SELECT count(*) FROM journal_transactions').fetchone()[0])

    def test_busy_failure_disables_dispatch_without_partial_write(self):
        other = sqlite3.connect(self.path,isolation_level=None)
        try:
            other.execute('BEGIN IMMEDIATE')
            with self.assertRaises(PersistenceUnavailable):
                self.ledger.post('paper-test','busy',(('cash',1),('capital',-1)))
            self.assertFalse(self.ledger.dispatch_enabled)
        finally:
            other.rollback()
            other.close()
        self.assertEqual(0,self.ledger.connection.execute('SELECT count(*) FROM journal_transactions').fetchone()[0])

    def test_changed_venue_event_key_payload_is_rejected(self):
        intent=self.reserve()
        self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        result=VenueOrderResult(OrderReference('paper-test',intent.intent_id,intent.client_order_id,'venue-order'),IntentState.OPEN,Decimal('0'),Decimal('1'),time.time_ns()//1000)
        event=OrderEvent('open',intent.intent_id,IntentState.OPEN,result,time.time_ns()//1000,venue_event_key='venue-key')
        self.ledger.append_order_event(event)
        with self.assertRaises(ContractViolation):
            self.ledger.append_order_event(dataclasses.replace(event,event_id='other'))

    def test_forged_assessor_verdict_never_authorizes(self):
        p=self.ready()
        limits,market,_,boot=self.ledger._risk_config
        self.ledger.configure_risk_checks(limits=limits,market=market,boot_id=boot,
            assess=lambda p,a,c,l,m,n,**kw:RiskVerdict(True,(),a.revision,HASH_B))
        with self.assertRaises(ContractViolation):
            self.reserve(p)

    def test_session_export_does_not_include_future_source_or_control(self):
        now=self.seed()
        c=self.ledger.connection
        c.execute("INSERT INTO source_events(event_id,source,stream,logical_key,revision,event_at_us,received_at_us,known_at_us,payload_json,payload_hash,recorded_at_us) VALUES ('future','test','book','future',0,?,?,?,'{}',?,?)",(now+1,now+1,now+1,content_hash({}),now))
        c.execute("UPDATE account_control SET halt_reason='future information',updated_at_us=?",(now+1,))
        archive=self.ledger.export_session(SessionCutoff('paper-test',now,'reconcile'))
        from bot.autonomous.contracts import strict_json
        payload=strict_json(c.execute('SELECT export_json FROM review_sessions').fetchone()[0])
        self.assertEqual([],payload['source_events'])
        self.assertNotIn('future information',str(payload))

    def test_changed_release_content_fails_startup_integrity(self):
        now=time.time_ns()//1000
        self.ledger.connection.execute("INSERT INTO release_bundles VALUES (?, '{}', NULL, ?, ?)",(HASH_A,now+10000000,now))
        self.ledger.close()
        with self.assertRaises(PersistenceUnavailable):
            load_ledger().open(self.path)

    def test_duplicate_correction_retains_original_and_rejects_changed_key(self):
        intent=self.reserve()
        c=self.ledger.connection
        now=time.time_ns()//1000
        c.execute("INSERT INTO fills VALUES ('fill','paper-test',?,'paper','trade','order','AAVE-USD','buy','1','100','100','USD','1',?,?,?,?)",(intent.intent_id,now,now,HASH_A,now))
        c.execute("INSERT INTO fill_corrections VALUES ('correction','fill',?,'{}',?,NULL,?,'verified correction',?)",(HASH_A,content_hash({}),now,now))
        for correction in ('correction','different-id'):
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO fill_corrections VALUES (?,'fill',?,'{}',?,NULL,?,'changed',?)",(correction,HASH_A,content_hash({}),now,now))
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("UPDATE fill_corrections SET reason='rewritten'")
        self.assertEqual(1,c.execute('SELECT count(*) FROM fills').fetchone()[0])
        self.assertEqual(1,c.execute('SELECT count(*) FROM fill_corrections').fetchone()[0])

    def test_startup_rejects_negative_decimal_projection(self):
        self.ledger.connection.execute("INSERT INTO balance_positions VALUES ('paper-test','USD','-1',0,0,0)")
        self.ledger.close()
        with self.assertRaises(PersistenceUnavailable):
            load_ledger().open(self.path)

    def test_dispatch_assessor_does_not_count_own_reservation_twice(self):
        p=self.ready()
        limits,market,_,boot=self.ledger._risk_config
        observed=[]
        def assessor(proposal,account,control,limits,market,now,**kw):
            observed.append(len(account.reservations))
            return RiskVerdict(True,(),account.revision,content_hash(proposal))
        self.ledger.configure_risk_checks(limits=limits,market=market,boot_id=boot,assess=assessor)
        intent=self.reserve(p)
        self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        self.assertEqual([0,0],observed)

    def test_filled_status_requires_complete_cumulative_quantity(self):
        intent=self.reserve()
        self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        result=VenueOrderResult(OrderReference('paper-test',intent.intent_id,intent.client_order_id,'venue-order'),IntentState.FILLED,Decimal('0'),Decimal('1'),time.time_ns()//1000)
        event=OrderEvent('bad-filled',intent.intent_id,IntentState.FILLED,result,time.time_ns()//1000)
        with self.assertRaises(ContractViolation):
            self.ledger.append_order_event(event)

    def test_session_order_projection_is_rebuilt_at_cutoff(self):
        p=self.ready()
        intent=self.reserve(p)
        cutoff=time.time_ns()//1000
        self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        archive=self.ledger.export_session(SessionCutoff('paper-test',cutoff,'reconcile'))
        from bot.autonomous.contracts import strict_json
        exported=strict_json(self.ledger.connection.execute('SELECT export_json FROM review_sessions').fetchone()[0])
        self.assertEqual('reserved',exported['order_intents'][0]['state'])
        self.assertEqual(0,exported['order_intents'][0]['version'])

    def test_correction_and_label_keys_are_unique(self):
        # Dedicated immutable event keys are schema contracts, even before fill accounting.
        for table, key in [('fill_corrections','correction_id'),('external_flows','flow_id'),('label_events','label_event_id')]:
            info = self.ledger.connection.execute('PRAGMA table_info('+table+')').fetchall()
            self.assertTrue(any(row['name']==key and row['pk'] for row in info))
            self.assertIn('recorded_at_us',{row['name'] for row in info})

    # Regression coverage for independent review R1-R5.
    def export_payload(self, cutoff):
        from bot.autonomous.contracts import strict_json
        archive = self.ledger.export_session(cutoff)
        raw = self.ledger.connection.execute('SELECT export_json FROM review_sessions WHERE session_id=?', (archive.session_id,)).fetchone()[0]
        return archive, strict_json(raw)

    def status_event(self, intent, event_id, state, received, *, filled='0', remaining='1'):
        result = VenueOrderResult(OrderReference('paper-test',intent.intent_id,intent.client_order_id,'venue-order'),state,Decimal(filled),Decimal(remaining),received)
        return OrderEvent(event_id,intent.intent_id,state,result,received,event_at_us=received)

    def test_review_r1_nested_dispatch_rejects_before_mutation(self):
        intent = self.reserve()
        c = self.ledger.connection
        with self.ledger.transaction():
            with self.assertRaises(ContractViolation):
                self.ledger.mark_dispatching(intent.intent_id,intent.generation)
            self.assertEqual('reserved',c.execute('SELECT state FROM order_intents').fetchone()[0])
            self.assertEqual(0,c.execute("SELECT count(*) FROM order_events WHERE kind='dispatching'").fetchone()[0])
            self.assertTrue(self.ledger.post('paper-test','nested-safe',(('cash',1),('capital',-1))))
        self.assertFalse(c.in_transaction)
        self.assertEqual(1,c.execute("SELECT count(*) FROM journal_transactions WHERE transaction_id='nested-safe'").fetchone()[0])

    def test_review_r1_uncommitted_reservation_cannot_dispatch(self):
        p = self.ready()
        with self.assertRaises(ContractViolation):
            with self.ledger.transaction():
                intent = self.reserve(p)
                self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        self.assertEqual(0,self.ledger.connection.execute('SELECT count(*) FROM order_intents').fetchone()[0])

    def test_review_r2_delayed_first_export_uses_historical_revision_and_generation(self):
        now = self.seed()
        self.ledger._clock_us=lambda:now+10
        self.ledger.post('paper-test','before',(('cash',100),('capital',-100)))
        historical = self.ledger.account_view('paper-test').revision
        self.ledger._clock_us=lambda:now+30
        self.ledger.post('paper-test','after',(('cash',50),('capital',-50)))
        self.ledger.close()
        self.ledger=load_ledger().open(self.path)
        archive, payload = self.export_payload(SessionCutoff('paper-test',now+20,'reconcile'))
        self.assertEqual(historical,archive.account_revision)
        self.assertEqual(0,payload['accounts'][0]['generation'])
        self.assertEqual(['before'],[row['transaction_id'] for row in payload['journal_transactions']])
        self.assertNotIn('account_control',payload)

    def test_review_r2_all_event_receipt_knowledge_and_segment_endpoints_are_causal(self):
        p = self.ready()
        now = time.time_ns()//1000+1000
        self.ledger._clock_us=lambda:now
        intent = self.reserve(p)
        self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        self.ledger.append_order_event(self.status_event(intent,'future-open',IntentState.OPEN,now+100))
        c=self.ledger.connection
        c.execute("INSERT INTO source_events(event_id,source,stream,logical_key,revision,event_at_us,received_at_us,known_at_us,payload_json,payload_hash,recorded_at_us) VALUES ('past','test','metric','past',0,?,?,?,'{}',?,?)",(now,now,now,content_hash({}),now))
        c.execute("INSERT INTO source_invalidations VALUES ('future-invalidation','past',?,'future',?)",(now+100,now))
        c.execute("INSERT INTO event_segments VALUES (?,'test','AAVE-USD',0,1,?,?,?,?,?,1,'segment',?,?)",(HASH_A,now,now+100,now,now+100,HASH_B,now,now))
        c.execute("INSERT INTO fills VALUES ('future-fill','paper-test',?,'paper','future-trade','order','AAVE-USD','buy','1','100','100','USD','1',?,?,?,?)",(intent.intent_id,now+100,now+100,HASH_A,now))
        archive,payload=self.export_payload(SessionCutoff('paper-test',now+10,'reconcile'))
        self.assertEqual([],payload['source_invalidations'])
        self.assertEqual([],payload['event_segments'])
        self.assertEqual([],payload['fills'])
        self.assertNotIn('future-open',[row['event_id'] for row in payload['order_events']])
        self.assertEqual('dispatching',payload['order_intents'][0]['state'])

    def test_review_r3_same_timestamp_order_replay_uses_durable_append_order(self):
        p=self.ready()
        now=time.time_ns()//1000+1000
        self.ledger._clock_us=lambda:now
        intent=self.reserve(p)
        self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        _,payload=self.export_payload(SessionCutoff('paper-test',now,'reconcile'))
        self.assertEqual('dispatching',payload['order_intents'][0]['state'])
        sequences=[row['append_sequence'] for row in payload['order_events']]
        self.assertEqual(len(sequences),len(set(sequences)))

    def test_review_r3_clock_regression_and_reverse_ids_preserve_order(self):
        p=self.ready()
        base=time.time_ns()//1000+1000
        recorded=[base+100]
        self.ledger._clock_us=lambda:recorded[0]
        intent=self.reserve(p)
        recorded[0]=base+90
        self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        recorded[0]=base+80
        self.ledger.append_order_event(self.status_event(intent,'zzz-open',IntentState.OPEN,base+80))
        recorded[0]=base+70
        self.ledger.append_order_event(self.status_event(intent,'aaa-partial',IntentState.PARTIAL,base+70,filled='.5',remaining='.5'))
        _,payload=self.export_payload(SessionCutoff('paper-test',base+200,'reconcile'))
        self.assertEqual('partial',payload['order_intents'][0]['state'])
        self.assertEqual(3,payload['order_intents'][0]['version'])

    def test_review_r4_manifest_ids_use_real_primary_keys(self):
        now=self.seed()
        c=self.ledger.connection
        self.ledger._clock_us=lambda:now
        with self.ledger.transaction():
            c.execute("INSERT INTO snapshots SELECT 'snapshot-2',account_id,release_hash,candle_close_us,knowledge_cutoff_us,field_hash,state_json,tokenizer_hash,token_count,lineage_hash,account_revision,valid,invalid_reason,recorded_at_us FROM snapshots")
            for run in ('run-1','run-2'):
                c.execute("INSERT INTO evaluation_runs VALUES (?,?,?,?,?,'{}','completed',?)",(run,RELEASE_HASH,HASH_A,HASH_B,'holdout',now))
            c.execute("INSERT INTO forecasts VALUES ('forecast','decision-test',1,'buy',4,'[]',NULL,NULL,0,?,?)",(HASH_A,now))
            c.execute("INSERT INTO outcome_labels VALUES ('label','forecast',0,?,NULL,'pending',NULL,NULL,NULL,NULL,0)",(now+100,))
            for revision,event_id,previous in ((0,'label-pending',None),(1,'label-corrected','label-pending')):
                c.execute("INSERT INTO label_events VALUES (?,'label',?,?,'pending','{}',?,?)",(event_id,revision,previous,content_hash({}),now))
            c.execute("INSERT INTO source_events(event_id,source,stream,logical_key,revision,event_at_us,received_at_us,known_at_us,payload_json,payload_hash,recorded_at_us) VALUES ('source','test','metric','source',0,?,?,?,'{}',?,?)",(now,now,now,content_hash({}),now))
            for invalidation in ('invalidation-1','invalidation-2'):
                c.execute("INSERT INTO source_invalidations VALUES (?,'source',?,'test',?)",(invalidation,now,now))
            for line in (0,1):
                c.execute("INSERT INTO snapshot_inputs VALUES (?,?,'source',NULL,NULL,NULL,?)",(HASH_B,line,now))
        archive,_=self.export_payload(SessionCutoff('paper-test',now+10,'reconcile'))
        identities=[entry.record_id for entry in archive.event_manifest+archive.outcome_manifest+archive.artifact_manifest]
        self.assertEqual(len(identities),len(set(identities)))
        for expected in ('snapshots:'+HASH_B,'snapshots:snapshot-2','evaluation_runs:run-1','evaluation_runs:run-2','label_events:label-pending','label_events:label-corrected','source_invalidations:invalidation-1','source_invalidations:invalidation-2'):
            self.assertIn(expected,identities)
        composite=[identity for identity in identities if identity.startswith('snapshot_inputs:')]
        self.assertEqual(2,len(set(composite)))

    def test_review_r4_duplicate_manifest_record_id_is_rejected(self):
        make_manifest=getattr(self.ledger,'_manifest_entries',None)
        self.assertIsNotNone(make_manifest,'manifest construction needs duplicate identity validation')
        with self.assertRaises(ContractViolation):
            make_manifest({'snapshots':[{'snapshot_id':'same'},{'snapshot_id':'same'}]},('snapshots',))

    def test_review_r5_high_scale_order_ignores_ambient_decimal_precision(self):
        from decimal import localcontext
        p=self.ready()
        limits,provider,assess,boot=self.ledger._risk_config
        price=Decimal('1.00000000000000000000000000001')
        market=dataclasses.replace(provider(p.instrument),tick_size=Decimal('0.00000000000000000000000000001'),minimum_notional=Decimal('0'),minimum_quantity=Decimal('0'),lot_size=Decimal('0.00000000000000000000000000001'))
        self.ledger.configure_risk_checks(limits=limits,market=lambda _:market,assess=assess,boot_id=boot)
        p=dataclasses.replace(p,limit_price=price,base_quantity=price)
        with localcontext() as context:
            context.prec=6
            prepared=self.reserve(p)
        self.assertEqual(price,prepared.proposal.base_quantity)

    def test_review_r5_cumulative_quantity_is_exact_under_low_ambient_precision(self):
        from decimal import localcontext
        p=self.ready()
        total=Decimal('1.00000000000000000000000000001')
        limits,provider,assess,boot=self.ledger._risk_config
        market=dataclasses.replace(provider(p.instrument),lot_size=Decimal('0.00000000000000000000000000001'))
        self.ledger.configure_risk_checks(limits=limits,market=lambda _:market,assess=assess,boot_id=boot)
        intent=self.reserve(dataclasses.replace(p,base_quantity=total,max_fee_usd_u=1_000_001))
        self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        event=self.status_event(intent,'precise-partial',IntentState.PARTIAL,time.time_ns()//1000,filled='0.5',remaining='0.50000000000000000000000000001')
        with localcontext() as context:
            context.prec=6
            self.assertTrue(self.ledger.append_order_event(event).appended)

    def test_review_r2_event_before_commit_capture_uses_previous_committed_prefix(self):
        p=self.ready()
        base=time.time_ns()//1000+1000
        self.ledger._clock_us=lambda:base
        intent=self.reserve(p)
        historical=self.ledger.account_view('paper-test').revision
        # Dispatch evidence is written at T; the commit proof is captured at T+1.
        calls=iter((base+10,base+10,base+10,base+11))
        self.ledger._clock_us=lambda:next(calls)
        self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        self.ledger._clock_us=lambda:base+20
        archive,payload=self.export_payload(SessionCutoff('paper-test',base+10,'reconcile'))
        self.assertEqual(historical,archive.account_revision)
        self.assertEqual('reserved',payload['order_intents'][0]['state'])
        self.assertEqual(['reserved'],[row['kind'] for row in payload['order_events']])

    def test_review_r2_clock_regression_cutoff_preserves_committed_event_prefix(self):
        p=self.ready()
        base=time.time_ns()//1000+1000
        clock=[base+80]
        self.ledger._clock_us=lambda:clock[0]
        intent=self.reserve(p)
        self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        historical=self.ledger.account_view('paper-test').revision
        clock[0]=base+100
        self.ledger.append_order_event(self.status_event(intent,'open-at-100',IntentState.OPEN,base+100))
        clock[0]=base+90
        self.ledger.append_order_event(self.status_event(intent,'partial-at-90',IntentState.PARTIAL,base+90,filled='.5',remaining='.5'))
        archive,payload=self.export_payload(SessionCutoff('paper-test',base+95,'reconcile'))
        self.assertEqual(historical,archive.account_revision)
        self.assertEqual('dispatching',payload['order_intents'][0]['state'])
        self.assertEqual(['reserved','dispatching'],[row['kind'] for row in sorted(payload['order_events'],key=lambda row:row['append_sequence'])])

    def test_review_r2_nested_result_receipt_participates_in_revision_visibility(self):
        p=self.ready()
        base=time.time_ns()//1000+1000
        self.ledger._clock_us=lambda:base
        intent=self.reserve(p)
        self.ledger.mark_dispatching(intent.intent_id,intent.generation)
        historical=self.ledger.account_view('paper-test').revision
        event=self.status_event(intent,'nested-future-receipt',IntentState.OPEN,base)
        event=dataclasses.replace(event,result=dataclasses.replace(event.result,received_at_us=base+100))
        self.ledger.append_order_event(event)
        current=self.ledger.account_view('paper-test').revision
        archive,payload=self.export_payload(SessionCutoff('paper-test',base+10,'reconcile'))
        self.assertEqual(historical,archive.account_revision)
        self.assertEqual('dispatching',payload['order_intents'][0]['state'])
        later,later_payload=self.export_payload(SessionCutoff('paper-test',base+100,'reconcile'))
        self.assertEqual(current,later.account_revision)
        self.assertEqual('open',later_payload['order_intents'][0]['state'])

    def test_review_r2_non_economic_import_commits_new_prefix_without_revision_change(self):
        now=self.seed()
        base=time.time_ns()//1000+1000
        self.ledger._clock_us=lambda:base
        before=self.ledger.account_view('paper-test').revision
        count=self.ledger.connection.execute('SELECT count(*) FROM account_revision_history').fetchone()[0]
        with self.ledger.transaction() as c:
            c.execute("INSERT INTO source_events(event_id,source,stream,logical_key,revision,event_at_us,received_at_us,known_at_us,payload_json,payload_hash,recorded_at_us) VALUES ('new-observation','test','metric','new',0,?,?,?,'{}',?,?)",(base,base,base,content_hash({}),base))
        self.assertEqual(before,self.ledger.account_view('paper-test').revision)
        self.assertEqual(count+1,self.ledger.connection.execute('SELECT count(*) FROM account_revision_history').fetchone()[0])
        archive,payload=self.export_payload(SessionCutoff('paper-test',base,'reconcile'))
        self.assertEqual(['new-observation'],[row['event_id'] for row in payload['source_events']])
        self.assertEqual(before,archive.account_revision)
        with self.ledger.transaction():
            pass
        self.assertEqual(count+1,self.ledger.connection.execute('SELECT count(*) FROM account_revision_history').fetchone()[0])

    def test_review_r2_reconciliation_anchor_requires_selected_committed_prefix(self):
        self.seed()
        base=time.time_ns()//1000+1000
        # The reconciliation row is visible by timestamp T, but its committed
        # evidence-prefix proof is captured at T+1.
        self.ledger._clock_us=lambda:base+1
        with self.ledger.transaction() as c:
            c.execute("INSERT INTO reconciliations(reconcile_id,account_id,started_at_us,finished_at_us,status,balances_json,open_orders_json,differences_json,recorded_at_us) VALUES ('new-anchor','paper-test',?,?,'completed','[]','[]','[]',?)",(base,base,base))
        self.ledger._clock_us=lambda:base+2
        with self.assertRaises(ContractViolation):
            self.ledger.export_session(SessionCutoff('paper-test',base,'new-anchor'))
        self.assertEqual(0,self.ledger.connection.execute('SELECT count(*) FROM review_sessions').fetchone()[0])
        archive,payload=self.export_payload(SessionCutoff('paper-test',base+1,'new-anchor'))
        self.assertEqual('new-anchor',archive.cutoff.reconcile_id)
        self.assertIn('new-anchor',[row['reconcile_id'] for row in payload['reconciliations']])
        self.assertEqual(['paper-test'],[row['account_id'] for row in payload['accounts']])

    def test_review_r2_session_account_anchor_requires_creation_prefix(self):
        created=self.ledger.connection.execute('SELECT created_at_us FROM accounts').fetchone()[0]
        with self.assertRaises(ContractViolation):
            self.ledger.export_session(SessionCutoff('paper-test',created-1,'missing'))
        with self.assertRaises(ContractViolation):
            self.ledger.export_session(SessionCutoff('different-account',created+1,'missing'))
        self.assertEqual(0,self.ledger.connection.execute('SELECT count(*) FROM review_sessions').fetchone()[0])


class RiskAuthorityTests(unittest.TestCase):
    """Task 11: the deterministic risk layer wired through durable reservation."""
    setUp, tearDown = LedgerTests.setUp, LedgerTests.tearDown
    seed, ready, reserve = LedgerTests.seed, LedgerTests.ready, LedgerTests.reserve

    def next_decision(self, p, k):
        """Fresh authority-issued decision for candle +k (same fixture rows)."""
        c = self.ledger.connection
        snap = content_hash(('snap', k))
        c.execute("INSERT INTO snapshots SELECT ?,account_id,release_hash,candle_close_us+?,knowledge_cutoff_us+?,field_hash,state_json,tokenizer_hash,token_count,lineage_hash,account_revision,valid,invalid_reason,recorded_at_us FROM snapshots WHERE snapshot_id=?", (snap, k, k, HASH_B))
        c.execute("INSERT INTO decisions SELECT ?,?,?,deadline_us,model_id,response_json,response_hash,status,reason,received_at_us,recorded_at_us FROM decisions WHERE decision_id='decision-test'", (f'decision-{k}', snap, f'request-{k}'))
        c.execute("INSERT INTO candidate_results VALUES (?,1,'eligible',NULL,'[]',?)", (f'decision-{k}', time.time_ns()//1000))
        return dataclasses.replace(p, action_id=f'action-{k}', candle_close_us=p.candle_close_us+k,
            decision=dataclasses.replace(p.decision, decision_id=f'decision-{k}', request_id=f'request-{k}', snapshot_hash=snap))

    def wire_real_risk(self):
        from bot.autonomous import risk
        limits, market, _, boot = self.ledger._risk_config
        self.ledger.configure_risk_checks(limits=limits, market=market, assess=risk.assess, boot_id=boot)

    def test_real_risk_assess_admits_valid_entry_through_reservation(self):
        p = self.ready()
        self.wire_real_risk()
        self.assertEqual(IntentState.RESERVED, self.reserve(p).state)

    def test_concurrent_entries_admit_only_residual_loss_capacity(self):
        p = dataclasses.replace(self.ready(), loss_budget_usd_u=25_000_000)
        self.wire_real_risk()
        self.reserve(p)
        for k in range(1, 4):
            self.reserve(self.next_decision(p, k))
        # Four $25 budgets fill the 1% pending-loss room on $10,000; the fifth is refused.
        with self.assertRaisesRegex(ContractViolation, 'modeled loss capacity'):
            self.reserve(self.next_decision(p, 4))
        self.assertEqual(4, self.ledger.connection.execute('SELECT count(*) FROM reservations').fetchone()[0])

    def test_concurrent_entries_admit_only_residual_asset_capacity(self):
        p = dataclasses.replace(self.ready(), base_quantity=Decimal('3'), max_fee_usd_u=3_000_000)
        self.wire_real_risk()
        self.reserve(p)
        with self.assertRaisesRegex(ContractViolation, 'position/gross capacity'):
            self.reserve(self.next_decision(p, 1))
        self.assertEqual(1, self.ledger.connection.execute('SELECT count(*) FROM reservations').fetchone()[0])

    def test_ledger_gross_backstop_counts_existing_other_asset_holdings(self):
        from unittest.mock import patch
        from bot.autonomous.contracts import Holding
        p = self.ready()  # Deliberately keeps the permissive assessor fixture.
        account = self.ledger.account_view(p.account_id)
        held = Holding("ETH-USD", "ETH", Decimal("1"), 1_950_000_000,
            1_950_000_000, account.marked_at_us, HASH_A)
        current = dataclasses.replace(account, holdings=(held,))
        with patch.object(self.ledger, "_account_view", return_value=current):
            with self.assertRaisesRegex(ContractViolation, "position/gross capacity"):
                self.reserve(p)
        self.assertEqual(0, self.ledger.connection.execute('SELECT count(*) FROM reservations').fetchone()[0])

    def test_stale_account_revision_refuses_reservation(self):
        p = self.ready()
        g = self.ledger.connection.execute("SELECT generation FROM accounts").fetchone()[0]
        with self.assertRaisesRegex(ContractViolation, 'stale account revision'):
            self.ledger.reserve_intent(p, AccountRevision(99, 99), g, p.decision)

    def test_breach_found_at_reservation_persists_latch_without_model(self):
        p = self.ready(high_water='1.2')  # NAV 1 <= 0.85 * 1.2: drawdown boundary crossed.
        self.wire_real_risk()
        with self.assertRaises(ContractViolation):
            self.reserve(p)
        row = self.ledger.connection.execute('SELECT drawdown_latched FROM account_control').fetchone()
        self.assertEqual(1, row[0])
        self.assertEqual(1, self.ledger.connection.execute("SELECT count(*) FROM control_events WHERE kind='risk_halt'").fetchone()[0])
        self.assertEqual(0, self.ledger.connection.execute('SELECT count(*) FROM reservations').fetchone()[0])

    def test_assessor_alone_refuses_when_ledger_checks_pass(self):
        p = self.ready()
        limits, market, _, boot = self.ledger._risk_config
        self.ledger.configure_risk_checks(limits=limits, market=market, boot_id=boot,
            assess=lambda proposal, account, control, limits, market, now, **kw: RiskVerdict(False, ('ASSESSOR_ONLY',), account.revision, content_hash(proposal)))
        with self.assertRaisesRegex(ContractViolation, 'current risk recheck refused'):
            self.reserve(p)
        self.assertEqual(0, self.ledger.connection.execute('SELECT count(*) FROM reservations').fetchone()[0])

    def test_hard_kill_blocks_new_reserve_and_dispatch_of_a_reserved_intent(self):
        p = self.ready()
        prepared = self.reserve(p)
        generation = self.ledger.connection.execute('SELECT generation FROM accounts').fetchone()[0]
        self.ledger.connection.execute("UPDATE account_control SET hard_killed=1 WHERE account_id='paper-test'")
        self.ledger.connection.commit()
        with self.assertRaisesRegex(ContractViolation, 'disabled/stale control'):
            self.ledger.mark_dispatching(prepared.intent_id, generation)
        self.assertEqual('reserved', self.ledger.connection.execute('SELECT state FROM order_intents').fetchone()[0])
        with self.assertRaisesRegex(ContractViolation, 'disabled/stale control'):
            self.reserve(self.next_decision(p, 1))
        self.assertEqual(1, self.ledger.connection.execute('SELECT count(*) FROM reservations').fetchone()[0])

    def test_operator_kill_rejects_reserved_intent_and_releases_cash(self):
        p = self.ready()
        prepared = self.reserve(p)
        self.assertTrue(self.ledger.latch_operator_kill(p.account_id))
        self.assertEqual('rejected', self.ledger.connection.execute('SELECT state FROM order_intents').fetchone()[0])
        self.assertEqual(0, self.ledger.connection.execute('SELECT cash_usd_u FROM reservations').fetchone()[0])
        self.assertEqual((), self.ledger.account_view(p.account_id).reservations)
        generation = self.ledger.connection.execute('SELECT generation FROM accounts').fetchone()[0]
        with self.assertRaisesRegex(ContractViolation, 'disabled/stale control'):
            self.ledger.mark_dispatching(prepared.intent_id, generation)

    def test_drawdown_latch_rejects_a_reserved_buy_and_releases_cash(self):
        from fractions import Fraction
        p = self.ready()
        self.reserve(p)
        with self.ledger.transaction():
            self.ledger._latch_losses(p.account_id, 'mark', Fraction(1), Fraction('6/5'), Fraction(1))
        self.assertEqual((1, 0), tuple(self.ledger.connection.execute(
            'SELECT drawdown_latched, daily_halted FROM account_control').fetchone()))
        self.assertEqual('rejected', self.ledger.connection.execute('SELECT state FROM order_intents').fetchone()[0])
        self.assertEqual(0, self.ledger.connection.execute('SELECT cash_usd_u FROM reservations').fetchone()[0])


class AccountingTests(unittest.TestCase):
    """All observations below are local paper fixtures, not venue attestations."""
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=Path(self.directory.name)/'accounting.sqlite'
        self.ledger=load_ledger().open(self.path)
        self.now=1728000000000  # exact UTC midnight
        self.ledger._clock_us=lambda:self.now
        self.ledger.create_paper_account('paper-test')

    def tearDown(self):
        self.ledger.close()
        self.directory.cleanup()

    def evidence(self, key, kind, payload):
        self.assertTrue(hasattr(self.ledger,'import_local_evidence'), 'accounting evidence intake missing')
        return self.ledger.import_local_evidence('paper-test',key,kind,payload,
            event_at_us=self.now,received_at_us=self.now,known_at_us=self.now)

    def initialize(self, cash='10000', extra=()):
        balances=[{'asset':'USD','quantity':cash,'basis_usd_u':int(Decimal(cash)*1000000)}]+list(extra)
        evidence=self.evidence('initial','initial',{'balances':balances,'units':'100'})
        self.ledger.initialize_account(evidence)
        return self.mark('opening')

    def balances(self):
        return {r['asset']:r['native_qty'] for r in self.ledger.connection.execute('SELECT * FROM balance_positions')}

    def mark(self,key,books=None,**changes):
        payload={'balances':self.balances(),'books':books or {},'heartbeat_at_us':self.now,
            'open_intent_ids':[r[0] for r in self.ledger.connection.execute("SELECT intent_id FROM order_intents WHERE state NOT IN ('filled','canceled','rejected') ORDER BY intent_id")],
            'liabilities_usd_u':0,'exit_cost_usd_u':0}
        payload.update(changes)
        return self.ledger.record_mark(self.evidence(key,'mark',payload))

    def flow(self,key,value,fee=0):
        pre=self.ledger.connection.execute('SELECT mark_id FROM equity_marks ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        return self.ledger.record_flow(self.evidence(key,'flow',{'transfer_key':key,'asset':'USD',
            'quantity':str(Decimal(value)/1000000),'usd_value_u':value,'fee_usd_u':fee,'preflow_mark_id':pre}))

    def wire_accounting_admission(self):
        """Wire current paper risk inputs without replacing accounting evidence."""
        from unittest.mock import patch
        from bot.autonomous.contracts import MarketRules
        with patch('tests.autonomous.test_ledger.time.time_ns',return_value=self.now*1000):
            LedgerTests.seed(self,valid=True)
        with self.ledger.transaction() as c:
            c.execute("INSERT INTO candidate_results(decision_id,candidate_id,status,veto_codes_json,recorded_at_us) VALUES ('decision-test',1,'eligible','[]',?)",(self.now,))
            c.execute("UPDATE account_control SET permit='entry',permit_epoch=1,lease_boot_id='test-boot',lease_deadline_mono_ns=100000000000,active_release_hash=?,version=version+1",(RELEASE_HASH,))
        limits=RiskLimits(armed=True,candidate_eligible=True,tokenizer_config_hash=HASH_A,calibration_config_hash=HASH_A)
        def market(_):
            return MarketRules('paper','AAVE-USD','AAVE','USD',Decimal('.01'),Decimal('.01'),Decimal('.01'),Decimal('1'),Decimal('.01'),2,HASH_A,self.now+100000000,True,True,self.now,10000000000)
        self.ledger.configure_risk_checks(limits=limits,market=market,
            assess=lambda p,a,c,l,m,n,**kw:RiskVerdict(True,(),a.revision,content_hash(p)),
            boot_id='test-boot',monotonic_ns=lambda:1)
        p=order_proposal()
        return dataclasses.replace(p,release_hash=RELEASE_HASH,
            decision=dataclasses.replace(p.decision,release_hash=RELEASE_HASH),
            control_epoch=1,deadline_mono_ns=100000000000)

    def near_expiry_held_mark(self, source):
        self.initialize(extra=({'asset':'OTHER','quantity':'1','basis_usd_u':1000000},))
        observed=self.now
        book={'at_us':observed,'bids':[['1','1']]}
        if source=='fx':
            book.update(quote_asset='USDC',fx_id=self.evidence('held-fx','fx',
                {'legs':[{'from_asset':'USDC','to_asset':'USD','to_units_per_from_unit':'1'}]}))
        self.mark('held-opening',books={'OTHER':book})
        self.now+=1900000
        if source=='fx':book['at_us']=self.now
        self.mark('held-near-expiry',books={'OTHER':book})
        return self.wire_accounting_admission()

    def test_held_book_and_fx_expire_independently_of_mark_for_readiness(self):
        for source in ('book','fx'):
            with self.subTest(source=source):
                if source=='fx':self.tearDown();self.setUp()
                self.near_expiry_held_mark(source)
                self.assertTrue(self.ledger.account_view('paper-test').ready)
                self.now+=200000
                view=self.ledger.account_view('paper-test')
                self.assertEqual(200000,self.now-view.marked_at_us)
                self.assertFalse(view.ready,'retained held '+source+' is older than two seconds')

    def test_held_book_and_fx_expiry_refuses_new_reservation(self):
        for source in ('book','fx'):
            with self.subTest(source=source):
                if source=='fx':self.tearDown();self.setUp()
                p=self.near_expiry_held_mark(source)
                self.now+=200000
                view=self.ledger.account_view('paper-test')
                with self.assertRaises(ContractViolation):
                    self.ledger.reserve_intent(p,view.revision,0,p.decision)
                self.assertEqual(0,self.ledger.connection.execute('SELECT count(*) FROM order_intents').fetchone()[0])

    def test_committed_reservation_rechecks_held_book_and_fx_at_dispatch(self):
        for source in ('book','fx'):
            with self.subTest(source=source):
                if source=='fx':self.tearDown();self.setUp()
                p=self.near_expiry_held_mark(source)
                view=self.ledger.account_view('paper-test')
                prepared=self.ledger.reserve_intent(p,view.revision,0,p.decision)
                self.assertFalse(self.ledger.connection.in_transaction)
                self.now+=200000
                with self.assertRaises(ContractViolation):
                    self.ledger.mark_dispatching(prepared.intent_id,0)
                self.assertEqual('reserved',self.ledger.connection.execute('SELECT state FROM order_intents').fetchone()[0])

    def test_current_accounting_mark_rebinds_only_non_economic_revisions(self):
        self.initialize()
        p=self.wire_accounting_admission()
        before=self.ledger.account_view('paper-test')
        prepared=self.ledger.reserve_intent(p,before.revision,0,p.decision)
        reserved=self.ledger.account_view('paper-test')
        self.assertTrue(reserved.ready)
        self.assertNotEqual(before.revision,reserved.revision)
        self.assertEqual(before.exact_nav.mark_id,reserved.exact_nav.mark_id)
        self.assertEqual(reserved.revision,reserved.exact_nav.revision)
        self.assertEqual(IntentState.DISPATCHING,self.ledger.mark_dispatching(prepared.intent_id,0).state)
        self.assertTrue(self.flow('economic-change',1000000))
        self.assertFalse(self.ledger.account_view('paper-test').ready)

    def test_open_flow_cannot_refresh_expired_held_valuation_sources(self):
        for source in ('book','fx'):
            with self.subTest(source=source):
                if source=='fx':self.tearDown();self.setUp()
                self.near_expiry_held_mark(source)
                self.now+=200000
                self.assertFalse(self.flow('stale-held-flow',1000000))
                self.assertEqual(0,self.ledger.connection.execute('SELECT count(*) FROM external_flows').fetchone()[0])

    def test_delayed_mark_application_cannot_refresh_held_book_or_fx(self):
        for source in ('book','fx'):
            with self.subTest(source=source):
                if source=='fx':self.tearDown();self.setUp()
                self.near_expiry_held_mark(source)
                row=self.ledger.connection.execute("SELECT payload_json FROM accounting_evidence WHERE evidence_id='held-near-expiry'").fetchone()
                from bot.autonomous.contracts import strict_json
                evidence=self.evidence('delayed-mark','mark',strict_json(row[0]))
                self.now+=200000
                mark=self.ledger.record_mark(evidence)
                self.assertEqual(0,self.ledger.connection.execute('SELECT ready FROM exact_marks WHERE mark_id=?',(mark,)).fetchone()[0])

    def test_ledger_written_loss_mark_halts_the_candle(self):
        from types import SimpleNamespace
        from fractions import Fraction
        from bot.autonomous.worker import PaperWorker
        self.initialize()
        self.assertTrue(self.flow('loss', 1000000, 1500150000))
        self.mark('worker-loss')
        view = self.ledger.account_view('paper-test')
        self.assertEqual(Fraction(85), view.exact_nav.nav.fraction())
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        worker.attach_equity(self.ledger.connection, 'paper-test')

        def consider(_cycle):
            raise AssertionError('a ledger loss mark must halt before consider')

        result = worker.on_close(4_000, consider)
        self.assertEqual(result['status'], 'halted')
        self.assertFalse(result['dispatched'])
        self.assertEqual(worker.considered, 0)
        self.assertEqual(1, self.ledger.connection.execute(
            'SELECT drawdown_latched FROM account_control').fetchone()[0])

    def test_delayed_closed_redeposit_preserves_exact_loss_history_and_requires_new_mark(self):
        from fractions import Fraction
        self.initialize()
        self.assertTrue(self.flow('loss',1000000,1500150000))
        self.mark('preclosure-loss')
        before=self.ledger.account_view('paper-test').exact_nav
        self.assertEqual(Fraction(85),before.nav.fraction())
        with self.ledger.transaction() as c:
            c.execute('UPDATE account_control SET crisis_halted=1,hard_killed=1')
        self.assertTrue(self.flow('close',-8500850000))
        self.now+=2000001
        self.mark('closed-account-cannot-refresh-zero-units')
        self.ledger.close();self.ledger=load_ledger().open(self.path)
        self.ledger._clock_us=lambda:self.now
        self.assertTrue(self.flow('delayed-redeposit',5000000000))
        view=self.ledger.account_view('paper-test')
        self.assertFalse(view.ready)
        self.assertEqual(Fraction(1000,17),view.exact_nav.units.fraction())
        self.assertEqual((before.nav,before.high_water_nav,before.day_open_nav),
            (view.exact_nav.nav,view.exact_nav.high_water_nav,view.exact_nav.day_open_nav))
        self.mark('current-reopened-reconciliation')
        self.assertTrue(self.ledger.account_view('paper-test').ready)
        self.assertEqual((1,1,1,1,'none'),tuple(self.ledger.connection.execute(
            'SELECT drawdown_latched,daily_halted,crisis_halted,hard_killed,permit FROM account_control').fetchone()))
        self.ledger.close();self.ledger=load_ledger().open(self.path)
        self.ledger._clock_us=lambda:self.now
        self.assertEqual(Fraction(1000,17),self.ledger.account_view('paper-test').exact_nav.units.fraction())

    def test_closed_redeposit_requires_fresh_transfer_not_fresh_preclosure_mark(self):
        self.initialize()
        self.assertTrue(self.flow('close',-10000000000))
        pre=self.ledger.connection.execute('SELECT last_mark_id FROM accounting_state').fetchone()[0]
        evidence=self.evidence('stale-redeposit','flow',{'transfer_key':'stale-redeposit','asset':'USD',
            'quantity':'5000','usd_value_u':5000000000,'fee_usd_u':0,'preflow_mark_id':pre})
        self.now+=2000001
        self.assertFalse(self.ledger.record_flow(evidence))
        self.assertEqual((1,1),tuple(self.ledger.connection.execute('SELECT closed,unresolved FROM accounting_state').fetchone()))
        self.assertTrue(self.flow('fresh-redeposit',5000000000))
        self.mark('fresh-after-redeposit')
        self.assertFalse(self.ledger.account_view('paper-test').ready,'existing review incident must survive redeposit')

    def intent(self,key,side='buy',quantity='10',price='100'):
        from bot.autonomous.contracts import Side
        p=dataclasses.replace(order_proposal(),action_id=key,side=Side(side),base_quantity=Decimal(quantity),
            limit_price=Decimal(price),reducing_only=side=='sell',max_fee_usd_u=1000000)
        with self.ledger.transaction() as c:
            c.execute("INSERT OR IGNORE INTO release_bundles VALUES (?,'{}',NULL,?,?)",(RELEASE_HASH,self.now+100000000,self.now))
            c.execute("INSERT INTO order_intents VALUES (?,?,NULL,1,?,?,?,'AAVE-USD',?,?,?,?,?,'open',NULL,0,0,0,?,?,?)",
                (key,'paper-test',RELEASE_HASH,self.now,key,side,quantity,price,1000000,key,p.to_json(),AccountRevision().to_json(),self.now))
            c.execute("INSERT INTO reservations VALUES (?,?,?,1000000000,25000000,?,'open',0,?)",(key,1001000000 if side=='buy' else 0,quantity,quantity,self.now))
        return key

    def fill(self,key,intent,quantity='1',quote='100',fee_asset='USD',fee='1',**changes):
        payload={'fill_id':key,'intent_id':intent,'venue':'paper','trade_id':key,'order_id':intent,
            'instrument':'AAVE-USD','side':'buy','base_quantity':quantity,'quote_quantity':quote,
            'price':format(Decimal(quote)/Decimal(quantity),'f'),'fee_asset':fee_asset,'fee_quantity':fee,
            'base_semantics':'gross','fee_final':True}
        payload.update(changes)
        self.ledger.record_fill(self.evidence(key,'fill',payload))
        return payload

    def test_deposit_loss_closure_restart_and_redeposit_preserve_latch(self):
        self.initialize('8500')
        # Opening NAV 85; establish genuine preceding high NAV via 10000 opening instead.
        self.ledger.close()
        self.directory.cleanup()
        self.setUp()
        self.initialize()
        self.intent('buy',quantity='10')
        self.fill('buy-fill','buy','10','1500','USD','0')
        self.mark('loss',books={'AAVE':{'at_us':self.now,'bids':[['0','10']]}})
        self.assertTrue(self.ledger.connection.execute('SELECT drawdown_latched FROM account_control').fetchone()[0])
        self.flow('deposit',8500000000)
        view=self.ledger.account_view('paper-test')
        self.assertEqual((view.units,view.nav),(Decimal(200),Decimal(85)))
        self.assertFalse(view.ready)
        self.ledger.close()
        self.ledger=load_ledger().open(self.path)
        self.ledger._clock_us=lambda:self.now
        self.assertEqual(1,self.ledger.connection.execute('SELECT drawdown_latched FROM account_control').fetchone()[0])

    def test_repeating_units_repeated_flows_and_exact_view(self):
        from fractions import Fraction
        self.initialize('3')
        for i in range(30):
            self.flow('deposit-'+str(i),7)
            self.mark('refresh-'+str(i))
            self.flow('withdraw-'+str(i),-3)
            self.mark('withdraw-refresh-'+str(i))
        view=self.ledger.account_view('paper-test')
        self.assertTrue(view.ready)
        self.assertEqual(view.exact_nav.nav.fraction(),Fraction(3,100))
        self.assertEqual(view.exact_nav.units.fraction(),Fraction(25001,250))
        self.assertEqual(view.exact_nav.revision,view.revision)

    def test_buy_sell_native_fees_basis_and_reservation_conservation(self):
        self.initialize(extra=({'asset':'FEE','quantity':'2','basis_usd_u':20000000},))
        self.intent('buy')
        self.fill('buy1','buy','2','200','AAVE','0.1')
        row=self.ledger.connection.execute("SELECT * FROM balance_positions WHERE asset='AAVE'").fetchone()
        self.assertEqual(('1.9',190000000),(row['native_qty'],row['cost_basis_usd_u']))
        r=self.ledger.connection.execute("SELECT * FROM reservations WHERE intent_id='buy'").fetchone()
        self.assertEqual('8',r['remaining_qty'])
        self.assertGreater(r['loss_budget_usd_u'],0)
        self.intent('sell','sell','1.9')
        self.fill('sell1','sell','1.9','228','USD','1',side='sell')
        row=self.ledger.connection.execute("SELECT * FROM balance_positions WHERE asset='AAVE'").fetchone()
        self.assertEqual(('0',0),(row['native_qty'],row['cost_basis_usd_u']))
        self.assertEqual(0,self.ledger.connection.execute('SELECT sum(usd_delta_u) FROM journal_postings').fetchone()[0])
        from fractions import Fraction
        native={}
        for asset,qty in self.ledger.connection.execute('SELECT asset,native_delta FROM journal_postings WHERE asset IS NOT NULL'):
            native[asset]=native.get(asset,Fraction(0))+Fraction(Decimal(qty))
        self.assertTrue(all(v==0 for v in native.values()))

    def test_missing_or_stale_depth_unknown_holding_and_boundary_are_unready(self):
        self.initialize()
        self.intent('buy')
        self.fill('f','buy')
        self.mark('missing')
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.mark('shallow',books={'AAVE':{'at_us':self.now,'bids':[['100','0.5']]}})
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.mark('good',books={'AAVE':{'at_us':self.now,'bids':[['100','1']]}})
        self.assertTrue(self.ledger.account_view('paper-test').ready)
        self.now+=86400000001
        self.mark('missed-boundary',books={'AAVE':{'at_us':self.now,'bids':[['100','1']]}})
        self.assertFalse(self.ledger.account_view('paper-test').ready)

    def test_correction_replays_old_buy_basis_after_partial_sell(self):
        self.initialize()
        self.intent('buy')
        original=self.fill('buy-fill','buy','2','200','USD','0')
        self.intent('sell','sell')
        self.fill('sell-fill','sell','1','120','USD','0',side='sell')
        corrected=dict(original,quote_quantity='220',price='110')
        eid=self.evidence('correction','correction',{'fill_id':'buy-fill','previous_payload_hash':content_hash(original),'replacement':corrected})
        self.assertTrue(self.ledger.correct_fill(eid))
        self.assertFalse(self.ledger.correct_fill(eid))
        row=self.ledger.connection.execute("SELECT * FROM balance_positions WHERE asset='AAVE'").fetchone()
        self.assertEqual(('1',110000000),(row['native_qty'],row['cost_basis_usd_u']))
        pnl=self.ledger.connection.execute("SELECT sum(usd_delta_u) FROM journal_postings WHERE ledger_account='realized_profit'").fetchone()[0]
        self.assertEqual(-10000000,pnl)
        self.assertEqual(2,self.ledger.connection.execute('SELECT count(*) FROM fills').fetchone()[0])

    def test_economic_failure_rolls_back_and_prefix_excludes_future_mark(self):
        self.initialize()
        cutoff=self.now
        before=self.ledger._revision('paper-test')
        self.now+=1
        self.ledger.connection.execute("CREATE TRIGGER fail_flow BEFORE INSERT ON exact_flows BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(PersistenceUnavailable):self.flow('failed',10)
        self.assertEqual(0,self.ledger.connection.execute('SELECT count(*) FROM external_flows').fetchone()[0])
        self.assertEqual(before,self.ledger._revision('paper-test'))

    def test_local_evidence_cannot_claim_live_provenance_or_changed_identity(self):
        self.initialize()
        with self.assertRaises(ContractViolation):self.evidence('initial','initial',{'fake':True})
        with self.ledger.transaction() as c:c.execute("UPDATE accounts SET mode='live'")
        with self.assertRaises(ContractViolation):self.evidence('fake-live','mark',{})

    def test_closure_redeposit_and_reopen_keep_halts_and_exact_history(self):
        self.initialize()
        with self.ledger.transaction() as c:c.execute('UPDATE account_control SET drawdown_latched=1,crisis_halted=1,hard_killed=1')
        self.assertTrue(self.flow('close',-10000000000))
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.assertEqual(0,self.ledger.account_view('paper-test').liquidation_equity_usd_u)
        self.assertTrue(self.flow('reopen',5000000000))
        self.mark('after-reopen')
        self.assertEqual(Decimal(50),self.ledger.account_view('paper-test').units)
        self.ledger.close();self.ledger=load_ledger().open(self.path)
        self.assertEqual((1,1,1),tuple(self.ledger.connection.execute('SELECT drawdown_latched,crisis_halted,hard_killed FROM account_control').fetchone()))

    def test_filled_parent_allocates_one_reduction_while_drawdown_is_latched(self):
        from bot.autonomous.contracts import MarketRules, Side
        from bot.autonomous.risk import assess as real_assess
        self.initialize()
        self.intent('parent', quantity='1', price='100')
        self.fill('parent-fill', 'parent', '1', '100', 'USD', '0')
        self.ledger.connection.execute("UPDATE order_intents SET state='filled' WHERE intent_id='parent'")
        self.ledger.connection.commit()
        self.mark('held', books={'AAVE': {'at_us': self.now, 'quote_asset': 'USD', 'bids': [['100', '1']]}})
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET permit='entry',permit_epoch=1,lease_boot_id='test-boot',lease_deadline_mono_ns=100000000000,active_release_hash=?,version=version+1", (RELEASE_HASH,))
        limits = RiskLimits(armed=True, candidate_eligible=True, tokenizer_config_hash=HASH_A, calibration_config_hash=HASH_A)
        rules = MarketRules('paper', 'AAVE-USD', 'AAVE', 'USD', Decimal('.01'), Decimal('.01'), Decimal('.01'), Decimal('1'), Decimal('.01'), 2, HASH_A, self.now + 100_000_000, True, True, self.now, 10_000_000_000)
        self.ledger.configure_risk_checks(limits=limits, market=lambda _: rules, assess=real_assess, boot_id='test-boot', monotonic_ns=lambda: 1)
        self.ledger.connection.execute("UPDATE account_control SET drawdown_latched=1, daily_halted=1")
        self.ledger.connection.commit()
        sell = dataclasses.replace(order_proposal(), side=Side.SELL, reducing_only=True, decision=None,
            release_hash=RELEASE_HASH, control_epoch=1, deadline_mono_ns=100_000_000_000,
            base_quantity=Decimal('0.1'), action_slot='reduce:parent:1', action_id='reduce-parent')
        view = self.ledger.account_view('paper-test')
        self.assertTrue(view.ready, 'held account should be ready before the reduction')
        generation = self.ledger.connection.execute('SELECT generation FROM accounts').fetchone()[0]
        for forged in ('reduce:missing:1', 'reduce:parent:2', 'random-7'):
            with self.assertRaises(ContractViolation):
                self.ledger.reserve_intent(dataclasses.replace(sell, action_slot=forged, action_id='bad-' + forged),
                    view.revision, generation, None)
        prepared = self.ledger.reserve_intent(sell, view.revision, generation, None)
        self.assertEqual('reserved', prepared.state.value if hasattr(prepared.state, 'value') else prepared.state)
        self.assertEqual(0, self.ledger.connection.execute(
            "SELECT cash_usd_u FROM reservations WHERE intent_id=?", (prepared.intent_id,)).fetchone()[0])
        with self.assertRaises(ContractViolation):
            self.ledger.reserve_intent(dataclasses.replace(sell, action_slot='reduce:parent:2', action_id='second'),
                self.ledger.account_view('paper-test').revision, generation, None)
        dispatched = self.ledger.mark_dispatching(prepared.intent_id, generation)
        self.assertEqual(IntentState.DISPATCHING, dispatched.state)
        from bot.autonomous.contracts import BookPayload, ExecutableBook
        from bot.autonomous.execution import PaperVenue
        book = ExecutableBook('paper', 'AAVE-USD', self.now, BookPayload(
            ((Decimal('100'), Decimal('1')),), ((Decimal('101'), Decimal('1')),)), 'a' * 64)
        execution = PaperVenue(slippage_bps=Decimal(0), clock_us=lambda: self.now).submit(dispatched, book, rules)
        self.assertEqual(1, len(execution.fills))
        self.assertEqual('sell', execution.fills[0]['side'])
        before = self.balances()
        self.fill('reduce-fill', prepared.intent_id, '0.1', '10', 'USD', '0', side='sell')
        after = self.balances()
        self.assertEqual('0.9', after['AAVE'])
        self.assertEqual(Decimal(before['USD']) + Decimal('10'), Decimal(after['USD']))
        self.assertEqual('0', self.ledger.connection.execute(
            'SELECT remaining_qty FROM reservations WHERE intent_id=?', (prepared.intent_id,)).fetchone()[0])
        self.assertEqual('dispatching', self.ledger.connection.execute(
            'SELECT state FROM order_intents WHERE intent_id=?', (prepared.intent_id,)).fetchone()[0])
        result = execution.results[0]
        self.ledger.append_order_event(OrderEvent('evt-reduce-fill', prepared.intent_id, result.state, result,
            self.now, self.now, result.venue_event_key))
        self.assertEqual('filled', self.ledger.connection.execute(
            'SELECT state FROM order_intents WHERE intent_id=?', (prepared.intent_id,)).fetchone()[0])
        self.assertEqual((1, 1), tuple(self.ledger.connection.execute(
            'SELECT drawdown_latched, daily_halted FROM account_control').fetchone()))
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.mark('after-reduction', books={'AAVE': {'at_us': self.now, 'quote_asset': 'USD', 'bids': [['100', '0.9']]}})
        refreshed = self.ledger.account_view('paper-test')
        self.assertTrue(refreshed.ready)
        self.assertEqual((1, 1), tuple(self.ledger.connection.execute(
            'SELECT drawdown_latched, daily_halted FROM account_control').fetchone()))
        from bot.autonomous.contracts import ControlState, Mode, Permit
        buy = dataclasses.replace(order_proposal(), release_hash=RELEASE_HASH, control_epoch=1,
            deadline_mono_ns=100_000_000_000,
            decision=dataclasses.replace(order_proposal().decision, release_hash=RELEASE_HASH))
        control = ControlState(mode=Mode.PAPER, permit=Permit.ENTRY, drawdown_latched=True, daily_halted=True,
            control_epoch=1, boot_id='test-boot', deadman_deadline_mono_ns=100_000_000_000,
            active_release_hash=RELEASE_HASH, reconciliation_ready=True, armed=True)
        verdict = real_assess(buy, refreshed, control, limits, rules, self.now, now_mono_ns=1, current_boot_id='test-boot')
        self.assertFalse(verdict.allowed)
        self.assertIn('DRAWDOWN_LATCH', verdict.reason_codes)
        with self.ledger.transaction() as c:
            c.execute("INSERT INTO snapshots(snapshot_id,account_id,release_hash,candle_close_us,knowledge_cutoff_us,field_hash,state_json,lineage_hash,account_revision,valid,tokenizer_hash,token_count,recorded_at_us) VALUES (?,'paper-test',?,1000000,1000000,?,'{}',?,'{}',1,?,?,?)",
                (HASH_B, RELEASE_HASH, HASH_A, HASH_B, HASH_A, 10, self.now))
            c.execute("INSERT INTO decisions(decision_id,snapshot_id,request_id,deadline_us,model_id,status,recorded_at_us) VALUES ('decision-test',?,'request-test',?,'test','completed',?)",
                (HASH_B, self.now + 100_000_000, self.now))
            c.execute("INSERT INTO candidate_results(decision_id,candidate_id,status,veto_codes_json,recorded_at_us) VALUES ('decision-test',1,'eligible','[]',?)", (self.now,))
        current = self.ledger.account_view('paper-test')
        orders_before = self.ledger.connection.execute('SELECT count(*) FROM order_intents').fetchone()[0]
        with self.assertRaisesRegex(ContractViolation, 'entry inhibited') as refusal:
            self.ledger.reserve_intent(buy, current.revision, generation, buy.decision)
        orders_after = self.ledger.connection.execute('SELECT count(*) FROM order_intents').fetchone()[0]
        self.assertEqual(orders_before, orders_after)

    def test_marked_fifteen_percent_drawdown_blocks_the_buy_and_allows_the_reduction(self):
        from bot.autonomous.contracts import MarketRules, Side
        from bot.autonomous.risk import assess as real_assess
        self.initialize()
        self.intent('parent', quantity='50', price='100')
        self.fill('parent-fill', 'parent', '50', '5000', 'USD', '0')
        self.ledger.connection.execute("UPDATE order_intents SET state='filled' WHERE intent_id='parent'")
        self.ledger.connection.commit()
        self.mark('held', books={'AAVE': {'at_us': self.now, 'quote_asset': 'USD', 'bids': [['100', '50']]}})
        self.assertEqual((0, 0), tuple(self.ledger.connection.execute(
            'SELECT drawdown_latched, daily_halted FROM account_control').fetchone()))
        self.mark('breached', books={'AAVE': {'at_us': self.now, 'quote_asset': 'USD', 'bids': [['70', '50']]}})
        view = self.ledger.account_view('paper-test')
        flags = tuple(self.ledger.connection.execute(
            'SELECT drawdown_latched, daily_halted FROM account_control').fetchone())
        self.assertEqual(8_500_000_000, view.liquidation_equity_usd_u)
        self.assertEqual((1, 1), flags)
        self.assertTrue(view.ready)
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET permit='entry',permit_epoch=1,lease_boot_id='test-boot',lease_deadline_mono_ns=100000000000,active_release_hash=?,version=version+1", (RELEASE_HASH,))
            c.execute("INSERT INTO snapshots(snapshot_id,account_id,release_hash,candle_close_us,knowledge_cutoff_us,field_hash,state_json,lineage_hash,account_revision,valid,tokenizer_hash,token_count,recorded_at_us) VALUES (?,'paper-test',?,1000000,1000000,?,'{}',?,'{}',1,?,?,?)",
                (HASH_B, RELEASE_HASH, HASH_A, HASH_B, HASH_A, 10, self.now))
            c.execute("INSERT INTO decisions(decision_id,snapshot_id,request_id,deadline_us,model_id,status,recorded_at_us) VALUES ('decision-test',?,'request-test',?,'test','completed',?)",
                (HASH_B, self.now + 100_000_000, self.now))
            c.execute("INSERT INTO candidate_results(decision_id,candidate_id,status,veto_codes_json,recorded_at_us) VALUES ('decision-test',1,'eligible','[]',?)", (self.now,))
        limits = RiskLimits(armed=True, candidate_eligible=True, tokenizer_config_hash=HASH_A, calibration_config_hash=HASH_A)
        rules = MarketRules('paper', 'AAVE-USD', 'AAVE', 'USD', Decimal('.01'), Decimal('.01'), Decimal('.01'), Decimal('1'), Decimal('.01'), 2, HASH_A, self.now + 100_000_000, True, True, self.now, 10_000_000_000)
        self.ledger.configure_risk_checks(limits=limits, market=lambda _: rules, assess=real_assess, boot_id='test-boot', monotonic_ns=lambda: 1)
        generation = self.ledger.connection.execute('SELECT generation FROM accounts').fetchone()[0]
        current = self.ledger.account_view('paper-test')
        buy = dataclasses.replace(order_proposal(), release_hash=RELEASE_HASH, control_epoch=1, deadline_mono_ns=100_000_000_000,
            decision=dataclasses.replace(order_proposal().decision, release_hash=RELEASE_HASH))
        with self.assertRaisesRegex(ContractViolation, 'entry inhibited') as refusal:
            self.ledger.reserve_intent(buy, current.revision, generation, buy.decision)
        sell = dataclasses.replace(buy, side=Side.SELL, reducing_only=True, decision=None, base_quantity=Decimal('0.1'),
            limit_price=Decimal('70'), action_slot='reduce:parent:1', action_id='reduce-parent')
        prepared = self.ledger.reserve_intent(sell, self.ledger.account_view('paper-test').revision, generation, None)
        self.assertEqual(IntentState.RESERVED, prepared.state)
        dispatched = self.ledger.mark_dispatching(prepared.intent_id, generation)
        from bot.autonomous.contracts import BookPayload, ExecutableBook
        from bot.autonomous.execution import PaperVenue
        book = ExecutableBook('paper', 'AAVE-USD', self.now, BookPayload(
            ((Decimal('70'), Decimal('50')),), ((Decimal('71'), Decimal('50')),)), 'b' * 64)
        execution = PaperVenue(slippage_bps=Decimal(0), clock_us=lambda: self.now).submit(dispatched, book, rules)
        self.assertEqual('70', execution.fills[0]['price'])
        before = self.balances()
        self.fill('reduce-fill', prepared.intent_id, '0.1', '7', 'USD', '0', side='sell')
        after = self.balances()
        self.assertEqual('49.9', after['AAVE'])
        self.assertEqual(Decimal(before['USD']) + Decimal('7'), Decimal(after['USD']))
        flags_after = tuple(self.ledger.connection.execute(
            'SELECT drawdown_latched, daily_halted FROM account_control').fetchone())
        self.assertEqual((1, 1), flags_after)
        result = execution.results[0]
        self.ledger.append_order_event(OrderEvent('evt-breach-fill', prepared.intent_id, result.state, result,
            self.now, self.now, result.venue_event_key))
        order_state = self.ledger.connection.execute(
            'SELECT state FROM order_intents WHERE intent_id=?', (prepared.intent_id,)).fetchone()[0]
        self.assertEqual('filled', order_state)
        flags_closed = tuple(self.ledger.connection.execute(
            'SELECT drawdown_latched, daily_halted FROM account_control').fetchone())
        self.assertEqual((1, 1), flags_closed)
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.mark('after-breach-exit', books={'AAVE': {'at_us': self.now, 'quote_asset': 'USD', 'bids': [['70', '49.9']]}})
        remarked = self.ledger.account_view('paper-test')
        self.assertTrue(remarked.ready)
        self.assertEqual(8_500_000_000, remarked.liquidation_equity_usd_u)
        flags_remarked = tuple(self.ledger.connection.execute(
            'SELECT drawdown_latched, daily_halted FROM account_control').fetchone())
        self.assertEqual((1, 1), flags_remarked)
        with self.assertRaisesRegex(ContractViolation, 'entry inhibited') as remarked_buy:
            self.ledger.reserve_intent(buy, remarked.revision, generation, buy.decision)
        self.mark('recovered', books={'AAVE': {'at_us': self.now, 'quote_asset': 'USD', 'bids': [['100', '49.9']]}})
        recovered = self.ledger.account_view('paper-test')
        flags_recovered = tuple(self.ledger.connection.execute(
            'SELECT drawdown_latched, daily_halted FROM account_control').fetchone())
        self.assertEqual(9_997_000_000, recovered.liquidation_equity_usd_u)
        self.assertEqual((1, 1), flags_recovered)
        self.assertTrue(recovered.ready)
        with self.assertRaisesRegex(ContractViolation, 'entry inhibited') as recovered_buy:
            self.ledger.reserve_intent(buy, recovered.revision, generation, buy.decision)
        self.now += 86_400_000_000
        self.mark('next-midnight', books={'AAVE': {'at_us': self.now, 'quote_asset': 'USD', 'bids': [['100', '49.9']]}})
        rolled = self.ledger.account_view('paper-test')
        flags_rolled = tuple(self.ledger.connection.execute(
            'SELECT drawdown_latched, daily_halted FROM account_control').fetchone())
        self.assertEqual((1, 0), flags_rolled)
        self.assertTrue(rolled.ready)
        rolled_rules = MarketRules('paper', 'AAVE-USD', 'AAVE', 'USD', Decimal('.01'), Decimal('.01'), Decimal('.01'), Decimal('1'), Decimal('.01'), 2, HASH_A, self.now + 100_000_000, True, True, self.now, 10_000_000_000)
        self.ledger.configure_risk_checks(limits=limits, market=lambda _: rolled_rules, assess=real_assess, boot_id='test-boot', monotonic_ns=lambda: 1)
        next_release = content_hash({"release": "next-day"})
        next_snapshot = content_hash({"snapshot": "next-day"})
        with self.ledger.transaction() as c:
            c.execute("INSERT INTO release_bundles(release_hash,manifest_json,expires_at_us,recorded_at_us) VALUES (?, '{}', ?, ?)",
                (next_release, self.now + 100_000_000, self.now))
            c.execute("UPDATE account_control SET active_release_hash=?,version=version+1 WHERE account_id='paper-test'", (next_release,))
            c.execute("INSERT INTO snapshots(snapshot_id,account_id,release_hash,candle_close_us,knowledge_cutoff_us,field_hash,state_json,lineage_hash,account_revision,valid,tokenizer_hash,token_count,recorded_at_us) VALUES (?,'paper-test',?,1000000,1000000,?,'{}',?,'{}',1,?,?,?)",
                (next_snapshot, next_release, HASH_A, HASH_B, HASH_A, 10, self.now))
            c.execute("INSERT INTO decisions(decision_id,snapshot_id,request_id,deadline_us,model_id,status,recorded_at_us) VALUES ('decision-next',?,'request-next',?,'test','completed',?)",
                (next_snapshot, self.now + 100_000_000, self.now))
            c.execute("INSERT INTO candidate_results(decision_id,candidate_id,status,veto_codes_json,recorded_at_us) VALUES ('decision-next',1,'eligible','[]',?)", (self.now,))
        rolled_buy_proposal = dataclasses.replace(buy, release_hash=next_release,
            decision=dataclasses.replace(buy.decision, decision_id='decision-next', request_id='request-next',
                snapshot_hash=next_snapshot, release_hash=next_release))
        with self.assertRaisesRegex(ContractViolation, 'entry inhibited') as rolled_buy:
            self.ledger.reserve_intent(rolled_buy_proposal, self.ledger.account_view('paper-test').revision, generation, rolled_buy_proposal.decision)
        second = dataclasses.replace(sell, release_hash=next_release, limit_price=Decimal('100'),
            action_slot='reduce:parent:2', action_id='reduce-next-day')
        second_prepared = self.ledger.reserve_intent(second, self.ledger.account_view('paper-test').revision, generation, None)
        self.assertEqual(IntentState.RESERVED, second_prepared.state)
        flags_exit = tuple(self.ledger.connection.execute(
            'SELECT drawdown_latched, daily_halted FROM account_control').fetchone())
        self.assertEqual((1, 0), flags_exit)
        dispatched_next = self.ledger.mark_dispatching(second_prepared.intent_id, generation)
        next_book = ExecutableBook('paper', 'AAVE-USD', self.now, BookPayload(
            ((Decimal('100'), Decimal('49.9')),), ((Decimal('101'), Decimal('49.9')),)), 'c' * 64)
        next_execution = PaperVenue(slippage_bps=Decimal(0), clock_us=lambda: self.now).submit(
            dispatched_next, next_book, rolled_rules)
        self.assertEqual('100', next_execution.fills[0]['price'])
        self.fill('reduce-next', second_prepared.intent_id, '0.1', '10', 'USD', '0', side='sell')
        next_result = next_execution.results[0]
        self.ledger.append_order_event(OrderEvent('evt-next-day', second_prepared.intent_id, next_result.state, next_result,
            self.now, self.now, next_result.venue_event_key))
        next_state = self.ledger.connection.execute(
            'SELECT state FROM order_intents WHERE intent_id=?', (second_prepared.intent_id,)).fetchone()[0]
        self.assertEqual('filled', next_state)
        self.assertEqual('49.8', self.balances()['AAVE'])
        flags_filled = tuple(self.ledger.connection.execute(
            'SELECT drawdown_latched, daily_halted FROM account_control').fetchone())
        self.assertEqual((1, 0), flags_filled)
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.mark('after-next-day', books={'AAVE': {'at_us': self.now, 'quote_asset': 'USD', 'bids': [['100', '49.8']]}})
        after_next = self.ledger.account_view('paper-test')
        flags_after_next = tuple(self.ledger.connection.execute(
            'SELECT drawdown_latched, daily_halted FROM account_control').fetchone())
        self.assertTrue(after_next.ready)
        self.assertEqual(9_997_000_000, after_next.liquidation_equity_usd_u)
        self.assertEqual((1, 0), flags_after_next)
        with self.assertRaisesRegex(ContractViolation, 'entry inhibited') as after_next_buy:
            self.ledger.reserve_intent(rolled_buy_proposal, after_next.revision, generation, rolled_buy_proposal.decision)

    def test_rollover_mark_keeps_drawdown_latch_and_clears_daily_halt(self):
        self.initialize()
        with self.ledger.transaction() as c:
            c.execute("UPDATE account_control SET drawdown_latched=1, daily_halted=1 WHERE account_id='paper-test'")
        self.now += 86_400_000_000
        self.mark('next-midnight')
        row = self.ledger.connection.execute('SELECT drawdown_latched, daily_halted FROM account_control').fetchone()
        self.assertEqual((1, 0), tuple(row))

    def test_transfer_cost_triggers_daily_halt_atomically_and_utc_rollover(self):
        self.initialize()
        self.assertTrue(self.flow('cost',1000000,200020000))
        self.assertEqual(1,self.ledger.connection.execute('SELECT daily_halted FROM account_control').fetchone()[0])
        self.now+=86400000000
        self.mark('next-midnight')
        self.assertEqual(0,self.ledger.connection.execute('SELECT daily_halted FROM account_control').fetchone()[0])
        self.assertTrue(self.ledger.account_view('paper-test').ready)

    def test_zero_equity_still_persists_breach_while_unarmed(self):
        self.initialize()
        self.mark('zero-equity',liabilities_usd_u=10000000000)
        self.assertEqual((1,1),tuple(self.ledger.connection.execute('SELECT drawdown_latched,daily_halted FROM account_control').fetchone()))
        self.assertFalse(self.ledger.account_view('paper-test').ready)

    def test_third_asset_fee_fx_and_pending_unknown_conversion(self):
        self.initialize(extra=({'asset':'FEE','quantity':'2','basis_usd_u':20000000},))
        fx=self.evidence('fx','fx',{'legs':[{'from_asset':'FEE','to_asset':'USD','to_units_per_from_unit':'12'}]})
        self.intent('buy')
        self.fill('fee1','buy','1','100','FEE','0.5',fee_fx_id=fx)
        self.assertEqual('1.5',self.balances()['FEE'])
        self.assertEqual(-1000000,self.ledger.connection.execute("SELECT sum(usd_delta_u) FROM journal_postings WHERE ledger_account='realized_profit'").fetchone()[0])
        self.fill('fee2','buy','1','100','FEE','0.5')
        self.assertEqual('2',self.balances()['AAVE'])
        self.assertEqual('1',self.balances()['FEE'])
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.assertGreater(self.ledger.connection.execute('SELECT cash_usd_u FROM reservations').fetchone()[0],0)

    def test_fill_known_cash_fee_can_latch_before_another_reconciliation(self):
        self.initialize()
        self.intent('buy')
        self.mark('pre-trade-depth',books={'AAVE':{'at_us':self.now,'bids':[['100','10']]}})
        self.fill('fee-loss','buy','1','100','USD','1600')
        self.assertEqual(1,self.ledger.connection.execute('SELECT drawdown_latched FROM account_control').fetchone()[0])
        self.assertFalse(self.ledger.account_view('paper-test').ready)

    def test_exact_accounting_events_are_cutoff_prefix_bound(self):
        self.initialize()
        cutoff=self.now;reconcile=self.ledger.account_view('paper-test').reconcile_id
        self.now+=10
        self.flow('later-flow',1000000)
        archive=self.ledger.export_session(SessionCutoff('paper-test',cutoff,reconcile))
        from bot.autonomous.contracts import strict_json
        raw=self.ledger.connection.execute('SELECT export_json FROM review_sessions WHERE session_id=?',(archive.session_id,)).fetchone()[0]
        payload=strict_json(raw)
        self.assertEqual([],payload['exact_flows'])
        self.assertEqual(['opening'],[e['evidence_id'] for e in payload['accounting_evidence'] if e['kind']=='mark'])
        self.assertEqual(1,len(payload['exact_marks']))

    def test_startup_rejects_mutable_unit_projection_tampering(self):
        self.initialize()
        self.ledger.connection.execute("UPDATE accounting_state SET units_json='{\"denominator\":\"1\",\"numerator\":\"999\"}'")
        self.ledger.close()
        with self.assertRaises(PersistenceUnavailable):load_ledger().open(self.path)

    def test_startup_rejects_day_boundary_projection_tampering(self):
        self.initialize()
        self.ledger.connection.execute('UPDATE accounting_state SET day_start_us=day_start_us+86400000000')
        self.ledger.close()
        with self.assertRaises(PersistenceUnavailable):load_ledger().open(self.path)

    def test_submicro_basis_residual_and_duplicate_fill_evidence(self):
        self.initialize()
        self.intent('buy')
        payload=self.fill('tiny-buy','buy','3','0.000001','USD','0',price='0.00000033333333333333333333333333333333')
        self.assertFalse(self.ledger.record_fill(self.evidence('duplicate','fill',payload)))
        self.intent('sell','sell','3')
        self.fill('tiny-sell','sell','1','0.000001','USD','0',side='sell')
        self.assertEqual(1,self.ledger.connection.execute("SELECT cost_basis_usd_u FROM balance_positions WHERE asset='AAVE'").fetchone()[0])
        self.fill('tiny-final','sell','2','0.000002','USD','0',side='sell')
        self.assertEqual(0,self.ledger.connection.execute("SELECT cost_basis_usd_u FROM balance_positions WHERE asset='AAVE'").fetchone()[0])
        self.assertGreater(self.ledger.connection.execute("SELECT count(*) FROM accounting_rounding WHERE exact_residual_json!='{\"denominator\":\"1\",\"numerator\":\"0\"}'").fetchone()[0],0)

    def test_fill_after_flow_still_latches_loss_in_economic_transaction(self):
        self.initialize()
        self.mark('preflow-depth',books={'AAVE':{'at_us':self.now,'bids':[['100','10']]}})
        self.flow('capital',1000000)
        self.intent('buy')
        self.fill('loss-after-flow','buy','1','100','USD','1600')
        self.assertEqual(1,self.ledger.connection.execute('SELECT drawdown_latched FROM account_control').fetchone()[0])

    def test_unknown_fill_semantics_records_incident_and_invalidates_ready_view(self):
        self.initialize()
        self.intent('buy')
        self.mark('ready-with-order')
        payload={'fill_id':'unknown','intent_id':'buy','venue':'paper','trade_id':'unknown','order_id':'buy',
            'instrument':'AAVE-USD','side':'buy','base_quantity':'1','quote_quantity':'100','price':'100',
            'fee_asset':'USD','fee_quantity':'0','base_semantics':'unknown','fee_final':True}
        self.assertFalse(self.ledger.record_fill(self.evidence('unknown','fill',payload)))
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.assertEqual(0,self.ledger.connection.execute('SELECT count(*) FROM fills').fetchone()[0])
        self.assertGreater(self.ledger.connection.execute('SELECT count(*) FROM accounting_findings').fetchone()[0],0)

    def test_missing_fx_liquidation_invalidates_previous_ready_mark(self):
        self.initialize(extra=({'asset':'USDC','quantity':'2','basis_usd_u':2000000},))
        self.mark('usd-book',books={'USDC':{'at_us':self.now,'bids':[['0.99','2']]}})
        self.assertTrue(self.ledger.account_view('paper-test').ready)
        self.mark('missing-fx',books={'USDC':{'at_us':self.now,'quote_asset':'EUR','fx_id':'absent','bids':[['1','2']]}})
        self.assertFalse(self.ledger.account_view('paper-test').ready)

    def test_startup_rejects_highwater_and_native_projection_tampering(self):
        self.initialize()
        with self.ledger.transaction() as c:
            c.execute("UPDATE accounting_state SET high_water_json='{\"denominator\":\"1\",\"numerator\":\"1\"}'")
        self.ledger.close()
        with self.assertRaises(PersistenceUnavailable):load_ledger().open(self.path)

    def test_repeated_capital_flow_keeps_depth_for_following_loss_event(self):
        self.initialize()
        self.intent('buy')
        self.fill('first-buy','buy','1','100','USD','0')
        self.mark('complete-depth',books={'AAVE':{'at_us':self.now,'bids':[['100','10']]}})
        self.flow('deposit-with-holding',1000000)
        self.fill('fee-event','buy','1','100','USD','1600')
        self.assertEqual(1,self.ledger.connection.execute('SELECT drawdown_latched FROM account_control').fetchone()[0])

    def test_non_usd_external_capital_requires_fx_and_preserves_nav(self):
        self.initialize(extra=({'asset':'USDC','quantity':'2','basis_usd_u':2000000},))
        self.mark('coin-depth',books={'USDC':{'at_us':self.now,'bids':[['0.99','100']]}})
        before=self.ledger.account_view('paper-test')
        fx=self.evidence('transfer-fx','fx',{'legs':[{'from_asset':'USDC','to_asset':'USD','to_units_per_from_unit':'0.99'}]})
        pre=self.ledger.connection.execute('SELECT mark_id FROM equity_marks ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        payload={'transfer_key':'usdc-deposit','asset':'USDC','quantity':'10','usd_value_u':9900000,'fee_usd_u':0,
            'fx_id':fx,'preflow_mark_id':pre}
        self.assertTrue(self.ledger.record_flow(self.evidence('usdc-deposit','flow',payload)))
        self.assertEqual('12',self.balances()['USDC'])
        self.mark('post-capital',books={'USDC':{'at_us':self.now,'bids':[['0.99','100']]}})
        after=self.ledger.account_view('paper-test')
        self.assertEqual(before.exact_nav.nav,after.exact_nav.nav)
        self.assertTrue(after.ready)

    def test_exact_flow_after_units_are_checked_on_restart(self):
        self.initialize()
        self.flow('capital',1000000)
        c=self.ledger.connection
        trigger=c.execute("SELECT sql FROM sqlite_master WHERE name='exact_flows_no_update'").fetchone()[0]
        c.execute('DROP TRIGGER exact_flows_no_update')
        bad='{\"denominator\":\"1\",\"numerator\":\"999\"}'
        c.execute('UPDATE exact_flows SET units_after_json=?',(bad,))
        c.execute('UPDATE accounting_state SET units_json=?',(bad,))
        c.execute(trigger)
        self.ledger.close()
        with self.assertRaises(PersistenceUnavailable):load_ledger().open(self.path)

    def test_flow_with_missing_fx_keeps_evidence_and_inhibits_entries(self):
        self.initialize()
        pre=self.ledger.account_view('paper-test').exact_nav.mark_id
        p={'transfer_key':'unvalued','asset':'USDC','quantity':'10','usd_value_u':10000000,
            'fee_usd_u':0,'preflow_mark_id':pre}
        self.assertFalse(self.ledger.record_flow(self.evidence('unvalued','flow',p)))
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.assertEqual(0,self.ledger.connection.execute('SELECT count(*) FROM external_flows').fetchone()[0])
        self.assertEqual(1,self.ledger.connection.execute("SELECT count(*) FROM accounting_evidence WHERE evidence_id='unvalued'").fetchone()[0])

    def test_submicro_usd_transfer_cannot_silently_round_native_cash(self):
        self.initialize()
        p={'transfer_key':'submicro','asset':'USD','quantity':'0.0000011','usd_value_u':1,
            'fee_usd_u':0,'preflow_mark_id':self.ledger.account_view('paper-test').exact_nav.mark_id}
        self.assertFalse(self.ledger.record_flow(self.evidence('submicro','flow',p)))
        self.assertEqual('10000',self.balances()['USD'])
        self.assertFalse(self.ledger.account_view('paper-test').ready)

    def test_buy_net_base_fee_and_sell_extra_base_fee_apply_once(self):
        self.initialize()
        self.intent('buy',quantity='10')
        self.fill('net-buy','buy','9.9','1000','AAVE','0.1',base_semantics='buy_net',price='100')
        self.assertEqual('9.9',self.balances()['AAVE'])
        self.assertEqual('9000',self.balances()['USD'])
        self.assertEqual('0',self.ledger.connection.execute("SELECT remaining_qty FROM reservations WHERE intent_id='buy'").fetchone()[0])
        self.intent('sell','sell','9.8')
        self.fill('base-fee-sell','sell','9.8','1078','AAVE','0.1',side='sell')
        self.assertEqual('0',self.balances()['AAVE'])
        self.assertEqual('10078',self.balances()['USD'])
        self.assertEqual(21000000,self.ledger.connection.execute("SELECT sum(usd_delta_u) FROM journal_postings WHERE ledger_account='fee_expense'").fetchone()[0])

    def test_late_fill_changed_retry_and_stale_correction_preserve_history(self):
        self.initialize()
        self.intent('buy')
        original=self.fill('original','buy','1','100','USD','0')
        with self.ledger.transaction() as c:c.execute("UPDATE order_intents SET state='canceled',version=version+1 WHERE intent_id='buy'")
        self.fill('late','buy','1','100','USD','0')
        self.assertEqual('2',self.balances()['AAVE'])
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.assertFalse(self.ledger.record_fill(self.evidence('changed-fill','fill',dict(original,fee_quantity='1'))))
        self.assertEqual(2,self.ledger.connection.execute('SELECT count(*) FROM fills').fetchone()[0])
        correction={'fill_id':'original','previous_payload_hash':content_hash(original),'replacement':dict(original,fee_quantity='1')}
        self.assertTrue(self.ledger.correct_fill(self.evidence('correct','correction',correction)))
        with self.assertRaises(ContractViolation):self.ledger.correct_fill(self.evidence('stale','correction',correction))
        self.assertEqual(1,self.ledger.connection.execute('SELECT count(*) FROM fill_corrections').fetchone()[0])

    def test_terminal_fill_replay_does_not_change_reservation_on_unrelated_deposit(self):
        self.initialize()
        self.intent('completed',quantity='1')
        self.fill('completed-fill','completed','1','100','USD','0')
        result=VenueOrderResult(OrderReference('paper-test','completed','completed','venue-completed'),
            IntentState.FILLED,Decimal('1'),Decimal('0'),self.now)
        self.ledger.append_order_event(OrderEvent('completed-event','completed',IntentState.FILLED,result,self.now))
        books={'AAVE':{'at_us':self.now,'bids':[['100','10']]}}
        self.mark('completed-reconciliation',books=books)
        self.assertTrue(self.ledger.account_view('paper-test').ready)
        before=tuple(self.ledger.connection.execute("SELECT * FROM reservations WHERE intent_id='completed'").fetchone())
        self.assertTrue(self.flow('unrelated-capital',1000000))
        self.assertEqual([],list(self.ledger.connection.execute(
            "SELECT * FROM accounting_findings WHERE reason='late fill or cumulative order disagreement'")))
        self.assertEqual(before,tuple(self.ledger.connection.execute("SELECT * FROM reservations WHERE intent_id='completed'").fetchone()))
        self.mark('post-deposit-reconciliation',books=books)
        self.assertTrue(self.ledger.account_view('paper-test').ready)

    def test_terminal_fill_replay_does_not_change_reservation_on_another_intent_fill(self):
        self.initialize()
        self.intent('completed',quantity='1')
        self.fill('completed-fill','completed','1','100','USD','0')
        result=VenueOrderResult(OrderReference('paper-test','completed','completed','venue-completed'),
            IntentState.FILLED,Decimal('1'),Decimal('0'),self.now)
        self.ledger.append_order_event(OrderEvent('completed-event','completed',IntentState.FILLED,result,self.now))
        self.mark('completed-reconciliation',books={'AAVE':{'at_us':self.now,'bids':[['100','10']]}})
        self.assertTrue(self.ledger.account_view('paper-test').ready)
        before=tuple(self.ledger.connection.execute("SELECT * FROM reservations WHERE intent_id='completed'").fetchone())
        self.intent('other',quantity='1')
        self.fill('other-fill','other','1','100','USD','0')
        self.assertEqual([],list(self.ledger.connection.execute(
            "SELECT * FROM accounting_findings WHERE reason='late fill or cumulative order disagreement'")))
        self.assertEqual(before,tuple(self.ledger.connection.execute("SELECT * FROM reservations WHERE intent_id='completed'").fetchone()))
        self.mark('post-fill-reconciliation',books={'AAVE':{'at_us':self.now,'bids':[['100','10']]}})
        self.assertTrue(self.ledger.account_view('paper-test').ready)

    def test_new_terminal_fill_and_open_cumulative_overfill_journal_once(self):
        for terminal in (False,True):
            with self.subTest(terminal=terminal):
                if terminal:self.tearDown();self.setUp()
                self.initialize()
                self.intent('buy',quantity='1')
                self.fill('first','buy','1','100','USD','0')
                if terminal:
                    result=VenueOrderResult(OrderReference('paper-test','buy','buy','venue-buy'),
                        IntentState.FILLED,Decimal('1'),Decimal('0'),self.now)
                    self.ledger.append_order_event(OrderEvent('terminal','buy',IntentState.FILLED,result,self.now))
                payload=self.fill('new-fill','buy','1','100','USD','0')
                count=self.ledger.connection.execute('SELECT count(*) FROM journal_transactions').fetchone()[0]
                self.assertFalse(self.ledger.record_fill(self.evidence('new-fill','fill',payload)))
                self.assertEqual(count,self.ledger.connection.execute('SELECT count(*) FROM journal_transactions').fetchone()[0])
                self.assertEqual(1,self.ledger.connection.execute(
                    "SELECT count(*) FROM accounting_findings WHERE reason='late fill or cumulative order disagreement'").fetchone()[0])
                self.mark('current',books={'AAVE':{'at_us':self.now,'bids':[['100','10']]}})
                self.assertFalse(self.ledger.account_view('paper-test').ready)
                self.assertEqual('2',self.balances()['AAVE'])

    def test_wide_native_correction_preserves_exact_posting_targets_and_reopens(self):
        from fractions import Fraction
        self.initialize()
        self.intent('buy',quantity='10')
        original=self.fill('wide-original','buy','10','0.000001','USD','0',price='0.0000001')
        tiny='0.00000000000000000000000000000000000001'
        replacement=dict(original,base_quantity=tiny,price='100000000000000000000000000000000')
        correction={'fill_id':'wide-original','previous_payload_hash':content_hash(original),'replacement':replacement}
        evidence=self.evidence('wide-correction','correction',correction)
        self.assertTrue(self.ledger.correct_fill(evidence))
        self.assertFalse(self.ledger.correct_fill(evidence))
        self.assertEqual(tiny,self.balances()['AAVE'])
        _,_,target,_,_,_=self.ledger._replay_economics('paper-test')
        actual={}
        for row in self.ledger.connection.execute(
                'SELECT ledger_account,asset,native_delta FROM journal_postings WHERE asset IS NOT NULL'):
            key=row['ledger_account'],row['asset']
            actual[key]=actual.get(key,Fraction(0))+Fraction(Decimal(row['native_delta']))
        self.assertEqual({k:v for k,v in target.items() if v},{k:v for k,v in actual.items() if v})
        self.assertEqual(Fraction(Decimal(tiny)),actual['inventory:AAVE','AAVE'])
        before=self.balances()
        self.ledger.close();self.ledger=load_ledger().open(self.path)
        self.ledger._clock_us=lambda:self.now
        self.assertEqual(before,self.balances())
        self.assertFalse(self.ledger.account_view('paper-test').ready)

    def test_unknown_correction_fee_retains_evidence_and_inhibits_without_economics(self):
        self.initialize()
        self.intent('buy',quantity='1')
        original=self.fill('original','buy','1','100','USD','0')
        books={'AAVE':{'at_us':self.now,'bids':[['100','1']]}}
        self.mark('current',books=books)
        self.assertTrue(self.ledger.account_view('paper-test').ready)
        evidence=self.evidence('unknown-correction','correction',{'fill_id':'original',
            'previous_payload_hash':content_hash(original),
            'replacement':dict(original,fee_asset='MISSING',fee_quantity='1')})
        before=self.balances()
        count=self.ledger.connection.execute('SELECT count(*) FROM journal_transactions').fetchone()[0]
        try:admitted=self.ledger.correct_fill(evidence)
        except ContractViolation as exc:self.fail('unsupported correction must retain an inhibition: '+str(exc))
        self.assertFalse(admitted)
        self.assertFalse(self.ledger.correct_fill(evidence))
        self.assertEqual(before,self.balances())
        self.assertEqual(count,self.ledger.connection.execute('SELECT count(*) FROM journal_transactions').fetchone()[0])
        self.assertEqual(0,self.ledger.connection.execute('SELECT count(*) FROM fill_corrections').fetchone()[0])
        self.assertEqual(1,self.ledger.connection.execute('SELECT count(*) FROM accounting_evidence WHERE evidence_id=?',(evidence,)).fetchone()[0])
        self.assertEqual(1,self.ledger.connection.execute('SELECT count(*) FROM accounting_findings WHERE evidence_id=?',(evidence,)).fetchone()[0])
        self.mark('after-rejected-correction',books=books)
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.ledger.close();self.ledger=load_ledger().open(self.path)
        self.ledger._clock_us=lambda:self.now
        self.assertEqual(before,self.balances())
        self.assertFalse(self.ledger.account_view('paper-test').ready)

    def test_correction_serialization_failure_rolls_back_before_retaining_incident(self):
        from unittest.mock import patch
        self.initialize()
        self.intent('buy',quantity='1')
        original=self.fill('original','buy','1','100','USD','0')
        self.mark('current',books={'AAVE':{'at_us':self.now,'bids':[['100','1']]}})
        self.assertTrue(self.ledger.account_view('paper-test').ready)
        evidence=self.evidence('unsupported-correction','correction',{'fill_id':'original',
            'previous_payload_hash':content_hash(original),
            'replacement':dict(original,fee_quantity='1')})
        tables=('journal_transactions','journal_postings','fill_corrections','accounting_operations','balance_positions')
        before={table:[tuple(row) for row in self.ledger.connection.execute('SELECT * FROM '+table)] for table in tables}
        with patch('bot.autonomous.ledger._native_posting_quantities',side_effect=ContractViolation('native posting scale exceeded')):
            try:admitted=self.ledger.correct_fill(evidence)
            except ContractViolation as exc:self.fail('unsupported serialization must retain an inhibition: '+str(exc))
            self.assertFalse(admitted)
        after={table:[tuple(row) for row in self.ledger.connection.execute('SELECT * FROM '+table)] for table in tables}
        self.assertEqual(before,after)
        self.assertEqual(1,self.ledger.connection.execute('SELECT count(*) FROM accounting_evidence WHERE evidence_id=?',(evidence,)).fetchone()[0])
        self.assertEqual(1,self.ledger.connection.execute('SELECT unresolved FROM accounting_state').fetchone()[0])
        self.assertFalse(self.ledger.account_view('paper-test').ready)

    def test_trailing_zero_flow_is_canonical_idempotent_and_restartable(self):
        from bot.autonomous.contracts import strict_json
        self.initialize()
        payload={'transfer_key':'formatted-transfer','asset':'USD','quantity':'1.0',
            'usd_value_u':1000000,'fee_usd_u':0,
            'preflow_mark_id':self.ledger.account_view('paper-test').exact_nav.mark_id}
        evidence=self.evidence('formatted-transfer','flow',payload)
        self.assertTrue(self.ledger.record_flow(evidence))
        self.assertEqual('1',self.ledger.connection.execute('SELECT native_qty FROM external_flows').fetchone()[0])
        self.assertFalse(self.ledger.record_flow(evidence))
        self.assertFalse(self.ledger.record_flow(self.evidence('transfer-retry','flow',payload)))
        retained=self.ledger.connection.execute('SELECT payload_json,payload_hash FROM accounting_evidence WHERE evidence_id=?',(evidence,)).fetchone()
        self.assertEqual(payload,strict_json(retained['payload_json']))
        self.assertEqual(content_hash(payload),retained['payload_hash'])
        self.assertEqual(1,self.ledger.connection.execute('SELECT count(*) FROM external_flows').fetchone()[0])
        self.mark('canonical-flow-current')
        before=self.balances();authority=self.ledger.account_view('paper-test').exact_nav
        self.ledger.close();self.ledger=load_ledger().open(self.path)
        self.ledger._clock_us=lambda:self.now
        self.assertEqual(before,self.balances())
        self.assertEqual(authority.units,self.ledger.account_view('paper-test').exact_nav.units)
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.mark('canonical-flow-restarted')
        self.assertTrue(self.ledger.account_view('paper-test').ready)

    def test_trailing_zero_fill_fields_are_canonical_idempotent_and_restartable(self):
        from bot.autonomous.contracts import strict_json
        self.initialize()
        self.intent('buy',quantity='1')
        payload=self.fill('formatted-fill','buy','1.0','100.00','USD','0.0100',price='100.000')
        row=self.ledger.connection.execute('SELECT base_qty,quote_qty,price,fee_qty,payload_hash FROM fills').fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(('1','100','100','0.01'),tuple(row)[:4])
        self.assertEqual(content_hash(payload),row['payload_hash'])
        self.assertFalse(self.ledger.record_fill(self.evidence('formatted-fill','fill',payload)))
        self.assertFalse(self.ledger.record_fill(self.evidence('fill-retry','fill',payload)))
        retained=self.ledger.connection.execute("SELECT payload_json FROM accounting_evidence WHERE evidence_id='formatted-fill'").fetchone()[0]
        self.assertEqual(payload,strict_json(retained))
        self.assertEqual(1,self.ledger.connection.execute('SELECT count(*) FROM fills').fetchone()[0])
        self.mark('canonical-fill-current',books={'AAVE':{'at_us':self.now,'bids':[['100','1']]}})
        before=self.balances();authority=self.ledger.account_view('paper-test').exact_nav
        self.ledger.close();self.ledger=load_ledger().open(self.path)
        self.ledger._clock_us=lambda:self.now
        self.assertEqual(before,self.balances())
        self.assertEqual(authority.nav,self.ledger.account_view('paper-test').exact_nav.nav)
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.mark('canonical-fill-restarted',books={'AAVE':{'at_us':self.now,'bids':[['100','1']]}})
        self.assertTrue(self.ledger.account_view('paper-test').ready)

    def test_stale_depth_heartbeat_and_unexplained_holdings_do_not_refresh_readiness(self):
        self.initialize(extra=({'asset':'OTHER','quantity':'1','basis_usd_u':1000000},))
        self.mark('complete-other',books={'OTHER':{'at_us':self.now,'bids':[['1','1']]}})
        self.assertTrue(self.ledger.account_view('paper-test').ready)
        self.mark('stale-other',books={'OTHER':{'at_us':self.now-2000001,'bids':[['1','1']]}})
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.mark('stale-private',books={'OTHER':{'at_us':self.now,'bids':[['1','1']]}},heartbeat_at_us=self.now-5000001)
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.mark('unexplained',books={'OTHER':{'at_us':self.now,'bids':[['1','1']]}},balances={'USD':'20000','OTHER':'1'})
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.assertEqual('10000',self.balances()['USD'])

    def test_rounding_and_compensation_export_cutoff_keeps_original_economics(self):
        self.initialize()
        self.intent('buy')
        original=self.fill('before-correction','buy','1','100','USD','0')
        self.mark('reconciled-before-correction',books={'AAVE':{'at_us':self.now,'bids':[['100','1']]}})
        cutoff=self.now;view=self.ledger.account_view('paper-test')
        self.now+=1
        corrected={'fill_id':'before-correction','previous_payload_hash':content_hash(original),'replacement':dict(original,quote_quantity='110',price='110')}
        self.ledger.correct_fill(self.evidence('future-correction','correction',corrected))
        archive=self.ledger.export_session(SessionCutoff('paper-test',cutoff,view.reconcile_id))
        from bot.autonomous.contracts import strict_json
        payload=strict_json(self.ledger.connection.execute('SELECT export_json FROM review_sessions WHERE session_id=?',(archive.session_id,)).fetchone()[0])
        self.assertEqual([],payload['fill_corrections'])
        self.assertEqual([], [o for o in payload['accounting_operations'] if o['kind']=='correction'])
        self.assertEqual('100',payload['fills'][0]['quote_qty'])
        self.assertTrue(payload['accounting_rounding'])

    def test_projection_failure_retains_factual_transfer_and_inhibits_ready_view(self):
        initial=self.evidence('huge-units','initial',{'balances':[{'asset':'USD','quantity':'1','basis_usd_u':1000000}],
            'units':'10000000000000000000000000000000000000'})
        self.ledger.initialize_account(initial)
        self.mark('huge-opening')
        self.assertTrue(self.ledger.account_view('paper-test').ready)
        # Exact rational units can grow beyond the bounded display range. This is
        # a supported resource failure, never permission to truncate authority.
        pre=self.ledger.account_view('paper-test').exact_nav.mark_id
        p={'transfer_key':'oversized','asset':'USD','quantity':'1000000','usd_value_u':1000000000000,
            'fee_usd_u':0,'preflow_mark_id':pre}
        self.assertFalse(self.ledger.record_flow(self.evidence('oversized','flow',p)))
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.assertEqual(0,self.ledger.connection.execute('SELECT count(*) FROM external_flows').fetchone()[0])
        self.assertEqual('1',self.balances()['USD'])

    def test_later_depth_cannot_be_relabeled_as_midnight(self):
        self.initialize(extra=({'asset':'AAVE','quantity':'1','basis_usd_u':100000000},))
        self.mark('initial-asset',books={'AAVE':{'at_us':self.now,'bids':[['100','1']]}})
        boundary=self.now+86400000000
        self.now=boundary+1
        p={'balances':self.balances(),'books':{'AAVE':{'at_us':self.now,'bids':[['1000','1']]}},
            'heartbeat_at_us':boundary,'open_intent_ids':[],'liabilities_usd_u':0,'exit_cost_usd_u':0}
        evidence=self.ledger.import_local_evidence('paper-test','false-boundary','mark',p,
            event_at_us=boundary,received_at_us=self.now,known_at_us=self.now)
        self.ledger.record_mark(evidence)
        self.assertNotEqual(boundary,self.ledger.connection.execute('SELECT day_start_us FROM accounting_state').fetchone()[0])
        self.assertFalse(self.ledger.account_view('paper-test').ready)

    def test_genuine_boundary_restored_after_outage_requires_current_reconciliation(self):
        self.initialize()
        self.flow('known-loss',1000000,300000000)
        self.assertEqual(1,self.ledger.connection.execute('SELECT daily_halted FROM account_control').fetchone()[0])
        boundary=self.now+86400000000
        self.now=boundary+10000000
        p={'balances':self.balances(),'books':{},'heartbeat_at_us':boundary,'open_intent_ids':[],
            'liabilities_usd_u':0,'exit_cost_usd_u':0}
        evidence=self.ledger.import_local_evidence('paper-test','real-boundary','mark',p,
            event_at_us=boundary,received_at_us=self.now,known_at_us=self.now)
        self.ledger.record_mark(evidence)
        self.assertEqual(boundary,self.ledger.connection.execute('SELECT day_start_us FROM accounting_state').fetchone()[0])
        self.assertFalse(self.ledger.account_view('paper-test').ready)
        self.assertEqual(1,self.ledger.connection.execute('SELECT daily_halted FROM account_control').fetchone()[0])
        self.mark('fresh-after-outage')
        self.assertTrue(self.ledger.account_view('paper-test').ready)
        self.assertEqual(0,self.ledger.connection.execute('SELECT daily_halted FROM account_control').fetchone()[0])

    def test_additive_accounting_migration_preserves_legacy_committed_export(self):
        import hashlib
        from unittest.mock import patch
        from bot.autonomous import ledger as module
        path=Path(self.directory.name)/'legacy.sqlite'
        c=sqlite3.connect(path,isolation_level=None)
        c.row_factory=sqlite3.Row
        migration=Path('data/migrations/001_autonomous_ledger.sql')
        c.executescript(migration.read_text())
        c.execute('INSERT INTO schema_migrations VALUES (1,?,?)',(hashlib.sha256(migration.read_bytes()).hexdigest(),self.now))
        legacy=load_ledger()(c)
        legacy._clock_us=lambda:self.now
        additions={'accounting_evidence','accounting_operations','exact_marks','exact_flows','accounting_findings','accounting_rounding'}
        old_policy={k:v for k,v in module._EXPORT_TIMES.items() if k not in additions}
        with patch.dict(module._EXPORT_TIMES,old_policy,clear=True):
            legacy.create_paper_account('paper-test')
            with legacy.transaction():
                c.execute("INSERT INTO reconciliations VALUES ('legacy-reconcile','paper-test',?,?,NULL,'completed','{}','[]','[]',?)",(self.now,self.now,self.now))
        legacy.close()
        upgraded=load_ledger().open(path)
        try:
            upgraded._clock_us=lambda:self.now+100
            archive=upgraded.export_session(SessionCutoff('paper-test',self.now,'legacy-reconcile'))
            from bot.autonomous.contracts import strict_json
            payload=strict_json(upgraded.connection.execute('SELECT export_json FROM review_sessions WHERE session_id=?',(archive.session_id,)).fetchone()[0])
            self.assertTrue(all(payload[table]==[] for table in additions))
            historical_inventory=upgraded.inventory_as_of('paper-test',CausalCutoffs(self.now,self.now,1,'legacy-boot'))
            self.assertEqual((),historical_inventory.quantities)
            self.assertEqual(0,historical_inventory.revision.journal_sequence)
        finally:upgraded.close()

    def test_accounting_write_boundary_failures_roll_back_every_projection(self):
        self.initialize()
        self.intent('buy')
        self.mark('depth-before-faults',books={'AAVE':{'at_us':self.now,'bids':[['100','10']]}})
        targets={'journal_transactions':'INSERT','journal_postings':'INSERT','balance_positions':'UPDATE',
            'accounting_operations':'INSERT','external_flows':'INSERT','exact_flows':'INSERT',
            'equity_marks':'INSERT','exact_marks':'INSERT','account_readiness':'UPDATE',
            'account_control':'UPDATE','control_events':'INSERT','reservations':'UPDATE'}
        for table,action in targets.items():
            with self.subTest(table=table),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'fault.sqlite'
                copy=sqlite3.connect(path)
                self.ledger.connection.backup(copy);copy.close()
                ledger=load_ledger().open(path);ledger._clock_us=lambda:self.now
                try:
                    if table=='reservations':
                        kind='fill'
                        p={'fill_id':'fault','intent_id':'buy','venue':'paper','trade_id':'fault','order_id':'buy',
                            'instrument':'AAVE-USD','side':'buy','base_quantity':'1','quote_quantity':'100','price':'100',
                            'fee_asset':'USD','fee_quantity':'1600','base_semantics':'gross','fee_final':True}
                    else:
                        kind='flow'
                        p={'transfer_key':'fault','asset':'USD','quantity':'1','usd_value_u':1000000,'fee_usd_u':1600000000,
                            'preflow_mark_id':ledger.account_view('paper-test').exact_nav.mark_id}
                    evidence=ledger.import_local_evidence('paper-test','fault',kind,p,event_at_us=self.now,received_at_us=self.now,known_at_us=self.now)
                    tables=[r[0] for r in ledger.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                    def snapshot():return {name:[tuple(r) for r in ledger.connection.execute('SELECT * FROM '+name+' ORDER BY rowid')] for name in tables}
                    before=snapshot()
                    ledger.connection.execute('CREATE TRIGGER injected BEFORE '+action+' ON '+table+" BEGIN SELECT RAISE(ABORT,'accounting fault'); END")
                    with self.assertRaises(PersistenceUnavailable):
                        (ledger.record_fill if kind=='fill' else ledger.record_flow)(evidence)
                    self.assertEqual(before,snapshot())
                    self.assertFalse(ledger.dispatch_enabled)
                finally:ledger.close()


class HistoricalInventoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "historical-ledger.sqlite3"
        from bot.autonomous.ledger import Ledger
        self.ledger = Ledger.open(self.path)
        self.ledger.create_paper_account("paper-history")
        row=self.ledger.connection.execute("SELECT max(known_at_us),max(recorded_at_us) FROM account_revision_history WHERE account_id='paper-history'").fetchone()
        self.base=max(row)

    def tearDown(self):
        self.ledger.close()
        self.directory.cleanup()

    def append_native_tx(self, txid, source_kind, event_at_us, recorded_at_us, postings):
        self.ledger._clock_us=lambda:recorded_at_us
        with self.ledger.transaction() as c:
            sequence=c.execute("SELECT coalesce(max(sequence),0)+1 FROM journal_transactions").fetchone()[0]
            c.execute("INSERT INTO journal_transactions(transaction_id,sequence,account_id,source_kind,source_id,event_at_us,recorded_at_us) VALUES (?,?,?,?,?,?,?)",
                (txid,sequence,"paper-history",source_kind,txid,event_at_us,recorded_at_us))
            for line_no,(ledger_account,asset,native_delta,usd_delta) in enumerate(postings):
                c.execute("INSERT INTO journal_postings(transaction_id,line_no,ledger_account,asset,native_delta,usd_delta_u,recorded_at_us) VALUES (?,?,?,?,?,?,?)",
                    (txid,line_no,ledger_account,asset,native_delta,usd_delta,recorded_at_us))

    def test_inventory_excludes_post_close_fill_and_flow_and_keeps_exact_fee_quantity(self):
        close=self.base+10
        self.append_native_tx("fill-before-close","fill",close,self.base+20,(
            ("cash",None,None,101),("fee_expense",None,None,-101),
            ("inventory:BTC","BTC","0.12345678901234567890123456789012345678",0),
            ("settlement","BTC","-0.12345678901234567890123456789012345678",0),
            ("inventory:FEE","FEE","-0.00000000000000000000000000000000000001",0),
            ("fee_suspense","FEE","0.00000000000000000000000000000000000001",0),
            ("cash","USD","50",0),("capital","USD","-50",0)))
        self.append_native_tx("fill-after-close","fill",close+1,self.base+30,(
            ("cash",None,None,900),("fee_expense",None,None,-900),
            ("inventory:BTC","BTC","9",0),("settlement","BTC","-9",0)))
        self.append_native_tx("flow-after-close","transfer",close+2,self.base+40,(
            ("cash",None,None,500),("capital",None,None,-500),
            ("inventory:BTC","BTC","5",0),("capital","BTC","-5",0)))
        cutoffs=CausalCutoffs(close,self.base+100,1,"boot-history")
        inventory=self.ledger.inventory_as_of("paper-history",cutoffs)
        self.assertEqual((("BTC",Decimal("0.12345678901234567890123456789012345678")),
            ("FEE",Decimal("-0.00000000000000000000000000000000000001"))),inventory.quantities)
        self.assertEqual(cutoffs,inventory.cutoffs)
        self.assertEqual("paper-history",inventory.account_id)
        self.assertEqual(3,inventory.revision.journal_sequence)
        self.assertNotIn("USD",dict(inventory.quantities))
        self.assertEqual(64,len(inventory.lineage_hash))

    def test_late_known_pre_close_event_is_excluded_at_older_knowledge_cutoff(self):
        close=self.base+10
        self.append_native_tx("visible","fill",close,self.base+20,(
            ("cash",None,None,125),("fee_expense",None,None,-125),
            ("inventory:BTC","BTC","1.25",0),("settlement","BTC","-1.25",0)))
        cutoff=CausalCutoffs(close,self.base+100,1,"boot-history")
        self.append_native_tx("late-known","fill",close-1,self.base+200,(
            ("inventory:BTC","BTC","7",-700),("cash:USD",None,None,700)))
        inventory=self.ledger.inventory_as_of("paper-history",cutoff)
        self.assertEqual((("BTC",Decimal("1.25")),),inventory.quantities)
        self.assertEqual(1,inventory.revision.journal_sequence)

    def test_inventory_rejects_transaction_truncated_at_watermark(self):
        close=self.base+10
        self.append_native_tx("truncated","fill",close,self.base+20,(
            ("cash",None,None,100),("fee_expense",None,None,-100),
            ("inventory:BTC","BTC","1",0),("settlement","BTC","-1",0)))
        selected=self.ledger.connection.execute("SELECT append_sequence,evidence_watermarks_json FROM account_revision_history WHERE account_id='paper-history' ORDER BY append_sequence DESC LIMIT 1").fetchone()
        from bot.autonomous.contracts import strict_json
        watermarks=strict_json(selected['evidence_watermarks_json'])
        monetary_prefix=self.ledger.connection.execute("SELECT rowid FROM journal_postings WHERE transaction_id='truncated' AND line_no=1").fetchone()[0]
        watermarks['journal_postings']=monetary_prefix
        self.ledger.connection.execute("DROP TRIGGER account_revision_history_no_update")
        self.ledger.connection.execute("UPDATE account_revision_history SET evidence_watermarks_json=? WHERE append_sequence=?",
            (canonical_bytes(watermarks).decode(),selected['append_sequence']))
        with self.assertRaises(ContractViolation):
            self.ledger.inventory_as_of("paper-history",CausalCutoffs(close,self.base+100,1,"boot-history"))

    def test_inventory_rejects_unbalanced_native_asset_postings(self):
        close=self.base+10
        self.append_native_tx("native-imbalance","fill",close,self.base+20,(
            ("cash",None,None,100),("fee_expense",None,None,-100),
            ("inventory:BTC","BTC","1",0),("settlement","BTC","-0.5",0)))
        with self.assertRaises(ContractViolation):
            self.ledger.inventory_as_of("paper-history",CausalCutoffs(close,self.base+100,1,"boot-history"))

    def test_inventory_fails_closed_without_cutoff_visible_history(self):
        cutoffs=CausalCutoffs(self.base,self.base,1,"boot-history")
        with self.assertRaises(ContractViolation):
            self.ledger.inventory_as_of("missing-history",cutoffs)
