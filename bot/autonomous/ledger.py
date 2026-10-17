"""Authority-only durable journal and conservative order reservation boundary.

There is deliberately no implicit account import, live adoption, or arming. The
risk hook is an in-process authority dependency; it is never accepted from JSON.
All hooks are synchronous and execute inside the immediate reservation transaction.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace
from decimal import Context, Decimal, DecimalException, ROUND_CEILING, localcontext
from functools import wraps
from pathlib import Path
from typing import Callable, Iterator

from .contracts import (
    AccountRevision, AccountView, AppendResult, Balance, ContractViolation,
    ControlState, DecisionReference, IntentState, ManifestEntry, MarketRules, Mode,
    OrderEvent, OrderProposal, Permit, PersistenceUnavailable, PreparedIntent,
    Reservation, RiskLimits, RiskVerdict, SessionArchive, SessionCutoff, ReviewJob,
    candidate_wire_id, canonical_bytes, checked_money, content_hash, decimal_text,
    decimal_value, strict_json,
)

_MIGRATION = Path(__file__).resolve().parents[2] / 'data/migrations/001_autonomous_ledger.sql'
_TERMINAL = {IntentState.FILLED, IntentState.CANCELED, IntentState.REJECTED}
_TRANSITIONS = {
    IntentState.RESERVED: {IntentState.DISPATCHING, IntentState.REJECTED},
    IntentState.DISPATCHING: {IntentState.OPEN, IntentState.PARTIAL, IntentState.FILLED, IntentState.REJECTED, IntentState.UNKNOWN},
    IntentState.OPEN: {IntentState.PARTIAL, IntentState.FILLED, IntentState.CANCELED, IntentState.UNKNOWN},
    IntentState.PARTIAL: {IntentState.PARTIAL, IntentState.FILLED, IntentState.CANCELED, IntentState.UNKNOWN},
    IntentState.UNKNOWN: {IntentState.OPEN, IntentState.PARTIAL, IntentState.FILLED, IntentState.CANCELED, IntentState.REJECTED},
}


# Visibility policies describe observation/receipt/knowledge time, not future
# deadlines, expiry windows, forecast maturity, or other declared metadata.
_EXPORT_TIMES = {
    'accounts': ('created_at_us',),
    'account_revision_history': ('recorded_at_us','known_at_us'),
    'control_events': ('recorded_at_us',),
    'source_events': ('recorded_at_us','event_at_us','received_at_us','known_at_us'),
    'source_invalidations': ('recorded_at_us','known_at_us'),
    'event_segments': ('recorded_at_us','committed_at_us','event_from_us','event_to_us','received_from_us','received_to_us'),
    'snapshots': ('recorded_at_us','candle_close_us','knowledge_cutoff_us'),
    'snapshot_inputs': ('recorded_at_us',),
    'decisions': ('recorded_at_us','received_at_us'),
    'candidate_results': ('recorded_at_us',),
    'order_intents': ('recorded_at_us','candle_close_us'),
    'order_events': ('recorded_at_us','event_at_us','received_at_us'),
    'fills': ('recorded_at_us','event_at_us','received_at_us'),
    'fill_corrections': ('recorded_at_us','received_at_us'),
    'journal_transactions': ('recorded_at_us','event_at_us'),
    'journal_postings': ('recorded_at_us',),
    'equity_marks': ('recorded_at_us','marked_at_us'),
    'external_flows': ('recorded_at_us','event_at_us'),
    'reconciliations': ('recorded_at_us','started_at_us','finished_at_us'),
    'forecasts': ('recorded_at_us',),
    'outcome_labels': (),  # stable identity requires a visible label event below
    'label_events': ('recorded_at_us',),
    'evidence_manifests': ('recorded_at_us','cutoff_us'),
    'release_bundles': ('recorded_at_us',),
    'release_events': ('recorded_at_us',),
    'evaluation_runs': ('recorded_at_us',),
}
_EXPORT_OMIT = {'schema_migrations','review_sessions','review_jobs','account_control',
    'account_readiness','balance_positions','candidate_control','reservations'}
_MANIFEST_KEYS = {
    'source_events': ('event_id',), 'source_invalidations': ('invalidation_id',),
    'event_segments': ('segment_hash',), 'forecasts': ('forecast_id',),
    'outcome_labels': ('label_id',), 'label_events': ('label_event_id',),
    'release_bundles': ('release_hash',), 'evidence_manifests': ('manifest_hash',),
    'evaluation_runs': ('run_id',), 'snapshots': ('snapshot_id',),
    'snapshot_inputs': ('snapshot_id','line_no'),
}


def _decimal_boundary(function):
    """Supported contract arithmetic must not inherit a caller's precision/traps."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            with localcontext(Context(prec=100)):
                return function(*args, **kwargs)
        except DecimalException as exc:
            raise ContractViolation('invalid decimal boundary arithmetic') from exc
    return wrapped


def validate_postings(postings: tuple[tuple[str, int], ...]) -> None:
    if type(postings) is not tuple or len(postings) < 2:
        raise ContractViolation('invalid postings')
    for line in postings:
        if type(line) is not tuple or len(line) != 2:
            raise ContractViolation('invalid posting line')
        name, amount = line
        if type(name) is not str or not name or len(name) > 256:
            raise ContractViolation('invalid ledger account')
        checked_money(amount)
    if sum(amount for _, amount in postings) != 0:
        raise ContractViolation('unbalanced journal')


def _identifier(value: str) -> str:
    if type(value) is not str or not value or len(value) > 256 or not value.isascii():
        raise ContractViolation('invalid identifier')
    return value


