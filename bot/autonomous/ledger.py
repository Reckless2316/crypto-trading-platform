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
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterator

from .contracts import (
    AccountRevision, AccountView, AppendResult, Balance, ContractViolation,
    ControlState, DecisionReference, IntentState, ManifestEntry, MarketRules, Mode,
    OrderEvent, OrderProposal, Permit, PersistenceUnavailable, PreparedIntent,
    Reservation, RiskLimits, RiskVerdict, SessionArchive, SessionCutoff, ReviewJob,
    candidate_wire_id, canonical_bytes, checked_money, content_hash, decimal_text,
    decimal_value, strict_json, ExactRatio, ExactNavEvidence, Holding,
    MAX_DECIMAL_SCALE, StateUnavailable,
)

_MIGRATION = Path(__file__).resolve().parents[2] / 'data/migrations/001_autonomous_ledger.sql'
_MIGRATIONS = (_MIGRATION, _MIGRATION.with_name('002_autonomous_accounting.sql'))
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
    'accounting_evidence': ('event_at_us','received_at_us','known_at_us','recorded_at_us'),
    'accounting_operations': ('recorded_at_us',),
    'exact_marks': ('recorded_at_us',),
    'exact_flows': ('recorded_at_us',),
    'accounting_findings': ('recorded_at_us',),
    'accounting_rounding': ('recorded_at_us',),
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
_EXPORT_OMIT = {'accounting_state','schema_migrations','review_sessions','review_jobs','account_control',
    'account_readiness','balance_positions','candidate_control','reservations'}
# Only these explicitly introduced migration-002 tables have a proven empty
# prefix in a migration-001 history record. Future tables need their own policy.
_ACCOUNTING_EXPORT_TABLES = frozenset({'accounting_evidence','accounting_operations',
    'exact_marks','exact_flows','accounting_findings','accounting_rounding'})
