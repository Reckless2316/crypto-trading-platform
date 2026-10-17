"""Durability, idempotency, and fail-closed authority boundaries (no network)."""
import dataclasses
import sqlite3
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from bot.autonomous.contracts import (AccountRevision, ContractViolation, IntentState,
    OrderEvent, OrderReference, PersistenceUnavailable, ReceiptStatus, ReviewJob,
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
        """Persist inert fixtures; production authority owns import and arming."""
        c = self.ledger.connection
        now = time.time_ns() // 1000
        c.execute("INSERT INTO release_bundles(release_hash,manifest_json,expires_at_us,recorded_at_us) VALUES (?, '{}', ?, ?)", (RELEASE_HASH, now + 100_000_000, now))
        c.execute("INSERT INTO snapshots(snapshot_id,account_id,release_hash,candle_close_us,knowledge_cutoff_us,field_hash,state_json,lineage_hash,account_revision,valid,tokenizer_hash,token_count,recorded_at_us) VALUES (?,'paper-test',?,1000000,1000000,?,'{}',?,'{}',?,?,?,?)", (HASH_B, RELEASE_HASH, HASH_A, HASH_B, int(valid), HASH_A if valid else None, 10 if valid else None, now))
        c.execute("INSERT INTO decisions(decision_id,snapshot_id,request_id,deadline_us,model_id,status,recorded_at_us) VALUES ('decision-test',?,'request-test',?,'test','completed',?)", (HASH_B, now + 100_000_000, now))
        c.execute("INSERT INTO reconciliations(reconcile_id,account_id,started_at_us,finished_at_us,status,balances_json,open_orders_json,differences_json,recorded_at_us) VALUES ('reconcile','paper-test',?,?,'completed','[]','[]','[]',?)", (now, now, now))
        return now

    def ready(self):
        now = self.seed(valid=True)
        from bot.autonomous.contracts import MarketRules
        c = self.ledger.connection
        c.execute("INSERT INTO candidate_results(decision_id,candidate_id,status,veto_codes_json,recorded_at_us) VALUES ('decision-test',1,'eligible','[]',?)", (now,))
        c.execute("INSERT INTO equity_marks(mark_id,account_id,lineage_hash,equity_usd_u,units,nav,high_water_nav,day_open_nav,drawdown,daily_loss,valuation_version,marked_at_us,recorded_at_us) VALUES ('mark','paper-test',?,10000000000,'10000','1','1','1','0','0',0,?,?)", (HASH_A, now, now))
        c.execute("INSERT INTO balance_positions(account_id,asset,native_qty,cost_basis_usd_u,journal_sequence,version) VALUES ('paper-test','USD','10000',10000000000,0,0)")
        c.execute("UPDATE account_control SET permit='entry',permit_epoch=1,lease_boot_id='test-boot',lease_deadline_mono_ns=?,active_release_hash=?,reconcile_id='reconcile',version=version+1", (time.monotonic_ns() + 100_000_000_000, RELEASE_HASH))
        c.execute("UPDATE account_readiness SET ready=1,private_heartbeat_us=?,version=version+1 WHERE account_id='paper-test'", (now,))
        limits = RiskLimits(armed=True,candidate_eligible=True,tokenizer_config_hash=HASH_A,calibration_config_hash=HASH_A)
        market = MarketRules('paper','AAVE-USD','AAVE','USD',Decimal('0.01'),Decimal('0.01'),Decimal('0.01'),Decimal('1'),Decimal('0.01'),2,HASH_A,now+100_000_000,True,True,now,10_000_000_000)
        self.ledger.configure_risk_checks(limits=limits, market=lambda _: market,
            assess=lambda p,a,c,l,m,n: RiskVerdict(True,(),a.revision,content_hash(p)), boot_id='test-boot')
        p=order_proposal()
        return dataclasses.replace(p, release_hash=RELEASE_HASH, decision=dataclasses.replace(p.decision,release_hash=RELEASE_HASH),control_epoch=1,deadline_mono_ns=time.monotonic_ns()+100_000_000_000)

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
            assess=lambda p,a,c,l,m,n:RiskVerdict(True,(),a.revision,HASH_B))
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
        def assessor(proposal,account,control,limits,market,now):
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