class Ledger:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.dispatch_enabled = True
        self._risk_config: tuple | None = None
        self._transaction_failed = False
        self._clock_us: Callable[[], int] = lambda: time.time_ns() // 1000
        self._monotonic_ns: Callable[[], int] = time.monotonic_ns

    @classmethod
    def open(cls, path: Path) -> Ledger:
        connection = None
        try:
            connection = sqlite3.connect(str(path), timeout=0.25, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA busy_timeout=250')
            connection.execute('PRAGMA wal_autocheckpoint=1000')
            connection.execute('PRAGMA cache_size=-8192')
            connection.execute('PRAGMA journal_size_limit=16777216')
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16 * 1024 * 1024)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 1024 * 1024)
            ledger = cls(connection)
            ledger._migrate()
            ledger._integrity_check()
            ledger._recover()
            return ledger
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
            if connection is not None:
                connection.close()
            raise PersistenceUnavailable('ledger open/integrity unavailable') from exc
        except PersistenceUnavailable:
            if connection is not None:
                connection.close()
            raise

    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error as exc:
            self.dispatch_enabled = False
            raise PersistenceUnavailable('ledger close unavailable') from exc
        finally:
            self.dispatch_enabled = False

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Short synchronous boundary for authority/accounting collaborators.

        Nested operations use the outer immediate transaction. Any SQLite or
        uncertain commit failure disables dispatch until this Ledger is reopened.
        """
        if not self.dispatch_enabled:
            raise PersistenceUnavailable('ledger requires reopen and reconciliation')
        outer = not self.connection.in_transaction
        committing = False
        try:
            if outer:
                self.connection.execute('BEGIN IMMEDIATE')
                self._transaction_failed = False
            yield self.connection
            if outer:
                if self._transaction_failed or not self.dispatch_enabled:
                    raise PersistenceUnavailable('nested operation failed; outer transaction rolled back')
                self._record_revisions()
                committing = True
                self.connection.execute('COMMIT')
        except BaseException as exc:
            if committing:
                self.dispatch_enabled = False
            self._transaction_failed = True
            if outer and self.connection.in_transaction:
                try:
                    self.connection.execute('ROLLBACK')
                except sqlite3.Error:
                    self.dispatch_enabled = False
            if isinstance(exc, sqlite3.Error):
                self.dispatch_enabled = False
                raise PersistenceUnavailable('ledger persistence failed; dispatch disabled') from exc
            raise

    def _migrate(self) -> None:
        sql = _MIGRATION.read_text()
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        exists = self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        if exists:
            migrations = self.connection.execute('SELECT version,checksum FROM schema_migrations ORDER BY version').fetchall()
            if [(r[0], r[1]) for r in migrations] != [(1, checksum)]:
                raise PersistenceUnavailable('migration checksum/version mismatch')
            return
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone():
            raise PersistenceUnavailable('refusing unrecognized database adoption')
        # executescript controls its own transaction; include every DDL and marker
        # in that transaction, so a failed migration leaves no partial schema.
        try:
            self.connection.executescript('BEGIN IMMEDIATE;\n' + sql +
                f"\nINSERT INTO schema_migrations VALUES(1,'{checksum}',{self._clock_us()});\nCOMMIT;")
        except sqlite3.Error:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise

    def _integrity_check(self) -> None:
        if self.connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise PersistenceUnavailable('database corruption')
        if self.connection.execute('PRAGMA foreign_key_check').fetchone():
            raise PersistenceUnavailable('foreign key integrity failure')
        # Verify protections as well as the migration marker (a dropped trigger
        # must not silently weaken an otherwise checksummed schema).
        reference = sqlite3.connect(':memory:')
        try:
            reference.executescript(_MIGRATION.read_text())
            expected = {row[0]:row[1] for row in reference.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
        finally:
            reference.close()
        actual = {row[0]:row[1] for row in self.connection.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
        if any(actual.get(name)!=sql for name,sql in expected.items()):
            raise PersistenceUnavailable('schema/evidence protection integrity failure')
        for tx in self.connection.execute('SELECT transaction_id,account_id,source_kind,source_id FROM journal_transactions'):
            rows = self.connection.execute('SELECT ledger_account,usd_delta_u FROM journal_postings WHERE transaction_id=? ORDER BY line_no',(tx['transaction_id'],)).fetchall()
            validate_postings(tuple((r[0], r[1]) for r in rows))
            table_key = {'fill': ('fills','fill_id'), 'correction': ('fill_corrections','correction_id'), 'transfer': ('external_flows','flow_id')}.get(tx['source_kind'])
            if table_key and not self.connection.execute('SELECT 1 FROM '+table_key[0]+' WHERE '+table_key[1]+'=?',(tx['source_id'],)).fetchone():
                raise PersistenceUnavailable('missing journal source evidence')
        for table, json_column, hash_column in (
            ('source_events','payload_json','payload_hash'),
            ('fill_corrections','corrected_payload_json','corrected_payload_hash'),
            ('label_events','payload_json','payload_hash')):
            for row in self.connection.execute('SELECT '+json_column+','+hash_column+' FROM '+table):
                if content_hash(strict_json(row[0])) != row[1]:
                    raise PersistenceUnavailable('content hash integrity failure in '+table)
        for row in self.connection.execute('SELECT release_hash,manifest_json FROM release_bundles'):
            manifest = strict_json(row['manifest_json'])
            if type(manifest) is not dict:
                raise PersistenceUnavailable('release manifest is not an object')
            # Gate approvals bind the canonical core, avoiding self-reference.
            core = {key:value for key,value in manifest.items() if key!='gate_approvals'}
            if content_hash(core)!=row['release_hash']:
                raise PersistenceUnavailable('release content identity mismatch')
        for row in self.connection.execute('SELECT archive_json,manifest_hash,export_json FROM review_sessions'):
            archive = SessionArchive.from_json(row[0])
            expected_manifest = content_hash((archive.cutoff,archive.account_revision,
                archive.database_export_hash,archive.event_manifest,archive.forecast_manifest,
                archive.outcome_manifest,archive.artifact_manifest))
            if archive.manifest_hash != row[1] or archive.manifest_hash!=expected_manifest or archive.database_export_hash != content_hash(strict_json(row[2])):
                raise PersistenceUnavailable('session content integrity failure')
        for table in [row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
            columns = [r['name'] for r in self.connection.execute('PRAGMA table_info('+table+')')]
            json_columns = [name for name in columns if name.endswith('_json') or name=='account_revision']
            if json_columns:
                for row in self.connection.execute('SELECT '+','.join(json_columns)+' FROM '+table):
                    for raw in row:
                        if raw is not None and canonical_bytes(strict_json(raw)).decode()!=raw:
                            raise PersistenceUnavailable('noncanonical JSON in '+table)
        decimal_columns = {
            'order_intents': ('qty','limit_price'), 'reservations': ('base_qty','remaining_qty'),
            'balance_positions': ('native_qty',), 'equity_marks': ('units','nav','high_water_nav','day_open_nav','drawdown','daily_loss'),
            'fills': ('base_qty','quote_qty','price','fee_qty'), 'journal_postings': ('native_delta',),
            'external_flows': ('native_qty','units_delta'), 'outcome_labels': ('net_return',),
        }
        for table, columns in decimal_columns.items():
            for row in self.connection.execute('SELECT '+','.join(columns)+' FROM '+table):
                for value in row:
                    if value is not None:
                        number = decimal_value(value)
                        if decimal_text(number)!=value or number<0 and table not in {'journal_postings','external_flows','outcome_labels'}:
                            raise PersistenceUnavailable('invalid decimal in '+table)

    def _recover(self) -> None:
        now = self._clock_us()
        with self.transaction() as c:
            for row in c.execute('SELECT account_id,generation FROM accounts').fetchall():
                account_id = row['account_id']
                c.execute('UPDATE accounts SET generation=generation+1 WHERE account_id=?',(account_id,))
                c.execute("UPDATE account_control SET permit='none',permit_epoch=permit_epoch+1,lease_boot_id=NULL,lease_deadline_mono_ns=NULL,version=version+1,updated_at_us=? WHERE account_id=?",(now,account_id))
                c.execute('UPDATE account_readiness SET ready=0,private_heartbeat_us=NULL,version=version+1 WHERE account_id=?',(account_id,))
                for intent in c.execute("SELECT intent_id,generation FROM order_intents WHERE account_id=? AND state='dispatching'",(account_id,)).fetchall():
                    payload = canonical_bytes({'reason':'crash_after_dispatch','generation':intent['generation']}).decode()
                    self._append_order_evidence('recovery-'+content_hash((intent['intent_id'],row['generation'])),intent['intent_id'],'unknown',payload,now,now)
                    c.execute("UPDATE order_intents SET state='unknown',version=version+1 WHERE intent_id=?",(intent['intent_id'],))
                    c.execute("UPDATE reservations SET state='unknown',version=version+1,updated_at_us=? WHERE intent_id=?",(now,intent['intent_id']))
                control = c.execute('SELECT permit_epoch FROM account_control WHERE account_id=?',(account_id,)).fetchone()
                event_id = 'recovery-'+content_hash((account_id,row['generation']+1))
                c.execute("INSERT INTO control_events VALUES (?,?,?,?,'recovery','authority','startup unarmed; reconciliation required',?)",(event_id,account_id,event_id,control[0],now))

    def _require_account(self, account_id: str) -> sqlite3.Row:
        row = self.connection.execute('SELECT * FROM accounts WHERE account_id=?',(_identifier(account_id),)).fetchone()
        if row is None:
            raise ContractViolation('unknown account; explicit adoption required')
        return row

    def create_paper_account(self, account_id: str) -> None:
        _identifier(account_id)
        with self.transaction() as c:
            existing = c.execute('SELECT mode,venue FROM accounts WHERE account_id=?',(account_id,)).fetchone()
            if existing:
                if tuple(existing) != ('paper','paper'):
                    raise ContractViolation('account mode/venue identity conflict')
                return
            if c.execute('SELECT 1 FROM accounts').fetchone():
                raise ContractViolation('deployment has one selected account')
            now = self._clock_us()
            c.execute("INSERT INTO accounts VALUES (?,'paper','paper','USD',?,0)",(account_id,now))
            c.execute("INSERT INTO account_control VALUES (?,'none',0,0,0,0,0,NULL,NULL,NULL,NULL,NULL,0,?)",(account_id,now))
            c.execute('INSERT INTO account_readiness(account_id) VALUES (?)',(account_id,))

    def _revision(self, account_id: str) -> AccountRevision:
        sequence = self.connection.execute('SELECT coalesce(max(sequence),0) FROM journal_transactions WHERE account_id=?',(account_id,)).fetchone()[0]
        version = self.connection.execute('SELECT version FROM account_control WHERE account_id=?',(account_id,)).fetchone()[0]
        version += self.connection.execute('SELECT version FROM account_readiness WHERE account_id=?',(account_id,)).fetchone()[0]
        for sql in (
            'SELECT coalesce(sum(version),0) FROM balance_positions WHERE account_id=?',
            'SELECT coalesce(sum(version+1),0) FROM order_intents WHERE account_id=?',
            'SELECT coalesce(sum(r.version+1),0) FROM reservations r JOIN order_intents i USING(intent_id) WHERE i.account_id=?',
            'SELECT coalesce(sum(version),0) FROM candidate_control WHERE account_id=?'):
            version += self.connection.execute(sql,(account_id,)).fetchone()[0]
        return AccountRevision(sequence,version)

    def invalidate_account(self, account_id: str) -> None:
        """Called by accounting in its outer transaction after economic changes."""
        self._require_account(account_id)
        self.connection.execute('UPDATE account_readiness SET ready=0,version=version+1 WHERE account_id=?',(account_id,))

    def post(self, account_id: str, transaction_id: str, postings: tuple[tuple[str,int], ...]) -> bool:
        validate_postings(postings)
        _identifier(transaction_id)
        with self.transaction() as c:
            self._require_account(account_id)
            tx = c.execute('SELECT account_id,source_kind FROM journal_transactions WHERE transaction_id=?',(transaction_id,)).fetchone()
            if tx:
                existing = tuple((r[0],r[1]) for r in c.execute('SELECT ledger_account,usd_delta_u FROM journal_postings WHERE transaction_id=? ORDER BY line_no',(transaction_id,)))
                if tuple(tx) != (account_id,'journal') or existing != postings:
                    raise ContractViolation('journal identity reused with different content')
                return False
            sequence = c.execute('SELECT coalesce(max(sequence),0)+1 FROM journal_transactions').fetchone()[0]
            now = self._clock_us()
            c.execute("INSERT INTO journal_transactions VALUES (?,?,?,'journal',?,?,?)",(transaction_id,sequence,account_id,transaction_id,now,now))
            for line_no, (name,amount) in enumerate(postings):
                c.execute('INSERT INTO journal_postings VALUES (?,?,?,NULL,NULL,?,?)',(transaction_id,line_no,name,amount,now))
            self.invalidate_account(account_id)
            return True

    def account_view(self, account_id: str) -> AccountView:
        # A view is one read snapshot, including when used outside a write transaction.
        outer = not self.connection.in_transaction
        try:
            if outer:
                self.connection.execute('BEGIN')
            result = self._account_view(account_id)
            if outer:
                self.connection.execute('COMMIT')
            return result
        except BaseException as exc:
            if outer and self.connection.in_transaction:
                self.connection.rollback()
            if isinstance(exc,sqlite3.Error):
                self.dispatch_enabled = False
                raise PersistenceUnavailable('account snapshot unavailable') from exc
            raise

    def _account_view(self, account_id: str) -> AccountView:
        account = self._require_account(account_id)
        control = self.connection.execute('SELECT * FROM account_control WHERE account_id=?',(account_id,)).fetchone()
        runtime = self.connection.execute('SELECT * FROM account_readiness WHERE account_id=?',(account_id,)).fetchone()
        balances = tuple(Balance(r['asset'],decimal_value(r['native_qty']),r['cost_basis_usd_u']) for r in self.connection.execute('SELECT * FROM balance_positions WHERE account_id=? ORDER BY asset',(account_id,)))
        intents = self.connection.execute('SELECT * FROM order_intents WHERE account_id=? ORDER BY intent_id',(account_id,)).fetchall()
        # Retain even terminal reservations until the reconciler explicitly verifies
        # cumulative fills and final fees. Terminal order status alone proves neither.
        reservations = tuple(self._reservation(r) for r in self.connection.execute('SELECT r.* FROM reservations r JOIN order_intents i USING(intent_id) WHERE i.account_id=? AND (r.cash_usd_u>0 OR r.gross_usd_u>0 OR r.loss_budget_usd_u>0 OR r.remaining_qty!=\'0\') ORDER BY r.intent_id',(account_id,)))
        mark = self.connection.execute('SELECT * FROM equity_marks WHERE account_id=? ORDER BY marked_at_us DESC,recorded_at_us DESC,mark_id DESC LIMIT 1',(account_id,)).fetchone()
        reconcile = self.connection.execute('SELECT * FROM reconciliations WHERE reconcile_id=? AND account_id=?',(control['reconcile_id'],account_id)).fetchone()
        ready = bool(runtime['ready'] and mark and reconcile and reconcile['status']=='completed' and reconcile['finished_at_us'] is not None and runtime['private_heartbeat_us'] is not None and mark['valuation_version']==runtime['valuation_version'] and mark['equity_usd_u']>0)
        # Held-asset liquidation evidence comes from Task3. Until its immutable
        # holding valuation projection is integrated, any non-USD balance blocks.
        ready = ready and all(b.asset=='USD' or b.quantity==0 for b in balances)
        baseline = tuple(decimal_value(mark[k]) for k in ('units','nav','high_water_nav','day_open_nav')) if mark else (Decimal(0),)*4
        ready = ready and min(baseline)>0 and baseline[2]>=baseline[1]
        return AccountView(account_id=account_id,mode=Mode(account['mode']),revision=self._revision(account_id),
            liquidation_equity_usd_u=mark['equity_usd_u'] if mark else 0,
            units=baseline[0],nav=baseline[1],high_water_nav=baseline[2],day_open_nav=baseline[3],
            balances=balances,reservations=reservations,
            open_intent_ids=tuple(r['intent_id'] for r in intents if IntentState(r['state']) not in _TERMINAL),
            unknown_intent_ids=tuple(r['intent_id'] for r in intents if r['state']=='unknown'),
            reconcile_id=reconcile['reconcile_id'] if reconcile else None,
            reconciled_at_us=reconcile['finished_at_us'] if reconcile else None,
            private_heartbeat_us=runtime['private_heartbeat_us'],marked_at_us=mark['marked_at_us'] if mark else None,ready=bool(ready))

    @staticmethod
    def _reservation(row: sqlite3.Row) -> Reservation:
        return Reservation(row['intent_id'],row['cash_usd_u'],decimal_value(row['base_qty']),row['gross_usd_u'],row['loss_budget_usd_u'],decimal_value(row['remaining_qty']),IntentState(row['state']),row['version'])

    def _prepared(self, intent_id: str) -> PreparedIntent:
        row = self.connection.execute('SELECT * FROM order_intents WHERE intent_id=?',(intent_id,)).fetchone()
        if row is None:
            raise ContractViolation('unknown intent')
        reservation = self.connection.execute('SELECT * FROM reservations WHERE intent_id=?',(intent_id,)).fetchone()
        if reservation is None:
            raise PersistenceUnavailable('intent missing reservation')
        return PreparedIntent(intent_id,OrderProposal.from_json(row['proposal_json']),self._reservation(reservation),row['client_order_id'],row['generation'],AccountRevision.from_json(row['prepared_revision_json']),IntentState(row['state']))

    def configure_risk_checks(self, *, limits: RiskLimits, market: Callable[[str], MarketRules],
            assess: Callable[[OrderProposal,AccountView,ControlState,RiskLimits,MarketRules,int],RiskVerdict],
            boot_id: str, clock_us: Callable[[],int] | None = None,
            monotonic_ns: Callable[[],int] | None = None) -> None:
        """Trusted Authority wiring only; never expose via control/import protocol.

        Market provider and assessor must be deterministic synchronous local
        reads/functions. Dispatch reuses this same wiring and re-reads durable state.
        """
        if type(limits) is not RiskLimits or not callable(market) or not callable(assess):
            raise ContractViolation('invalid trusted risk dependency')
        _identifier(boot_id)
        self._risk_config = (limits,market,assess,boot_id)
        if clock_us is not None:
            self._clock_us = clock_us
        if monotonic_ns is not None:
            self._monotonic_ns = monotonic_ns

    @_decimal_boundary
    def _risk_check(self, proposal: OrderProposal, expected: AccountRevision | None,
            generation: int, *, exclude_intent_id: str | None = None) -> tuple[AccountView,int,int]:
        if self._risk_config is None:
            raise ContractViolation('risk assessment dependency unavailable')
        limits, market_provider, assess, boot_id = self._risk_config
        a = self._require_account(proposal.account_id)
        account = self._account_view(proposal.account_id)
        row = self.connection.execute('SELECT * FROM account_control WHERE account_id=?',(proposal.account_id,)).fetchone()
        if expected is not None and account.revision != expected:
            raise ContractViolation('stale account revision')
        if type(generation) is not int or generation != a['generation']:
            raise ContractViolation('stale process generation')
        now, mono = self._clock_us(), self._monotonic_ns()
        if row['hard_killed'] or row['permit']=='none' or row['permit_epoch']!=proposal.control_epoch or row['lease_boot_id']!=boot_id or row['lease_deadline_mono_ns'] is None or mono>=row['lease_deadline_mono_ns'] or mono>=proposal.deadline_mono_ns:
            raise ContractViolation('disabled/stale control or deadline')
        if a['mode']=='shadow' or limits.mode.value != a['mode'] or not account.ready:
            raise ContractViolation('account/mode unready')
        if row['active_release_hash'] != proposal.release_hash:
            raise ContractViolation('inactive release')
        release = self.connection.execute('SELECT expires_at_us FROM release_bundles WHERE release_hash=?',(proposal.release_hash,)).fetchone()
        if release is None or release[0]<=now:
            raise ContractViolation('missing/expired release')
        if not proposal.reducing_only and (row['permit']!='entry' or row['drawdown_latched'] or row['daily_halted'] or row['crisis_halted'] or not limits.exposure_enabled or proposal.candidate_id=='ondo'):
            raise ContractViolation('entry inhibited')
        pause = self.connection.execute('SELECT review_paused FROM candidate_control WHERE account_id=? AND candidate_id=?',(proposal.account_id,candidate_wire_id(proposal.candidate_id))).fetchone()
        if pause and pause[0] and not proposal.reducing_only:
            raise ContractViolation('candidate review paused')
        if any(timestamp is None or timestamp>now or now-timestamp>age for timestamp,age in (
                (account.reconciled_at_us,15_000_000),(account.private_heartbeat_us,5_000_000),(account.marked_at_us,2_000_000))):
            raise ContractViolation('stale reconciled account/valuation')
        market = market_provider(proposal.instrument)
        if type(market) is not MarketRules or market.instrument!=proposal.instrument or market.quote_asset!='USD' or not market.spot_allowed or not market.limit_ioc_verified or market.expires_at_us<=now or market.book_at_us is None or not 0<=now-market.book_at_us<=2_000_000:
            raise ContractViolation('market capabilities/book unavailable')
        if proposal.limit_price % market.tick_size or proposal.base_quantity % market.lot_size or proposal.base_quantity<market.minimum_quantity:
            raise ContractViolation('invalid venue increments/minimum')
        with localcontext() as ctx:
            ctx.prec = 100
            notional_decimal = proposal.base_quantity * proposal.limit_price
            if notional_decimal<market.minimum_notional:
                raise ContractViolation('below venue minimum notional')
            notional = checked_money(int((notional_decimal*1_000_000).to_integral_value(rounding=ROUND_CEILING)))
            cash = checked_money(notional+proposal.max_fee_usd_u) if proposal.side.value=='buy' else 0
            if proposal.max_fee_usd_u < int((notional*market.fee_bound).to_integral_value(rounding=ROUND_CEILING)):
                raise ContractViolation('insufficient fee bound')
            pending = tuple(r for r in account.reservations if r.intent_id!=exclude_intent_id)
            # Core cash/base/gross/loss checks remain mandatory even with an assessor.
            available_usd = sum(b.quantity for b in account.balances if b.asset=='USD')*1_000_000
            if cash+sum(r.cash_usd_u for r in pending)>available_usd:
                raise ContractViolation('insufficient unreserved USD')
            if proposal.side.value=='sell':
                owned = sum(b.quantity for b in account.balances if b.asset==market.base_asset)
                reserved_base = sum(decimal_value(r['base_qty']) for r in self.connection.execute('SELECT r.base_qty FROM reservations r JOIN order_intents i USING(intent_id) WHERE i.account_id=? AND i.instrument=? AND i.side=\'sell\' AND i.intent_id!=?',(proposal.account_id,proposal.instrument,exclude_intent_id or '')))
                if proposal.base_quantity+reserved_base>owned:
                    raise ContractViolation('insufficient unreserved base')
            if not proposal.reducing_only:
                equity = Decimal(account.liquidation_equity_usd_u)
                asset_pending = sum(r['gross_usd_u'] for r in self.connection.execute('SELECT r.gross_usd_u FROM reservations r JOIN order_intents i USING(intent_id) WHERE i.account_id=? AND i.instrument=? AND i.side=\'buy\' AND i.intent_id!=?',(proposal.account_id,proposal.instrument,exclude_intent_id or '')))
                if notional+asset_pending>equity*limits.max_position_fraction or notional+sum(r.gross_usd_u for r in pending)>equity*limits.max_gross_fraction:
                    raise ContractViolation('position/gross capacity exceeded')
                if proposal.loss_budget_usd_u<=0 or proposal.loss_budget_usd_u>equity*limits.max_entry_loss_fraction or proposal.loss_budget_usd_u+sum(r.loss_budget_usd_u for r in pending)>equity*limits.max_pending_loss_fraction:
                    raise ContractViolation('modeled loss capacity exceeded')
                if account.nav<=account.high_water_nav*Decimal('.85') or account.nav<=account.day_open_nav*Decimal('.98'):
                    raise ContractViolation('NAV loss limit')
                if notional>market.available_buy_notional_usd_u:
                    raise ContractViolation('insufficient bounded depth')
        control = ControlState(mode=Mode(a['mode']),permit=Permit(row['permit']),hard_killed=bool(row['hard_killed']),drawdown_latched=bool(row['drawdown_latched']),daily_halted=bool(row['daily_halted']),crisis_halted=bool(row['crisis_halted']),control_epoch=row['permit_epoch'],boot_id=row['lease_boot_id'],deadman_deadline_mono_ns=row['lease_deadline_mono_ns'],active_release_hash=row['active_release_hash'],reconciliation_ready=account.ready,armed=True)
        assess_account = replace(account,
            reservations=tuple(r for r in account.reservations if r.intent_id!=exclude_intent_id),
            open_intent_ids=tuple(i for i in account.open_intent_ids if i!=exclude_intent_id)) if exclude_intent_id else account
        verdict = assess(proposal,assess_account,control,limits,market,now)
        if type(verdict) is not RiskVerdict or not verdict.allowed or verdict.account_revision!=account.revision or verdict.proposal_hash!=content_hash(proposal) or verdict.halt_transitions or verdict.escalation_codes:
            raise ContractViolation('current risk recheck refused/unbound')
        return account, cash, notional

    def _check_decision(self, proposal: OrderProposal, decision: DecisionReference | None) -> None:
        if decision != proposal.decision:
            raise ContractViolation('decision reference mismatch')
        if decision is None:
            if not proposal.reducing_only:
                raise ContractViolation('missing decision authority')
            return
        row = self.connection.execute('SELECT d.*,s.account_id,s.release_hash,s.snapshot_id,s.lineage_hash,s.valid,s.candle_close_us FROM decisions d JOIN snapshots s USING(snapshot_id) WHERE d.decision_id=?',(decision.decision_id,)).fetchone()
        if row is None or row['account_id']!=proposal.account_id or row['request_id']!=decision.request_id or row['release_hash']!=decision.release_hash or row['snapshot_id']!=decision.snapshot_hash or row['candle_close_us']!=proposal.candle_close_us or not row['valid'] or row['status']!='completed':
            raise ContractViolation('unproven decision reference')
        candidate = self.connection.execute('SELECT status FROM candidate_results WHERE decision_id=? AND candidate_id=?',(decision.decision_id,candidate_wire_id(proposal.candidate_id))).fetchone()
        if not proposal.reducing_only and (candidate is None or candidate[0]!='eligible'):
            raise ContractViolation('decision candidate not eligible')

    def reserve_intent(self, proposal: OrderProposal, expected: AccountRevision,
            generation: int, decision: DecisionReference | None) -> PreparedIntent:
        if type(proposal) is not OrderProposal or type(expected) is not AccountRevision:
            raise ContractViolation('typed proposal/revision required')
        with self.transaction() as c:
            wire_id = candidate_wire_id(proposal.candidate_id)
            existing = c.execute('SELECT intent_id,proposal_json,generation FROM order_intents WHERE account_id=? AND candle_close_us=? AND candidate_id=? AND action_slot=?',(proposal.account_id,proposal.candle_close_us,wire_id,proposal.action_slot)).fetchone()
            if existing:
                if existing['proposal_json']!=proposal.to_json() or existing['generation']!=generation or generation!=self._require_account(proposal.account_id)['generation']:
                    raise ContractViolation('economic action identity conflict')
                return self._prepared(existing['intent_id'])
            # Only the initial semantic slot is currently implemented. Replacement
            # and reduction ordinals are allocated by later authority integration.
            if proposal.action_slot!='entry' or proposal.reducing_only:
                raise ContractViolation('authority-allocated reduction/replacement slot unavailable')
            self._check_decision(proposal,decision)
            account,cash,notional = self._risk_check(proposal,expected,generation)
            economic_key = (proposal.account_id,proposal.candle_close_us,wire_id,proposal.action_slot)
            identity = content_hash(economic_key)
            intent_id, client_id = 'intent-'+identity, 'a'+identity
            now = self._clock_us()
            c.execute('INSERT INTO order_intents(intent_id,account_id,decision_id,candidate_id,release_hash,candle_close_us,action_slot,instrument,side,qty,limit_price,max_fee_usd_u,client_order_id,state,permit_epoch,generation,version,proposal_json,prepared_revision_json,recorded_at_us) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,\'reserved\',?,?,0,?,?,?)',
                (intent_id,proposal.account_id,decision.decision_id if decision else None,wire_id,proposal.release_hash,proposal.candle_close_us,proposal.action_slot,proposal.instrument,proposal.side.value,decimal_text(proposal.base_quantity),decimal_text(proposal.limit_price),proposal.max_fee_usd_u,client_id,proposal.control_epoch,generation,proposal.to_json(),account.revision.to_json(),now))
            c.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?,'reserved',0,?)",(intent_id,cash,decimal_text(proposal.base_quantity),notional,proposal.loss_budget_usd_u,decimal_text(proposal.base_quantity),now))
            self._append_order_evidence('reserved-'+identity,intent_id,'reserved',proposal.to_json(),now,now)
            result = self._prepared(intent_id)
        return result

    def mark_dispatching(self, intent_id: str, generation: int) -> PreparedIntent:
        # A returned dispatch capability promises a completed durable commit.
        if self.connection.in_transaction:
            raise ContractViolation('dispatch marker requires its own committed transaction')
        with self.transaction() as c:
            intent = self._prepared(intent_id)
            if intent.generation!=generation:
                raise ContractViolation('stale intent generation')
            self._check_decision(intent.proposal,intent.proposal.decision)
            self._risk_check(intent.proposal,None,generation,exclude_intent_id=intent_id)
            if intent.state is not IntentState.RESERVED:
                raise ContractViolation('intent has already left reserved; reconcile, never blindly redispatch')
            now = self._clock_us()
            self._append_order_evidence('dispatch-'+intent_id,intent_id,'dispatching',canonical_bytes({'generation':generation}).decode(),now,now)
            c.execute("UPDATE order_intents SET state='dispatching',version=version+1 WHERE intent_id=?",(intent_id,))
            c.execute("UPDATE reservations SET state='dispatching',version=version+1,updated_at_us=? WHERE intent_id=?",(now,intent_id))
            result = self._prepared(intent_id)
        return result

    @_decimal_boundary
    def append_order_event(self, event: OrderEvent) -> AppendResult:
        if type(event) is not OrderEvent:
            raise ContractViolation('typed order event required')
        with self.transaction() as c:
            intent = self._prepared(event.intent_id)
            account_id = intent.proposal.account_id
            if event.result.reference.account_id!=account_id or event.result.reference.client_order_id!=intent.client_order_id:
                raise ContractViolation('order event account/client identity mismatch')
            previous = c.execute('SELECT event_id,payload_json FROM order_events WHERE event_id=? OR (? IS NOT NULL AND venue_event_key=?)',(event.event_id,event.venue_event_key,event.venue_event_key)).fetchall()
            if previous:
                if len(previous)!=1 or previous[0]['payload_json']!=event.to_json():
                    raise ContractViolation('order event identity/content conflict')
                return AppendResult(False,previous[0]['event_id'],self._revision(account_id))
            if event.kind not in _TRANSITIONS.get(intent.state,set()):
                raise ContractViolation('invalid order transition; economic fills must use fill accounting')
            if event.result.filled_quantity+event.result.remaining_quantity!=intent.proposal.base_quantity:
                raise ContractViolation('order cumulative quantities disagree')
            if event.kind is IntentState.FILLED and event.result.remaining_quantity!=0:
                raise ContractViolation('filled event retains unfilled quantity')
            if event.kind is IntentState.PARTIAL and (event.result.filled_quantity<=0 or event.result.remaining_quantity<=0):
                raise ContractViolation('partial event lacks partial quantities')
            self._append_order_evidence(event.event_id,event.intent_id,event.kind.value,event.to_json(),event.received_at_us,self._clock_us(),event.event_at_us,event.venue_event_key)
            c.execute('UPDATE order_intents SET state=?,venue_order_id=?,version=version+1 WHERE intent_id=?',(event.kind.value,event.result.reference.venue_order_id,event.intent_id))
            c.execute('UPDATE reservations SET state=?,version=version+1,updated_at_us=? WHERE intent_id=?',(event.kind.value,self._clock_us(),event.intent_id))
            # Status never journals money or releases capacity. Confirmed fills,
            # final fees and terminal reconciliation own that atomic operation.
            if event.result.filled_quantity>0:
                self.invalidate_account(account_id)
            return AppendResult(True,event.event_id,self._revision(account_id))

    def _append_order_evidence(self, event_id: str, intent_id: str, kind: str,
            payload_json: str, received_at_us: int, recorded_at_us: int,
            event_at_us: int | None = None, venue_event_key: str | None = None) -> None:
        if not self.connection.in_transaction:
            raise ContractViolation('order evidence requires an immediate transaction')
        self.connection.execute('INSERT INTO order_events(event_id,append_sequence,intent_id,kind,venue_event_key,payload_json,event_at_us,received_at_us,recorded_at_us) SELECT ?,coalesce(max(append_sequence),0)+1,?,?,?,?,?,?,? FROM order_events',
            (event_id,intent_id,kind,venue_event_key,payload_json,event_at_us,received_at_us,recorded_at_us))

    def _record_revisions(self) -> None:
        """Commit a revision and an exact durable prefix for every evidence table.

        AccountRevision alone does not change for source/artifact imports. Their
        prefix changes still create a new history row. The history table itself
        is excluded explicitly to avoid an endlessly self-advancing watermark.
        Immutable tables retain rowids; mutable order/label/account rows export
        only stable identity fields or projections rebuilt from this evidence.
        """
        tables=tuple(table for table in _EXPORT_TIMES if table!='account_revision_history')
        watermarks={table:self.connection.execute('SELECT coalesce(max(rowid),0) FROM '+table).fetchone()[0] for table in tables}
        watermarks_json=canonical_bytes(watermarks).decode()
        # Use exactly the export visibility policies when establishing when the
        # whole committed prefix is knowable, including nested venue receipts.
        prefix_known=0
        for table in tables:
            for timestamp in _EXPORT_TIMES[table]:
                value=self.connection.execute('SELECT max('+timestamp+') FROM '+table+' WHERE rowid<=?',(watermarks[table],)).fetchone()[0]
                prefix_known=max(prefix_known,value or 0)
        nested_receipt=self.connection.execute("SELECT max(json_extract(payload_json,'$.result.received_at_us')) FROM order_events WHERE rowid<=?",(watermarks['order_events'],)).fetchone()[0]
        prefix_known=max(prefix_known,nested_receipt or 0)
        for account in self.connection.execute('SELECT account_id,generation FROM accounts').fetchall():
            account_id=account['account_id']
            revision=self._revision(account_id)
            previous=self.connection.execute('SELECT * FROM account_revision_history WHERE account_id=? ORDER BY append_sequence DESC LIMIT 1',(account_id,)).fetchone()
            if previous and (previous['journal_sequence'],previous['version'],previous['generation'],previous['evidence_watermarks_json'])==(revision.journal_sequence,revision.version,account['generation'],watermarks_json):
                continue
            recorded=self._clock_us()
            known=max(recorded,prefix_known,previous['known_at_us'] if previous else 0)
            # These mutable inputs are deliberately omitted from replay payload;
            # their revision still cannot precede current control/private proof.
            for sql in ('SELECT updated_at_us FROM account_control WHERE account_id=?',
                        'SELECT private_heartbeat_us FROM account_readiness WHERE account_id=?'):
                value=self.connection.execute(sql,(account_id,)).fetchone()[0]
                known=max(known,value or 0)
            self.connection.execute('INSERT INTO account_revision_history(account_id,journal_sequence,version,generation,evidence_watermarks_json,known_at_us,recorded_at_us) VALUES (?,?,?,?,?,?,?)',
                (account_id,revision.journal_sequence,revision.version,account['generation'],watermarks_json,known,recorded))

    @staticmethod
    def _manifest_entries(export: dict, tables: tuple[str,...]) -> tuple[ManifestEntry,...]:
        result=[]
        identities=set()
        for table in tables:
            keys=_MANIFEST_KEYS[table]
            for row in export.get(table,[]):
                # Composite keys retain all PK components in canonical order.
                key=row[keys[0]] if len(keys)==1 else canonical_bytes(tuple(row[k] for k in keys)).decode()
                identity=table+':'+str(key)
                if len(identity)>256:
                    identity=table+':sha256:'+content_hash(tuple(row[k] for k in keys))
                if identity in identities:
                    raise ContractViolation('duplicate manifest primary-key identity')
                identities.add(identity)
                result.append(ManifestEntry(identity,content_hash(row)))
        return tuple(sorted(result,key=lambda entry:entry.record_id))

    def append_review_job(self, job: ReviewJob) -> AppendResult:
        if type(job) is not ReviewJob:
            raise ContractViolation('typed review job required')
        with self.transaction() as c:
            previous = c.execute('SELECT * FROM review_jobs WHERE job_id=? OR dedupe_key=?',(job.job_id,job.dedupe_key)).fetchall()
            if previous:
                if len(previous)!=1 or previous[0]['job_json']!=job.to_json():
                    raise ContractViolation('review job identity/content conflict')
                return AppendResult(False,previous[0]['job_id'],self._job_revision(job))
            for table,key,value in (('review_sessions','session_id',job.session_id),('snapshots','snapshot_id',job.snapshot_id),('decisions','decision_id',job.decision_id)):
                if value is not None and not c.execute('SELECT 1 FROM '+table+' WHERE '+key+'=?',(value,)).fetchone():
                    raise ContractViolation('missing review job reference')
            c.execute('INSERT INTO review_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(job.job_id,job.dedupe_key,job.kind,job.session_id,job.snapshot_id,job.decision_id,job.status.value,job.payload_hash,job.attempts,job.next_attempt_us,job.to_json(),self._clock_us()))
            return AppendResult(True,job.job_id,self._job_revision(job))

    def _job_revision(self, job: ReviewJob) -> AccountRevision:
        accounts = set()
        for sql,value in (
                ('SELECT account_id FROM review_sessions WHERE session_id=?',job.session_id),
                ('SELECT account_id FROM snapshots WHERE snapshot_id=?',job.snapshot_id),
                ('SELECT s.account_id FROM decisions d JOIN snapshots s USING(snapshot_id) WHERE decision_id=?',job.decision_id)):
            if value is not None:
                row = self.connection.execute(sql,(value,)).fetchone()
                if row:
                    accounts.add(row[0])
        if len(accounts)>1:
            raise ContractViolation('cross-account review references')
        return self._revision(next(iter(accounts))) if accounts else AccountRevision()

    def export_session(self, cutoff: SessionCutoff) -> SessionArchive:
        if type(cutoff) is not SessionCutoff:
            raise ContractViolation('typed session cutoff required')
        with self.transaction() as c:
            self._require_account(cutoff.account_id)
            previous = c.execute('SELECT archive_json FROM review_sessions WHERE account_id=? AND cutoff_us=?',(cutoff.account_id,cutoff.cutoff_us)).fetchone()
            if previous:
                archive = SessionArchive.from_json(previous[0])
                if archive.cutoff!=cutoff:
                    raise ContractViolation('session cutoff identity reused with different reconciliation')
                return archive
            reconcile = c.execute('SELECT * FROM reconciliations WHERE reconcile_id=? AND account_id=?',(cutoff.reconcile_id,cutoff.account_id)).fetchone()
            if reconcile is None or reconcile['status']!='completed' or reconcile['finished_at_us'] is None or reconcile['finished_at_us']>cutoff.cutoff_us or reconcile['recorded_at_us']>cutoff.cutoff_us:
                raise ContractViolation('session lacks cutoff-consistent reconciliation')
            historical=c.execute('SELECT * FROM account_revision_history WHERE account_id=? AND recorded_at_us<=? AND known_at_us<=? ORDER BY append_sequence DESC LIMIT 1',
                (cutoff.account_id,cutoff.cutoff_us,cutoff.cutoff_us)).fetchone()
            if historical is None:
                raise ContractViolation('cutoff has no proven committed account prefix')
            watermarks=strict_json(historical['evidence_watermarks_json'])
            expected_tables=set(_EXPORT_TIMES)-{'account_revision_history'}
            if type(watermarks) is not dict or set(watermarks)!=expected_tables or any(type(value) is not int or value<0 for value in watermarks.values()):
                raise ContractViolation('invalid committed evidence watermark map')
            revision=AccountRevision(historical['journal_sequence'],historical['version'])
            # Serialize an actual consistent replay payload, not a hash of the
            # current revision. Account-bound tables are selected by account FKs;
            # shared causal source/artifact evidence may be included conservatively.
            export: dict[str,object] = {}
            account_id = cutoff.account_id
            tables = [row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for table in tables:
                if table in _EXPORT_OMIT:
                    continue
                if table not in _EXPORT_TIMES:
                    raise ContractViolation('missing explicit export visibility policy for '+table)
                columns = {r['name'] for r in c.execute('PRAGMA table_info('+table+')')}
                if table=='account_revision_history':
                    clauses,parameters=['append_sequence<=?'],[historical['append_sequence']]
                else:
                    clauses,parameters=['rowid<=?'],[watermarks[table]]
                if 'account_id' in columns:
                    clauses.append('account_id=?'); parameters.append(account_id)
                elif 'intent_id' in columns:
                    clauses.append('intent_id IN (SELECT intent_id FROM order_intents WHERE account_id=?)'); parameters.append(account_id)
                elif 'decision_id' in columns:
                    clauses.append('decision_id IN (SELECT d.decision_id FROM decisions d JOIN snapshots s USING(snapshot_id) WHERE s.account_id=?)'); parameters.append(account_id)
                elif 'snapshot_id' in columns:
                    clauses.append('snapshot_id IN (SELECT snapshot_id FROM snapshots WHERE account_id=?)'); parameters.append(account_id)
                elif 'transaction_id' in columns:
                    clauses.append('transaction_id IN (SELECT transaction_id FROM journal_transactions WHERE account_id=?)'); parameters.append(account_id)
                for timestamp in _EXPORT_TIMES[table]:
                    clauses.append('('+timestamp+' IS NULL OR '+timestamp+'<=?)')
                    parameters.append(cutoff.cutoff_us)
                if table=='order_events':
                    clauses.append("(json_extract(payload_json,'$.result.received_at_us') IS NULL OR json_extract(payload_json,'$.result.received_at_us')<=?)")
                    parameters.append(cutoff.cutoff_us)
                rows = [dict(r) for r in c.execute('SELECT * FROM '+table+(' WHERE '+' AND '.join(clauses) if clauses else ''),parameters)]
                export[table] = sorted(rows,key=canonical_bytes)
            if revision.journal_sequence!=max((row['sequence'] for row in export['journal_transactions']),default=0):
                raise ContractViolation('cutoff journal and committed prefix disagree')
            for row in export['accounts']:
                row['generation']=historical['generation']
            # Mutable order status is replayed from evidence at the cutoff;
            # copying today's projection would disclose a future acknowledgment.
            for row in export.get('order_intents',[]):
                events = sorted((event for event in export.get('order_events',[])
                    if event['intent_id']==row['intent_id']),
                    key=lambda event:event['append_sequence'])
                row['state']='reserved'
                row['venue_order_id']=None
                row['version']=0
                for event in events:
                    row['state']=event['kind']
                    row['version']+=int(event['kind']!='reserved')
                    payload=strict_json(event['payload_json'])
                    if type(payload) is dict and type(payload.get('result')) is dict:
                        row['venue_order_id']=payload['result']['reference']['venue_order_id']
            # Labels are mutable projections with no creation timestamp in the
            # approved schema. Export their stable identities plus causal events;
            # downstream label replay owns reconstructing revision/status/return.
            label_ids={row['label_id'] for row in export.get('label_events',[])}
            export['outcome_labels']=[{key:row[key] for key in
                ('label_id','forecast_id','label_version','maturity_us')}
                for row in export.get('outcome_labels',[]) if row['label_id'] in label_ids]
            # Every concrete reference in the export must resolve in the export,
            # not merely exist in a later live database state.
            for table,rows in export.items():
                foreign_keys=c.execute('PRAGMA foreign_key_list('+table+')').fetchall()
                for row in rows:
                    for foreign_key in foreign_keys:
                        source_column=foreign_key['from']
                        if source_column not in row or row[source_column] is None:
                            continue
                        targets=export.get(foreign_key['table'],[])
                        if not any(target.get(foreign_key['to'])==row[source_column] for target in targets):
                            raise ContractViolation('session cutoff lacks referenced '+foreign_key['table'])
            event_manifest = self._manifest_entries(export,('source_events','source_invalidations','event_segments'))
            forecast_manifest = self._manifest_entries(export,('forecasts',))
            outcome_manifest = self._manifest_entries(export,('outcome_labels','label_events'))
            artifact_manifest = self._manifest_entries(export,('release_bundles','evidence_manifests','evaluation_runs','snapshots','snapshot_inputs'))
            export_hash = content_hash(export)
            manifest_hash = content_hash((cutoff,revision,export_hash,event_manifest,forecast_manifest,outcome_manifest,artifact_manifest))
            session_id = 'session-'+content_hash((account_id,cutoff.cutoff_us))
            archive = SessionArchive(session_id,cutoff,revision,export_hash,event_manifest,forecast_manifest,outcome_manifest,artifact_manifest,manifest_hash)
            c.execute("INSERT INTO review_sessions VALUES (?,?,?,?,?,'completed',?,?,?)",(session_id,account_id,cutoff.cutoff_us,cutoff.reconcile_id,manifest_hash,archive.to_json(),canonical_bytes(export).decode(),self._clock_us()))
            return archive