_MANIFEST_KEYS = {
    'accounting_evidence': ('evidence_id',), 'accounting_operations': ('operation_id',),
    'exact_marks': ('mark_id',), 'exact_flows': ('flow_id',),
    'accounting_findings': ('finding_id',), 'accounting_rounding': ('rounding_id',),
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


def nav_after_flow(equity: Decimal, units: Decimal, flow: Decimal) -> tuple[Decimal,Decimal,Decimal]:
    for value in (equity,units,flow): decimal_text(value)
    e,u,f=map(Fraction,(equity,units,flow))
    if e<=0 or u<=0 or e+f<=0: raise ContractViolation('invalid account NAV')
    with localcontext(Context(prec=50)):
        return tuple(Decimal(v.numerator)/Decimal(v.denominator) for v in (e+f,u*(e+f)/e,e/u))


def _project(value: Fraction) -> Decimal:
    """38 significant digits / <=38 places, floor projection only; not authority."""
    return ExactRatio.of(value).projection()


def _native_posting_quantities(value: Fraction) -> tuple[str, ...]:
    """Serialize exact native changes as bounded integer/fractional legs.

    Two supported native quantities can have a difference wider than either
    quantity's 38 significant digits. Splitting at the decimal point preserves
    that difference without rounding any asset quantity.
    """
    scale=10**MAX_DECIMAL_SCALE
    scaled=abs(value)*scale
    if scaled.denominator!=1:raise ContractViolation('native posting scale exceeded')
    whole,fractional=divmod(scaled.numerator,scale)
    sign='-' if value<0 else ''
    raw=[]
    if whole:raw.append(sign+str(whole))
    if fractional:raw.append(sign+'0.'+str(fractional).rjust(MAX_DECIMAL_SCALE,'0'))
    legs=tuple(decimal_text(decimal_value(text)) for text in raw)
    if sum((Fraction(decimal_value(text)) for text in legs),Fraction(0))!=value:
        raise ContractViolation('native posting serialization mismatch')
    return legs


def _ratio(raw: str) -> Fraction:
    return ExactRatio.from_json(raw).fraction()


def _money(value: Fraction, *, ceiling=False) -> int:
    n,d=value.numerator,value.denominator
    return checked_money(-((-n)//d) if ceiling else n//d)



class Ledger:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.dispatch_enabled = True
        self._source_required = False
        self._source_engine = None
        self._risk_config: tuple | None = None
        self._transaction_failed = False
        self._clock_us: Callable[[], int] = lambda: time.time_ns() // 1000
        self._monotonic_ns: Callable[[], int] = time.monotonic_ns

    @property
    def source_entries_ready(self) -> bool:
        # A reopened source-bearing ledger requires archive recovery even before
        # the authority wires its StateEngine. An empty legacy paper fixture has
        # no source dependency until one is explicitly attached.
        if self._source_engine is None:
            return not self._source_required and not self.connection.execute('SELECT 1 FROM source_events LIMIT 1').fetchone()
        return self._source_engine.entries_ready

    def _bind_source_engine(self, engine) -> None:
        from .state import StateEngine
        if type(engine) is not StateEngine or engine.ledger is not self:
            raise ContractViolation('invalid source health owner')
        if self._source_engine is not None and self._source_engine is not engine:
            previous=self._source_engine
            if previous.archive.root!=engine.archive.root or previous.policies!=engine.policies:
                raise ContractViolation('source recovery configuration conflict')
        self._source_engine=engine

    def _check_source_health(self) -> None:
        if not self.source_entries_ready:
            raise StateUnavailable('causal source/archive recovery unavailable')

    def _source_lineage_invalid(self, snapshot_id: str) -> bool:
        # A delta checkpoint depends on the preceding full snapshot and every
        # intervening delta. Full resets break ancestry for later checkpoints,
        # while old frozen snapshots keep their original dependency forever.
        # Segment schema omits stream; resolve its concrete event candidates
        # conservatively by source/instrument/sequence, then follow each stream.
        snapshot=self.connection.execute('SELECT knowledge_cutoff_us FROM snapshots WHERE snapshot_id=?',(snapshot_id,)).fetchone()
        if snapshot is None:return False
        frozen_cutoff=snapshot[0]
        targets=self.connection.execute("""SELECT DISTINCT e.* FROM snapshot_inputs si
            JOIN source_events e ON e.event_id=si.event_id OR EXISTS (
                SELECT 1 FROM event_segments es WHERE es.segment_hash=si.segment_hash
                AND es.source=e.source AND es.instrument=e.instrument
                AND e.sequence BETWEEN es.first_sequence AND es.last_sequence)
            WHERE si.snapshot_id=?""",(snapshot_id,)).fetchall()
        for target in targets:
            if self.connection.execute("SELECT 1 FROM source_invalidations WHERE event_id=? AND (reason!='source_revision_superseded' OR known_at_us>? OR recorded_at_us>?)",
                    (target['event_id'],frozen_cutoff,frozen_cutoff)).fetchone():
                return True
            payload=strict_json(target['payload_json'])
            if type(payload) is not dict or 'is_snapshot' not in payload or target['sequence'] is None:
                continue
            invalid_ancestor=self.connection.execute("""SELECT 1 FROM source_invalidations i
                JOIN source_events ancestor ON ancestor.event_id=i.event_id
                WHERE ancestor.source=? AND ancestor.stream=? AND ancestor.instrument=?
                AND ancestor.sequence<=?
                AND (i.reason!='source_revision_superseded' OR i.known_at_us>? OR i.recorded_at_us>?)
                AND json_type(ancestor.payload_json,'$.is_snapshot') IN ('true','false')
                AND NOT EXISTS (SELECT 1 FROM source_events reset
                    WHERE reset.source=ancestor.source AND reset.stream=ancestor.stream
                    AND reset.instrument=ancestor.instrument
                    AND reset.sequence>ancestor.sequence AND reset.sequence<=?
                    AND json_extract(reset.payload_json,'$.is_snapshot')=1)
                LIMIT 1""",(target['source'],target['stream'],target['instrument'],
                    target['sequence'],frozen_cutoff,frozen_cutoff,target['sequence'])).fetchone()
            if invalid_ancestor:return True
        return False

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
        exists=self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        applied=self.connection.execute('SELECT version,checksum FROM schema_migrations ORDER BY version').fetchall() if exists else []
        expected=[(i,hashlib.sha256(path.read_bytes()).hexdigest()) for i,path in enumerate(_MIGRATIONS,1)]
        if [tuple(r) for r in applied]!=expected[:len(applied)] or len(applied)>len(expected):
            raise PersistenceUnavailable('migration checksum/version mismatch')
        if not exists and self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone():
            raise PersistenceUnavailable('refusing unrecognized database adoption')
        for (version,checksum),path in zip(expected[len(applied):],_MIGRATIONS[len(applied):]):
            try:
                self.connection.executescript('BEGIN IMMEDIATE;\n'+path.read_text()+
                    f"\nINSERT INTO schema_migrations VALUES({version},'{checksum}',{self._clock_us()});\nCOMMIT;")
            except sqlite3.Error:
                if self.connection.in_transaction:self.connection.rollback()
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
            for path in _MIGRATIONS:
                reference.executescript(path.read_text())
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
            self._validate_session_anchors(strict_json(row[2]),archive.cutoff,archive.account_revision)
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

        self._check_accounting_integrity()

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
        mark = self.connection.execute('SELECT * FROM equity_marks WHERE account_id=? ORDER BY rowid DESC LIMIT 1',(account_id,)).fetchone()
        reconcile = self.connection.execute('SELECT * FROM reconciliations WHERE reconcile_id=? AND account_id=?',(control['reconcile_id'],account_id)).fetchone()
        ready = bool(runtime['ready'] and mark and reconcile and reconcile['status']=='completed' and reconcile['finished_at_us'] is not None and runtime['private_heartbeat_us'] is not None and mark['valuation_version']==runtime['valuation_version'] and mark['equity_usd_u']>0)
        exact_row=self.connection.execute('SELECT * FROM exact_marks WHERE mark_id=?',(mark['mark_id'],)).fetchone() if mark else None
        revision=self._revision(account_id)
        exact=replace(ExactNavEvidence.from_json(exact_row['exact_json']),revision=revision) if exact_row else None
        holdings=tuple(Holding.from_dict(h) for h in strict_json(exact_row['holdings_json'])) if exact_row else ()
        state=self.connection.execute('SELECT * FROM accounting_state WHERE account_id=?',(account_id,)).fetchone()
        closed=bool(state and state['closed'])
        ready=ready and exact is not None and exact_row['journal_sequence']==revision.journal_sequence and exact_row['reconcile_id']==control['reconcile_id'] and bool(exact_row['ready'])
        ready=ready and not closed and not (state and state['unresolved'])
        now=self._clock_us()
        ready=ready and all(t is not None and 0<=now-t<=age for t,age in (
            (mark['marked_at_us'] if mark else None,2000000),
            (runtime['private_heartbeat_us'],5000000),
            (reconcile['finished_at_us'] if reconcile else None,15000000)))
        ready=ready and self._valuation_sources_current(account_id,exact_row['evidence_id'],holdings,now)
        ready=ready and {b.asset:b.quantity for b in balances if b.asset!='USD' and b.quantity}=={h.base_asset:h.quantity for h in holdings}
        if state:ready=ready and state['day_start_us']==now//86400000000*86400000000
        baseline = tuple(decimal_value(mark[k]) for k in ('units','nav','high_water_nav','day_open_nav')) if mark else (Decimal(0),)*4
        ready = ready and min(baseline)>0 and baseline[2]>=baseline[1]
        if closed:exact=None
        return AccountView(account_id=account_id,mode=Mode(account['mode']),revision=revision,
            liquidation_equity_usd_u=0 if closed else mark['equity_usd_u'] if mark else 0,
            units=baseline[0],nav=baseline[1],high_water_nav=baseline[2],day_open_nav=baseline[3],
            balances=balances,holdings=holdings,reservations=reservations,exact_nav=exact,
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
                from .risk import loss_triggers
                if any(loss_triggers(*(v.fraction() for v in (account.exact_nav.nav,account.exact_nav.high_water_nav,account.exact_nav.day_open_nav)))):
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
        if not proposal.reducing_only:self._check_source_health()
        if decision != proposal.decision:
            raise ContractViolation('decision reference mismatch')
        if decision is None:
            if not proposal.reducing_only:
                raise ContractViolation('missing decision authority')
            return
        row = self.connection.execute('SELECT d.*,s.account_id,s.release_hash,s.snapshot_id,s.lineage_hash,s.valid,s.candle_close_us FROM decisions d JOIN snapshots s USING(snapshot_id) WHERE d.decision_id=?',(decision.decision_id,)).fetchone()
        if row is None or row['account_id']!=proposal.account_id or row['request_id']!=decision.request_id or row['release_hash']!=decision.release_hash or row['snapshot_id']!=decision.snapshot_hash or row['candle_close_us']!=proposal.candle_close_us or not row['valid'] or row['status']!='completed':
            raise ContractViolation('unproven decision reference')
        if self._source_lineage_invalid(row['snapshot_id']):
            raise StateUnavailable('decision source lineage invalidated')
        candidate = self.connection.execute('SELECT status FROM candidate_results WHERE decision_id=? AND candidate_id=?',(decision.decision_id,candidate_wire_id(proposal.candidate_id))).fetchone()
        if not proposal.reducing_only and (candidate is None or candidate[0]!='eligible'):
            raise ContractViolation('decision candidate not eligible')

    def reserve_intent(self, proposal: OrderProposal, expected: AccountRevision,
            generation: int, decision: DecisionReference | None) -> PreparedIntent:
        if type(proposal) is not OrderProposal or type(expected) is not AccountRevision:
            raise ContractViolation('typed proposal/revision required')
        if not proposal.reducing_only:self._check_source_health()
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

    @staticmethod
    def _validate_session_anchors(export: object, cutoff: SessionCutoff,
            revision: AccountRevision) -> None:
        """The archive's own references must resolve inside its replay payload.

        A live-table FK cannot prove membership in the selected historical
        prefix. Apply this to new exports, cached archives and startup scans.
        """
        if type(export) is not dict:
            raise ContractViolation('invalid session replay payload')
        def rows(table: str) -> list[dict]:
            values=export.get(table,[])
            if type(values) is not list or any(type(row) is not dict for row in values):
                raise ContractViolation('invalid session replay table')
            return values
        accounts=[row for row in rows('accounts') if row.get('account_id')==cutoff.account_id]
        if len(accounts)!=1 or accounts[0]['created_at_us']>cutoff.cutoff_us:
            raise ContractViolation('session account anchor outside committed prefix')
        reconciliations=[row for row in rows('reconciliations')
            if row.get('reconcile_id')==cutoff.reconcile_id and row.get('account_id')==cutoff.account_id]
        if len(reconciliations)!=1:
            raise ContractViolation('session reconciliation anchor outside committed prefix')
        reconciliation=reconciliations[0]
        if reconciliation['status']!='completed' or reconciliation['finished_at_us'] is None or any(reconciliation[field]>cutoff.cutoff_us for field in _EXPORT_TIMES['reconciliations']):
            raise ContractViolation('session reconciliation anchor is not cutoff-complete')
        histories=[row for row in rows('account_revision_history')
            if row.get('account_id')==cutoff.account_id and row['known_at_us']<=cutoff.cutoff_us and row['recorded_at_us']<=cutoff.cutoff_us]
        if not histories:
            raise ContractViolation('session revision anchor outside committed prefix')
        historical=max(histories,key=lambda row:row['append_sequence'])
        if revision!=AccountRevision(historical['journal_sequence'],historical['version']) or accounts[0]['generation']!=historical['generation']:
            raise ContractViolation('session revision/generation anchor mismatch')

    def export_session(self, cutoff: SessionCutoff) -> SessionArchive:
        if type(cutoff) is not SessionCutoff:
            raise ContractViolation('typed session cutoff required')
        with self.transaction() as c:
            self._require_account(cutoff.account_id)
            previous = c.execute('SELECT archive_json,export_json FROM review_sessions WHERE account_id=? AND cutoff_us=?',(cutoff.account_id,cutoff.cutoff_us)).fetchone()
            if previous:
                archive = SessionArchive.from_json(previous[0])
                if archive.cutoff!=cutoff:
                    raise ContractViolation('session cutoff identity reused with different reconciliation')
                self._validate_session_anchors(strict_json(previous['export_json']),cutoff,archive.account_revision)
                return archive
            historical=c.execute('SELECT * FROM account_revision_history WHERE account_id=? AND recorded_at_us<=? AND known_at_us<=? ORDER BY append_sequence DESC LIMIT 1',
                (cutoff.account_id,cutoff.cutoff_us,cutoff.cutoff_us)).fetchone()
            if historical is None:
                raise ContractViolation('cutoff has no proven committed account prefix')
            watermarks=strict_json(historical['evidence_watermarks_json'])
            expected_tables=set(_EXPORT_TIMES)-{'account_revision_history'}
            if type(watermarks) is dict and set(watermarks)==expected_tables-_ACCOUNTING_EXPORT_TABLES:
                watermarks={**watermarks,**{table:0 for table in _ACCOUNTING_EXPORT_TABLES}}
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
            self._validate_session_anchors(export,cutoff,revision)
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

    # Accounting imports are deliberately paper-only. Task 13 must add an
    # authenticated acquisition adapter; raw JSON/Boolean/hash is not venue proof.
    def import_local_evidence(self, account_id: str, evidence_id: str, kind: str,
            payload: dict, *, event_at_us: int, received_at_us: int, known_at_us: int) -> str:
        _identifier(evidence_id)
        if kind not in {'initial','mark','fill','flow','correction','fx'} or type(payload) is not dict:
            raise ContractViolation('invalid accounting evidence kind/payload')
        if any(type(t) is not int or t<0 for t in (event_at_us,received_at_us,known_at_us)) or not event_at_us<=received_at_us<=known_at_us<=self._clock_us():
            raise ContractViolation('noncausal accounting evidence')
        raw=canonical_bytes(payload).decode()
        strict_json(raw)  # bound retained input before database mutation
        with self.transaction() as c:
            if self._require_account(account_id)['mode']!='paper':
                raise ContractViolation('local fixture is not authenticated venue evidence')
            identity=(account_id,kind,'local-paper',raw,content_hash(payload),event_at_us,received_at_us,known_at_us)
            previous=c.execute('SELECT * FROM accounting_evidence WHERE evidence_id=?',(evidence_id,)).fetchone()
            if previous:
                if tuple(previous[k] for k in ('account_id','kind','provenance','payload_json','payload_hash','event_at_us','received_at_us','known_at_us'))!=identity:
                    raise ContractViolation('evidence identity/content conflict')
            else:
                c.execute('INSERT INTO accounting_evidence VALUES (?,?,?,?,?,?,?,?,?,?)',(evidence_id,*identity,self._clock_us()))
            return evidence_id

    def _evidence(self, evidence_id: str, kind: str) -> tuple[sqlite3.Row,dict]:
        row=self.connection.execute('SELECT * FROM accounting_evidence WHERE evidence_id=?',(_identifier(evidence_id),)).fetchone()
        if row is None or row['kind']!=kind or row['known_at_us']>self._clock_us():
            raise ContractViolation('missing/wrong/future economic evidence')
        if self._require_account(row['account_id'])['mode']!='paper' or row['provenance']!='local-paper':
            raise ContractViolation('authenticated venue adapter not installed')
        payload=strict_json(row['payload_json'])
        if content_hash(payload)!=row['payload_hash']:raise ContractViolation('economic evidence hash mismatch')
        return row,payload

    def _finding(self, row: sqlite3.Row, reason: str) -> None:
        c=self.connection
        key='finding-'+content_hash((row['evidence_id'],reason))
        c.execute('INSERT OR IGNORE INTO accounting_findings VALUES (?,?,?,?,?)',(key,row['account_id'],row['evidence_id'],reason,self._clock_us()))
        c.execute('UPDATE accounting_state SET unresolved=1 WHERE account_id=?',(row['account_id'],))
        self.invalidate_account(row['account_id'])
        # Review incidents never silently disappear after a favorable correction.
        epoch=c.execute('SELECT permit_epoch FROM account_control WHERE account_id=?',(row['account_id'],)).fetchone()[0]
        c.execute("INSERT OR IGNORE INTO control_events VALUES (?,?,?,?,'review','accounting',?,?)",(key,row['account_id'],key,epoch,reason,self._clock_us()))
        c.execute("UPDATE account_control SET permit='none',version=version+1,halt_reason=?,updated_at_us=? WHERE account_id=?",(reason,self._clock_us(),row['account_id']))

    def _state(self, account_id: str) -> sqlite3.Row:
        row=self.connection.execute('SELECT * FROM accounting_state WHERE account_id=?',(account_id,)).fetchone()
        if row is None:raise ContractViolation('account economics not initialized')
        return row

    @staticmethod
    def _quantity(raw: str, *, positive=False) -> Fraction:
        value=Fraction(decimal_value(raw))
        if value<0 or (positive and value==0):raise ContractViolation('invalid native magnitude')
        return value

    def _fx(self, account_id: str, evidence_id: str, asset: str, quantity: Fraction, at_us: int) -> Fraction:
        row,p=self._evidence(evidence_id,'fx')
        if row['account_id']!=account_id or not 0<=at_us-row['event_at_us']<=2_000_000:
            raise ContractViolation('stale/wrong-account FX evidence')
        current=asset
        value=quantity
        legs=p.get('legs')
        if type(legs) is not list or not 1<=len(legs)<=4:raise ContractViolation('missing FX conversion legs')
        for leg in legs:
            if leg['from_asset']!=current:raise ContractViolation('disconnected FX direction')
            value*=self._quantity(leg['to_units_per_from_unit'],positive=True)
            current=leg['to_asset']
        if current!='USD':raise ContractViolation('FX does not resolve USD')
        return value*1000000

    def _valuation_sources_current(self, account_id: str, evidence_id: str,
            holdings: tuple[Holding,...], now: int) -> bool:
        """Age retained observations at admission, never at mark creation.

        A flow mark carries the preflow valuation forward without observing new
        depth, FX or private state. Follow that lineage to the actual observation.
        Historical marks remain valid history after their inputs expire.
        """
        seen=set()
        while evidence_id not in seen:
            seen.add(evidence_id)
            row=self.connection.execute('SELECT * FROM accounting_evidence WHERE evidence_id=? AND account_id=?',(evidence_id,account_id)).fetchone()
            if row is None:return False
            p=strict_json(row['payload_json'])
            if row['kind']=='mark':break
            if row['kind']!='flow':return False
            prior=self.connection.execute('SELECT evidence_id FROM exact_marks WHERE mark_id=? AND account_id=?',(p.get('preflow_mark_id'),account_id)).fetchone()
            if prior is None:return False
            evidence_id=prior['evidence_id']
        else:return False
        heartbeat=p.get('heartbeat_at_us')
        if not 0<=now-row['event_at_us']<=2_000_000 or type(heartbeat) is not int or not 0<=now-heartbeat<=5_000_000:
            return False
        for holding in holdings:
            if not 0<=now-holding.valuation_at_us<=2_000_000:return False
            book=p.get('books',{}).get(holding.base_asset)
            if book is None:return False
            quote=book.get('quote_asset','USD')
            if quote!='USD':
                try:self._fx(account_id,book.get('fx_id','missing-fx'),quote,Fraction(1),now)
                except ContractViolation:return False
        return True

    def _replay_economics(self, account_id: str, extra: tuple[str,dict,sqlite3.Row] | None = None):
        """Replay all acquisitions and disposals, including corrected old basis.

        Integer basis is allocated down on partial disposals; the final disposal
        takes the entire residual. Every discarded fraction is retained, bounded
        to <1 microdollar per allocation. No unexplained balance is rounded away.
        """
        rows=[]
        for op in self.connection.execute('SELECT * FROM accounting_operations WHERE account_id=? ORDER BY rowid',(account_id,)):
            e,p=self._evidence(op['evidence_id'],op['kind'])
            rows.append((op['kind'],p,e))
        if extra:rows.append(extra)
        replacements={}
        for kind,p,e in rows:
            if kind=='correction':replacements[p['fill_id']]=p['replacement']
        balances={}; totals={}; native={}; residuals=[]; filled={}; pending_fees=False
        def add(name,amount,asset=None,quantity=Fraction(0)):
            totals[name]=checked_money(totals.get(name,0)+amount)
            if asset is not None:native[name,asset]=native.get((name,asset),Fraction(0))+quantity
        def movement(asset,quantity,basis):
            owned,cost=balances.get(asset,(Fraction(0),0))
            owned+=quantity;cost=checked_money(cost+basis)
            if owned<0 or cost<0 or (owned==0 and cost):raise ContractViolation('unknown basis or insufficient owned asset')
            # Native projections must remain exactly representable within contract bounds.
            projected=_project(owned)
            if Fraction(projected)!=owned:raise ContractViolation('native precision exceeded')
            balances[asset]=(owned,cost)
        def disposal(asset,quantity):
            owned,cost=balances.get(asset,(Fraction(0),0))
            if quantity>owned or owned<=0:raise ContractViolation('insufficient asset for disposal/fee')
            exact=Fraction(cost)*quantity/owned
            basis=cost if quantity==owned else _money(exact)
            residuals.append(exact-basis)
            movement(asset,-quantity,-basis)
            return basis
        for kind,p,e in rows:
            if kind=='correction':continue
            if kind=='initial':
                seen=set()
                for b in p['balances']:
                    asset=_identifier(b['asset']);qty=self._quantity(b['quantity']);basis=checked_money(b['basis_usd_u'])
                    if asset in seen or basis<0 or (asset=='USD' and qty*1000000!=basis):raise ContractViolation('invalid opening basis')
                    seen.add(asset);movement(asset,qty,basis)
                    add('cash' if asset=='USD' else 'inventory:'+asset,basis,asset,qty)
                    add('capital',-basis,asset,-qty)
            elif kind=='flow':
                asset=_identifier(p['asset'])
                value=checked_money(p['usd_value_u']);fee=checked_money(p['fee_usd_u'])
                quantity=Fraction(decimal_value(p['quantity']))
                exact_value=quantity*1000000 if asset=='USD' else self._fx(account_id,p.get('fx_id','missing-fx'),asset,quantity,e['event_at_us'])
                if (asset=='USD' and exact_value.denominator!=1) or _money(exact_value)!=value or fee<0 or not quantity or (quantity>0)!=(value>0):
                    raise ContractViolation('flow quantity/value or fee mismatch')
                residuals.append(exact_value-value)
                if quantity>0:
                    basis=value;movement(asset,quantity,basis)
                else:
                    basis=-disposal(asset,-quantity)
                    add('realized_profit',basis-value)
                add('cash' if asset=='USD' else 'inventory:'+asset,basis,asset,quantity)
                add('capital',-value,asset,-quantity)
                if fee:movement('USD',-Fraction(fee,1000000),-fee)
                add('cash',-fee,'USD',-Fraction(fee,1000000))
                add('fee_expense',fee,'USD',Fraction(fee,1000000))
            elif kind=='fill':
                p=replacements.get(p['fill_id'],p)
                intent=self.connection.execute('SELECT * FROM order_intents WHERE intent_id=? AND account_id=?',(p['intent_id'],account_id)).fetchone()
                if intent is None or p['instrument']!=intent['instrument'] or p['side']!=intent['side'] or p['venue']!=self._require_account(account_id)['venue']:
                    raise ContractViolation('fill identity does not match account/intent')
                if not p['instrument'].endswith('-USD'):raise ContractViolation('USD execution quote required')
                asset=p['instrument'][:-4];base=self._quantity(p['base_quantity'],positive=True)
                quote=self._quantity(p['quote_quantity'],positive=True)*1000000
                if quote.denominator!=1:raise ContractViolation('settled quote exceeds microdollar precision')
                notional=checked_money(quote.numerator);price=self._quantity(p['price'],positive=True)
                fee=self._quantity(p['fee_quantity']);fee_asset=_identifier(p['fee_asset'])
                if type(p['fee_final']) is not bool:raise ContractViolation('explicit final-fee evidence required')
                pending_fees|=not p['fee_final']
                semantics=p['base_semantics']
                if semantics=='buy_net' and p['side']=='buy' and fee_asset==asset:base+=fee
                elif semantics!='gross':raise ContractViolation('unknown fill/native fee semantics')
                if abs(base*price*1000000-quote)>1:raise ContractViolation('settled quote inconsistency exceeds one microdollar')
                residuals.append(base*price*1000000-quote)
                if p['side']=='buy':
                    movement(asset,base,notional);movement('USD',-quote/1000000,-notional)
                    add('inventory:'+asset,notional,asset,base);add('cash',-notional,'USD',-quote/1000000)
                    add('settlement',0,asset,-base);add('settlement',0,'USD',quote/1000000)
                else:
                    basis=disposal(asset,base);movement('USD',quote/1000000,notional)
                    add('inventory:'+asset,-basis,asset,-base);add('cash',notional,'USD',quote/1000000)
                    add('realized_profit',-(notional-basis));add('settlement',0,asset,base);add('settlement',0,'USD',-quote/1000000)
                filled[p['intent_id']]=filled.get(p['intent_id'],Fraction(0))+base
                if fee:
                    if fee_asset=='USD':
                        exact_fee=fee*1000000
                        if exact_fee.denominator!=1:raise ContractViolation('USD fee precision exceeded')
                        value=checked_money(exact_fee.numerator);movement('USD',-fee,-value)
                        add('cash',-value,'USD',-fee);add('fee_expense',value,'USD',fee)
                    else:
                        basis=disposal(fee_asset,fee)
                        try:
                            exact_fee=fee*price*1000000 if fee_asset==asset else self._fx(account_id,p.get('fee_fx_id','missing-fx'),fee_asset,fee,e['event_at_us'])
                        except ContractViolation:
                            # Confirm native delivery and principal. Unvalued fees
                            # remain in suspense, never a fabricated zero expense.
                            pending_fees=True
                            add('inventory:'+fee_asset,-basis,fee_asset,-fee)
                            add('unvalued_fee',basis,fee_asset,fee)
                            continue
                        value=_money(exact_fee,ceiling=True)
                        residuals.append(Fraction(value)-exact_fee)
                        add('inventory:'+fee_asset,-basis,fee_asset,-fee);add('fee_expense',value,fee_asset,fee)
                        add('realized_profit',-(value-basis))
        if sum(totals.values())!=0:raise ContractViolation('economic replay is unbalanced')
        for asset in {a for _,a in native}:
            if sum(v for (_,a),v in native.items() if a==asset):raise ContractViolation('native asset conservation failure')
        return balances,totals,native,residuals,filled,pending_fees

    def _apply_economics(self, e: sqlite3.Row, p: dict, kind: str) -> str:
        account_id=e['account_id'];c=self.connection
        prior_filled=self._replay_economics(account_id)[4]
        balances,totals,native,residuals,filled,pending_fees=self._replay_economics(account_id,(kind,p,e))
        tx='economic-'+content_hash(e['evidence_id']);now=self._clock_us()
        sequence=c.execute('SELECT coalesce(max(sequence),0)+1 FROM journal_transactions').fetchone()[0]
        source_kind={'initial':'journal','fill':'fill','flow':'transfer','correction':'correction'}[kind]
        source_id=p['fill_id'] if kind=='fill' else e['evidence_id']
        c.execute('INSERT INTO journal_transactions VALUES (?,?,?,?,?,?,?)',(tx,sequence,account_id,source_kind,source_id,e['event_at_us'],now))
        prior_totals={r[0]:r[1] for r in c.execute('SELECT ledger_account,sum(usd_delta_u) FROM journal_postings JOIN journal_transactions USING(transaction_id) WHERE account_id=? GROUP BY ledger_account',(account_id,))}
        prior_native={}
        for r in c.execute('SELECT ledger_account,asset,native_delta FROM journal_postings JOIN journal_transactions USING(transaction_id) WHERE account_id=? AND asset IS NOT NULL',(account_id,)):
            prior_native[r[0],r[1]]=prior_native.get((r[0],r[1]),Fraction(0))+Fraction(decimal_value(r[2]))
        lines=[]
        for name in sorted(totals.keys()|prior_totals.keys()):
            delta=checked_money(totals.get(name,0)-prior_totals.get(name,0))
            if delta:lines.append((name,None,None,delta))
        for name,asset in sorted(native.keys()|prior_native.keys()):
            delta=native.get((name,asset),Fraction(0))-prior_native.get((name,asset),Fraction(0))
            lines.extend((name,asset,quantity,0) for quantity in _native_posting_quantities(delta))
        lines.extend([('rounding',None,None,0),('settlement',None,None,0)])
        validate_postings(tuple((name,amount) for name,_,_,amount in lines))
        for i,(name,asset,qty,amount) in enumerate(lines):c.execute('INSERT INTO journal_postings VALUES (?,?,?,?,?,?,?)',(tx,i,name,asset,qty,amount,now))
        serialized_native=prior_native.copy()
        for row in c.execute('SELECT ledger_account,asset,native_delta FROM journal_postings WHERE transaction_id=? AND asset IS NOT NULL',(tx,)):
            key=row['ledger_account'],row['asset']
            serialized_native[key]=serialized_native.get(key,Fraction(0))+Fraction(decimal_value(row['native_delta']))
        if {k:v for k,v in serialized_native.items() if v}!={k:v for k,v in native.items() if v}:
            raise ContractViolation('serialized native journal disagrees with exact replay')
        for i,residual in enumerate(residuals):
            if abs(residual)>1:raise ContractViolation('unbounded monetary rounding')
            c.execute('INSERT INTO accounting_rounding VALUES (?,?,?,?,?)',(tx+'-'+str(i),tx,ExactRatio.of(residual).to_json(),'partial basis floor/final residual; fee ceiling; settled quote <=1 microdollar',now))
        for asset,(qty,basis) in balances.items():
            c.execute('INSERT INTO balance_positions VALUES (?,?,?,?,?,1) ON CONFLICT(account_id,asset) DO UPDATE SET native_qty=excluded.native_qty,cost_basis_usd_u=excluded.cost_basis_usd_u,journal_sequence=excluded.journal_sequence,version=balance_positions.version+1',(account_id,asset,decimal_text(_project(qty)),basis,sequence))
        c.execute('INSERT INTO accounting_operations VALUES (?,?,?,?,?,?)',(e['evidence_id'],account_id,e['evidence_id'],kind,tx,now))
        self.invalidate_account(account_id)
        for intent_id,total in filled.items():
            # Full-history replay rebuilds balances, but only a changed cumulative
            # fill belongs to this operation. Unchanged terminal history must not
            # recreate reservations or report another late-fill incident.
            if total==prior_filled.get(intent_id,Fraction(0)):continue
            intent=c.execute('SELECT * FROM order_intents WHERE intent_id=?',(intent_id,)).fetchone()
            proposal=OrderProposal.from_json(intent['proposal_json'])
            original=Fraction(decimal_value(intent['qty']));remaining=max(Fraction(0),original-total)
            # Retain all final-fee capacity and modeled-loss capacity until Task13
            # proves terminal cumulative settlement. Principal alone is consumed.
            gross=_money(remaining*Fraction(decimal_value(intent['limit_price']))*1000000,ceiling=True)
            cash=gross+intent['max_fee_usd_u'] if intent['side']=='buy' else 0
            c.execute('UPDATE reservations SET cash_usd_u=?,base_qty=?,gross_usd_u=?,remaining_qty=?,version=version+1,updated_at_us=? WHERE intent_id=?',(checked_money(cash),decimal_text(_project(remaining)),gross,decimal_text(_project(remaining)),now,intent_id))
            if total>original or intent['state'] in ('filled','canceled','rejected'):
                self._finding(e,'late fill or cumulative order disagreement')
        if pending_fees:self._finding(e,'final fees incomplete')
        return tx

    def initialize_account(self, evidence_id: str) -> bool:
        with self.transaction() as c:
            e,p=self._evidence(evidence_id,'initial');account_id=e['account_id']
            if c.execute('SELECT 1 FROM accounting_operations WHERE evidence_id=?',(evidence_id,)).fetchone():return False
            if c.execute('SELECT 1 FROM accounting_state WHERE account_id=?',(account_id,)).fetchone() or self._revision(account_id).journal_sequence:
                raise ContractViolation('initialization cannot reset existing economics')
            units=self._quantity(p['units'],positive=True)
            zero=ExactRatio.of(Fraction(0)).to_json()
            c.execute('INSERT INTO accounting_state(account_id,units_json,high_water_json,day_open_json,closed,unresolved) VALUES (?,?,?,?,0,0)',(account_id,ExactRatio.of(units).to_json(),zero,zero))
            self._apply_economics(e,p,'initial')
            return True

    def record_fill(self, evidence_id: str) -> bool:
        with self.transaction() as c:
            e,p=self._evidence(evidence_id,'fill');self._state(e['account_id'])
            previous=c.execute('SELECT * FROM fills WHERE fill_id=? OR (account_id=? AND venue=? AND venue_trade_id=?)',(p['fill_id'],e['account_id'],p['venue'],p['trade_id'])).fetchone()
            if previous:
                if previous['payload_hash']==e['payload_hash']:return False
                self._finding(e,'changed fill identity requires verified correction')
                return False
            try:
                self._replay_economics(e['account_id'],('fill',p,e))
            except (ContractViolation,KeyError,TypeError) as exc:
                self._finding(e,'unaccounted fill: '+str(exc))
                return False
            # Normalize database projections with the same rule startup checks;
            # the retained source payload/hash remains the idempotency identity.
            base,quote,price,fee=(decimal_text(decimal_value(p[key])) for key in ('base_quantity','quote_quantity','price','fee_quantity'))
            c.execute('INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(p['fill_id'],e['account_id'],p['intent_id'],p['venue'],p['trade_id'],p['order_id'],p['instrument'],p['side'],base,quote,price,p['fee_asset'],fee,e['event_at_us'],e['received_at_us'],e['payload_hash'],self._clock_us()))
            self._apply_economics(e,p,'fill')
            self._mark_after_economics(e)
            return True

    def correct_fill(self, evidence_id: str) -> bool:
        with self.transaction() as c:
            e,p=self._evidence(evidence_id,'correction')
            previous=c.execute('SELECT * FROM fill_corrections WHERE correction_id=?',(evidence_id,)).fetchone()
            if previous:return False  # imported evidence ID already binds complete content
            original=c.execute('SELECT * FROM fills WHERE fill_id=? AND account_id=?',(p['fill_id'],e['account_id'])).fetchone()
            if original is None:raise ContractViolation('missing original fill')
            last=c.execute('SELECT corrected_payload_hash FROM fill_corrections WHERE fill_id=? ORDER BY rowid DESC LIMIT 1',(p['fill_id'],)).fetchone()
            if p['previous_payload_hash']!=(last[0] if last else original['payload_hash']):raise ContractViolation('stale correction predecessor')
            replacement=p['replacement']
            if any(replacement[k]!=original[column] for k,column in (('fill_id','fill_id'),('intent_id','intent_id'),('venue','venue'),('trade_id','venue_trade_id'),('order_id','venue_order_id'),('instrument','instrument'),('side','side'))):
                raise ContractViolation('correction changes immutable trade identity')
            # Unsupported factual corrections must inhibit, while all attempted
            # compensation is discarded. Keep the incident outside this savepoint
            # so unknown basis or serialization bounds cannot leave stale readiness.
            c.execute('SAVEPOINT correction_economics')
            try:
                c.execute('INSERT INTO fill_corrections VALUES (?,?,?,?,?,NULL,?,?,?)',(evidence_id,p['fill_id'],p['previous_payload_hash'],canonical_bytes(replacement).decode(),content_hash(replacement),e['received_at_us'],'verified local paper correction',self._clock_us()))
                self._apply_economics(e,p,'correction')
            except (ContractViolation,KeyError,TypeError) as exc:
                c.execute('ROLLBACK TO correction_economics')
                c.execute('RELEASE correction_economics')
                self._finding(e,'unaccounted correction: '+str(exc))
                return False
            c.execute('RELEASE correction_economics')
            self._finding(e,'fill correction requires fresh economic review')
            self._mark_after_economics(e)
            return True

    def _latch_losses(self, account_id: str, mark_id: str, nav: Fraction, high: Fraction, day: Fraction, *, rollover=False) -> None:
        from .risk import loss_triggers
        if min(high,day)<=0:return
        drawdown,daily=loss_triggers(nav,high,day) if nav>0 else (True,True)
        c=self.connection;now=self._clock_us()
        old=c.execute('SELECT * FROM account_control WHERE account_id=?',(account_id,)).fetchone()
        daily_next=int(daily or (old['daily_halted'] and not rollover))
        drawdown_next=int(drawdown or old['drawdown_latched'])
        if (drawdown_next,daily_next)==(old['drawdown_latched'],old['daily_halted']):return
        key='loss-'+content_hash(mark_id)
        c.execute("UPDATE account_control SET drawdown_latched=?,daily_halted=?,version=version+1,updated_at_us=? WHERE account_id=?",(drawdown_next,daily_next,now,account_id))
        c.execute("INSERT INTO control_events VALUES (?,?,?,?,'risk_halt','accounting',?,?)",(key,account_id,key,old['permit_epoch'],'exact NAV loss boundary/UTC rollover',now))

    def _write_mark(self, e: sqlite3.Row, mark_id: str, equity: int, units: Fraction,
            high: Fraction, day: Fraction, holdings: tuple[Holding,...], reconcile_id: str | None,
            ready: bool, *, rollover=False) -> str:
        c=self.connection;account_id=e['account_id'];now=self._clock_us()
        nav=Fraction(equity,1000000)/units if units>0 else Fraction(0)
        high=max(high,nav)
        valuation=c.execute('SELECT valuation_version FROM account_readiness WHERE account_id=?',(account_id,)).fetchone()[0]+1
        dd=max(Fraction(0),1-nav/high) if high>0 else Fraction(0)
        dl=max(Fraction(0),1-nav/day) if day>0 else Fraction(0)
        values=[decimal_text(_project(v)) for v in (units,max(nav,Fraction(0)),max(high,Fraction(0)),max(day,Fraction(0)),dd,dl)]
        c.execute('INSERT INTO equity_marks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',(mark_id,account_id,e['payload_hash'],checked_money(equity),*values,valuation,e['event_at_us'],now))
        c.execute('UPDATE accounting_state SET units_json=?,high_water_json=?,day_open_json=?,last_mark_id=? WHERE account_id=?',(ExactRatio.of(units).to_json(),ExactRatio.of(high).to_json(),ExactRatio.of(day).to_json(),mark_id,account_id))
        c.execute('UPDATE account_readiness SET ready=?,valuation_version=?,version=version+1 WHERE account_id=?',(int(ready),valuation,account_id))
        self._latch_losses(account_id,mark_id,nav,high,day,rollover=rollover)
        if min(units,nav,high,day)>0:
            exact=ExactNavEvidence(account_id,mark_id,self._revision(account_id),valuation,equity,
                *(ExactRatio.of(v) for v in (units,nav,high,day)),e['payload_hash'])
            c.execute('INSERT INTO exact_marks VALUES (?,?,?,?,?,?,?,?,?)',(mark_id,account_id,e['evidence_id'],exact.to_json(),canonical_bytes(holdings).decode(),exact.revision.journal_sequence,reconcile_id,int(ready),now))
        else:
            c.execute('UPDATE account_readiness SET ready=0 WHERE account_id=?',(account_id,))
        return mark_id

    def record_mark(self, evidence_id: str, *, _derived: bool = False) -> str:
        with self.transaction() as c:
            e,p=self._evidence(evidence_id,'mark');account_id=e['account_id'];state=self._state(account_id)
            mark_id='mark-'+content_hash(evidence_id)
            if c.execute('SELECT 1 FROM equity_marks WHERE mark_id=?',(mark_id,)).fetchone():return mark_id
            now=self._clock_us();reasons=[];holdings=[]
            balances=list(c.execute('SELECT * FROM balance_positions WHERE account_id=? ORDER BY asset',(account_id,)))
            actual={r['asset']:r['native_qty'] for r in balances}
            if p['balances']!=actual:reasons.append('all-holdings reconciliation differs')
            actual_orders=[r[0] for r in c.execute("SELECT intent_id FROM order_intents WHERE account_id=? AND state NOT IN ('filled','canceled','rejected') ORDER BY intent_id",(account_id,))]
            if p['open_intent_ids']!=actual_orders:reasons.append('open-order reconciliation differs')
            heartbeat=p['heartbeat_at_us']
            if type(heartbeat) is not int or not 0<=e['event_at_us']-heartbeat<=5_000_000 or heartbeat>e['known_at_us']:reasons.append('missing/stale/future private observation')
            current_fresh=type(heartbeat) is int and 0<=now-heartbeat<=5_000_000 and 0<=now-e['event_at_us']<=2_000_000
            # A retained midnight observation may restore that baseline after an
            # outage only if economics have not changed since the observation.
            if c.execute('SELECT 1 FROM journal_transactions WHERE account_id=? AND (event_at_us>? OR recorded_at_us>?) LIMIT 1',(account_id,e['event_at_us'],e['event_at_us'])).fetchone():
                reasons.append('historical valuation does not cover current economics')
            liabilities=checked_money(p['liabilities_usd_u']);exit_cost=checked_money(p['exit_cost_usd_u'])
            if min(liabilities,exit_cost)<0:raise ContractViolation('negative liabilities/exit costs')
            equity=-liabilities-exit_cost
            books=p['books']
            if type(books) is not dict:raise ContractViolation('invalid depth evidence')
            for balance in balances:
                asset=balance['asset'];quantity=Fraction(decimal_value(balance['native_qty']))
                if asset=='USD':
                    equity+=_money(quantity*1000000);continue
                if quantity==0:continue
                book=books.get(asset)
                if not book or type(book.get('at_us')) is not int or not 0<=e['event_at_us']-book['at_us']<=2_000_000 or book['at_us']>e['known_at_us']:
                    reasons.append('missing/stale executable depth for '+asset);continue
                remaining=quantity;value=Fraction(0);prior=None
                bids=book.get('bids')
                if type(bids) is not list or len(bids)>10000:raise ContractViolation('bounded executable bids required')
                for level in bids:
                    if type(level) is not list or len(level)!=2:raise ContractViolation('invalid executable bid')
                    price=self._quantity(level[0]);size=self._quantity(level[1])
                    if prior is not None and price>prior:raise ContractViolation('unsorted liquidation bids')
                    prior=price;take=min(remaining,size);value+=take*price;remaining-=take
                    if remaining==0:break
                if remaining:reasons.append('insufficient executable depth for '+asset);continue
                quote=book.get('quote_asset','USD')
                try:
                    usd=value*1000000 if quote=='USD' else self._fx(account_id,book.get('fx_id','missing-fx'),quote,value,e['event_at_us'])
                except ContractViolation:
                    reasons.append('missing/stale verified liquidation FX for '+asset)
                    continue
                marked=_money(usd)
                equity=checked_money(equity+marked)
                holdings.append(Holding(asset+'-'+quote,asset,decimal_value(balance['native_qty']),balance['cost_basis_usd_u'],marked,book['at_us'],e['payload_hash']))
            units=_ratio(state['units_json']);high=_ratio(state['high_water_json']);day=_ratio(state['day_open_json'])
            if state['closed']:reasons.append('account closed')
            if units<=0:reasons.append('nonpositive account units')
            day_start=e['event_at_us']//86400000000*86400000000
            rollover=False;boundary_missing=False
            if state['day_start_us']!=day_start:
                if e['event_at_us']!=day_start or reasons or _derived or equity<=0:
                    boundary_missing=True
                else:
                    day=Fraction(equity,1000000)/units
                    c.execute('UPDATE accounting_state SET day_start_us=?,rollover_pending=1 WHERE account_id=?',(day_start,account_id))
                    rollover=True
            if reasons:
                for reason in reasons:
                    c.execute('INSERT OR IGNORE INTO accounting_findings VALUES (?,?,?,?,?)',('mark-finding-'+content_hash((evidence_id,reason)),account_id,evidence_id,reason,now))
                self.invalidate_account(account_id)
                # Incomplete liquidation must not invent zero values or authoritative
                # loss marks. Preserve prior immutable mark and expose unready state.
                return mark_id
            ready=not _derived and current_fresh and not boundary_missing and not state['unresolved'] and equity>0 and self._valuation_sources_current(account_id,evidence_id,tuple(holdings),now)
            if boundary_missing:
                c.execute('INSERT OR IGNORE INTO accounting_findings VALUES (?,?,?,?,?)',('boundary-'+content_hash(evidence_id),account_id,evidence_id,'missing genuine UTC boundary valuation',now))
            reconcile=None
            if ready:
                reconcile='reconcile-'+content_hash(evidence_id)
                c.execute("INSERT INTO reconciliations VALUES (?,?,?,?,NULL,'completed',?,?, '[]',?)",(reconcile,account_id,e['event_at_us'],e['received_at_us'],canonical_bytes(p['balances']).decode(),canonical_bytes(p['open_intent_ids']).decode(),now))
                c.execute('UPDATE account_control SET reconcile_id=?,version=version+1,updated_at_us=? WHERE account_id=?',(reconcile,now,account_id))
                c.execute('UPDATE account_readiness SET private_heartbeat_us=? WHERE account_id=?',(heartbeat,account_id))
            reset_daily=ready and (rollover or bool(state['rollover_pending']))
            result=self._write_mark(e,mark_id,equity,units,high,day,tuple(holdings),reconcile,ready,rollover=reset_daily)
            if ready:c.execute('UPDATE accounting_state SET rollover_pending=0 WHERE account_id=?',(account_id,))
            return result

    def _mark_after_economics(self, event: sqlite3.Row) -> None:
        """Reuse still-current executable depth, never prior reconciliation proof.

        A derived mark can latch loss and can never make an account ready. Its
        payload binds both the original observation and the new economic event.
        """
        state=self._state(event['account_id'])
        last=self.connection.execute('SELECT * FROM exact_marks WHERE mark_id=?',(state['last_mark_id'],)).fetchone()
        if last is None:return
        # Flow marks have no depth of their own. Preserve the most recent retained
        # depth observation across flows; its original timestamps still expire.
        source=self.connection.execute("SELECT e.* FROM exact_marks m JOIN accounting_evidence e USING(evidence_id) WHERE m.account_id=? AND e.kind='mark' ORDER BY m.rowid DESC LIMIT 1",(event['account_id'],)).fetchone()
        if source is None:return
        p=strict_json(source['payload_json'])
        p['derived_from']=source['evidence_id'];p['economic_event']=event['evidence_id']
        p['balances']={r['asset']:r['native_qty'] for r in self.connection.execute('SELECT * FROM balance_positions WHERE account_id=?',(event['account_id'],))}
        p['open_intent_ids']=[r[0] for r in self.connection.execute("SELECT intent_id FROM order_intents WHERE account_id=? AND state NOT IN ('filled','canceled','rejected') ORDER BY intent_id",(event['account_id'],))]
        key='derived-'+content_hash(event['evidence_id'])
        now=self._clock_us()
        self.import_local_evidence(event['account_id'],key,'mark',p,event_at_us=now,received_at_us=now,known_at_us=now)
        self.record_mark(key,_derived=True)

    def record_flow(self, evidence_id: str) -> bool:
        with self.transaction() as c:
            e,p=self._evidence(evidence_id,'flow');account_id=e['account_id'];state=self._state(account_id)
            previous=c.execute('SELECT * FROM external_flows WHERE account_id=? AND venue_transfer_key=?',(account_id,p['transfer_key'])).fetchone()
            if previous:
                operation=c.execute('SELECT evidence_id FROM accounting_operations WHERE transaction_id=?',(previous['transaction_id'],)).fetchone()
                original=c.execute('SELECT payload_hash FROM accounting_evidence WHERE evidence_id=?',(operation[0],)).fetchone()[0]
                if original!=e['payload_hash']:self._finding(e,'changed transfer identity')
                return False
            mark=c.execute('SELECT * FROM equity_marks WHERE mark_id=? AND account_id=?',(p['preflow_mark_id'],account_id)).fetchone()
            exact=c.execute('SELECT * FROM exact_marks WHERE mark_id=?',(p['preflow_mark_id'],)).fetchone()
            value=checked_money(p['usd_value_u']);fee=checked_money(p['fee_usd_u'])
            if mark is None or exact is None or state['last_mark_id']!=mark['mark_id']:
                self._finding(e,'transfer lacks current complete preflow valuation');return False
            authority=ExactNavEvidence.from_json(exact['exact_json']);old_units=authority.units.fraction()
            if state['closed']:
                if value<=0:self._finding(e,'closed account requires positive redeposit');return False
                # Closure retains its exact last positive NAV as the issuance
                # baseline, not as current valuation/reconciliation proof. It
                # cannot be refreshed while units are zero. Recovery instead
                # requires a current verified transfer and stays unready.
                if not 0<=self._clock_us()-e['event_at_us']<=2_000_000:
                    self._finding(e,'closed account requires fresh redeposit evidence');return False
                units=Fraction(value,1000000)/authority.nav.fraction();equity=value-fee;delta=units
            else:
                holdings=tuple(Holding.from_dict(h) for h in strict_json(exact['holdings_json']))
                if not 0<=self._clock_us()-mark['marked_at_us']<=2_000_000 or exact['journal_sequence']!=self._revision(account_id).journal_sequence or not self._valuation_sources_current(account_id,exact['evidence_id'],holdings,self._clock_us()):
                    self._finding(e,'transfer lacks current complete preflow valuation');return False
                before=mark['equity_usd_u'];equity=checked_money(before+value-fee)
                if before+value<0 or equity<0:self._finding(e,'transfer exceeds liquidation equity');return False
                delta=old_units*Fraction(value,before);units=old_units+delta
            try:
                unit_record=ExactRatio.of(units);delta_record=ExactRatio.of(delta)
                _project(delta);_project(units)
                if units>0:
                    _project(Fraction(equity,1000000)/units)
            except (ContractViolation,DecimalException):
                self._finding(e,'exact rational/display resource exhaustion; factual transfer retained');return False
            closed=units==0
            if closed and (fee or equity or any(r['asset']!='USD' and decimal_value(r['native_qty']) for r in c.execute('SELECT * FROM balance_positions WHERE account_id=?',(account_id,)))):
                self._finding(e,'closure must settle all assets and costs');return False
            try:
                self._replay_economics(account_id,('flow',p,e))
            except (ContractViolation,KeyError,TypeError) as exc:
                self._finding(e,'unaccounted transfer: '+str(exc));return False
            tx=self._apply_economics(e,p,'flow')
            c.execute('INSERT INTO external_flows VALUES (?,?,?,?,?,?,?,?,?,?,?)',(evidence_id,account_id,p['transfer_key'],p['asset'],decimal_text(decimal_value(p['quantity'])),value,decimal_text(_project(delta)),mark['mark_id'],tx,e['event_at_us'],self._clock_us()))
            c.execute('INSERT INTO exact_flows VALUES (?,?,?,?,?,?,?)',(evidence_id,account_id,mark['mark_id'],delta_record.to_json(),unit_record.to_json(),int(closed),self._clock_us()))
            c.execute('UPDATE accounting_state SET closed=?,units_json=? WHERE account_id=?',(int(closed),unit_record.to_json(),account_id))
            if not closed:
                holdings=tuple(Holding.from_dict(h) for h in strict_json(exact['holdings_json']))
                if p['asset']=='USD':
                    self._write_mark(e,'flow-mark-'+content_hash(evidence_id),equity,units,authority.high_water_nav.fraction(),authority.day_open_nav.fraction(),holdings,None,False)
                else:
                    self._mark_after_economics(e)
            return True

    def _check_accounting_integrity(self) -> None:
        c=self.connection
        for row in c.execute('SELECT * FROM accounting_evidence'):
            if content_hash(strict_json(row['payload_json']))!=row['payload_hash'] or not row['event_at_us']<=row['received_at_us']<=row['known_at_us']:
                raise PersistenceUnavailable('accounting source integrity failure')
        for row in c.execute('SELECT * FROM exact_marks'):
            exact=ExactNavEvidence.from_json(row['exact_json'])
            mark=c.execute('SELECT * FROM equity_marks WHERE mark_id=?',(row['mark_id'],)).fetchone()
            if exact.account_id!=row['account_id'] or exact.mark_id!=row['mark_id'] or exact.equity_usd_u!=mark['equity_usd_u'] or exact.valuation_version!=mark['valuation_version'] or exact.lineage_hash!=mark['lineage_hash'] or exact.revision.journal_sequence!=row['journal_sequence']:
                raise PersistenceUnavailable('exact mark lineage mismatch')
            for key in ('units','nav','high_water_nav','day_open_nav'):
                if _project(getattr(exact,key).fraction())!=decimal_value(mark[key]):raise PersistenceUnavailable('exact mark projection mismatch')
            tuple(Holding.from_dict(h) for h in strict_json(row['holdings_json']))
        closed_accounts={}
        for row in c.execute('SELECT * FROM exact_flows ORDER BY rowid'):
            delta=_ratio(row['units_delta_json']);after=_ratio(row['units_after_json'])
            flow=c.execute('SELECT * FROM external_flows WHERE flow_id=?',(row['flow_id'],)).fetchone()
            mark=c.execute('SELECT exact_json FROM exact_marks WHERE mark_id=?',(row['preflow_mark_id'],)).fetchone()
            prior=ExactNavEvidence.from_json(mark[0])
            expected_delta=prior.units.fraction()*Fraction(flow['usd_value_u'],prior.equity_usd_u)
            expected_after=expected_delta if closed_accounts.get(row['account_id'],False) else prior.units.fraction()+expected_delta
            if row['account_id']!=flow['account_id'] or row['account_id']!=prior.account_id or row['preflow_mark_id']!=flow['preflow_mark_id'] or delta!=expected_delta or after!=expected_after or _project(delta)!=decimal_value(flow['units_delta']) or after<0 or bool(after==0)!=bool(row['closed']):
                raise PersistenceUnavailable('exact flow lineage mismatch')
            closed_accounts[row['account_id']]=bool(row['closed'])
        for row in c.execute('SELECT exact_residual_json FROM accounting_rounding'):
            if abs(_ratio(row[0]))>1:raise PersistenceUnavailable('rounding residual out of bounds')
        for state in c.execute('SELECT * FROM accounting_state'):
            for key in ('units_json','high_water_json','day_open_json'):_ratio(state[key])
            # Existing raw Task2 test fixtures have no accounting state. Accounts
            # initialized through this seam must reconstruct exactly at startup.
            account_id=state['account_id']
            flow=c.execute('SELECT units_after_json,closed FROM exact_flows WHERE account_id=? ORDER BY rowid DESC LIMIT 1',(account_id,)).fetchone()
            if flow:
                expected_units=_ratio(flow['units_after_json'])
                expected_closed=flow['closed']
            else:
                initial=c.execute("SELECT e.payload_json FROM accounting_operations o JOIN accounting_evidence e USING(evidence_id) WHERE o.account_id=? AND o.kind='initial'",(account_id,)).fetchone()
                expected_units=Fraction(decimal_value(strict_json(initial[0])['units']))
                expected_closed=0
            if _ratio(state['units_json'])!=expected_units or state['closed']!=expected_closed:
                raise PersistenceUnavailable('unit/closure projection mismatch')
            last=c.execute('SELECT exact_json FROM exact_marks WHERE account_id=? ORDER BY rowid DESC LIMIT 1',(account_id,)).fetchone()
            if last:
                exact=ExactNavEvidence.from_json(last[0])
                if _ratio(state['high_water_json'])!=exact.high_water_nav.fraction() or _ratio(state['day_open_json'])!=exact.day_open_nav.fraction():
                    raise PersistenceUnavailable('loss baseline projection mismatch')
            boundary=None
            for mark in c.execute("SELECT m.exact_json,e.payload_json,e.event_at_us FROM exact_marks m JOIN accounting_evidence e USING(evidence_id) WHERE m.account_id=? AND e.kind='mark' ORDER BY m.rowid",(account_id,)):
                payload=strict_json(mark['payload_json'])
                exact=ExactNavEvidence.from_json(mark['exact_json'])
                if mark['event_at_us']%86400000000==0 and 'derived_from' not in payload and exact.day_open_nav==exact.nav:
                    boundary=mark['event_at_us']
            if state['day_start_us']!=boundary:
                raise PersistenceUnavailable('UTC boundary projection mismatch')
            balances,totals,native,_,_,_=self._replay_economics(account_id)
            actual={r['asset']:(Fraction(decimal_value(r['native_qty'])),r['cost_basis_usd_u']) for r in c.execute('SELECT * FROM balance_positions WHERE account_id=?',(account_id,))}
            if actual!=balances:raise PersistenceUnavailable('economic balance projection mismatch')
            posted={r[0]:r[1] for r in c.execute('SELECT ledger_account,sum(usd_delta_u) FROM journal_postings JOIN journal_transactions USING(transaction_id) WHERE account_id=? GROUP BY ledger_account',(account_id,))}
            if {k:v for k,v in posted.items() if v}!={k:v for k,v in totals.items() if v}:raise PersistenceUnavailable('economic journal projection mismatch')
            actual_native={}
            for r in c.execute('SELECT ledger_account,asset,native_delta FROM journal_postings JOIN journal_transactions USING(transaction_id) WHERE account_id=? AND asset IS NOT NULL',(account_id,)):
                actual_native[r[0],r[1]]=actual_native.get((r[0],r[1]),Fraction(0))+Fraction(decimal_value(r[2]))
            if {k:v for k,v in actual_native.items() if v}!={k:v for k,v in native.items() if v}:
                raise PersistenceUnavailable('native journal projection mismatch')
