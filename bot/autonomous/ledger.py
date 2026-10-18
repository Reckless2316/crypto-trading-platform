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
    AccountRevision, AccountView, AppendResult, Balance, ContractViolation, CausalCutoffs,
    ControlState, DecisionReference, IntentState, ManifestEntry, MarketRules, Mode,
    OrderEvent, OrderProposal, OrderReference, Permit, PersistenceUnavailable, PreparedIntent, VenueOrderResult,
    Reservation, RiskLimits, RiskVerdict, SessionArchive, SessionCutoff, ReviewJob,
    candidate_wire_id, canonical_bytes, checked_money, content_hash, decimal_text, CANDIDATE_WIRE_IDS,
    decimal_value, strict_json, ExactRatio, ExactNavEvidence, Holding,
    MAX_DECIMAL_SCALE, StateUnavailable, HistoricalInventory,
    CalibrationArtifact, DecisionBatch, NumericSnapshot,
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
        self._outcome_verifier = None

    def configure_outcome_verifier(self, verifier) -> None:
        """Bind one trusted in-process source dependency, never a wire value.

        A reopened ledger has no verifier until authority explicitly wires it.
        This does not approve a calibrator or enable trading.
        """
        from .calibration import OutcomeEvidenceVerifier
        if not isinstance(verifier, OutcomeEvidenceVerifier):
            raise ContractViolation('typed authority outcome verifier required')
        if self._outcome_verifier is not None and self._outcome_verifier is not verifier:
            raise ContractViolation('outcome verifier already bound')
        self._outcome_verifier = verifier

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
        # Label events are the source of truth. Verify their contiguous
        # append-only chain against the current mutable projection on startup.
        for label in self.connection.execute('SELECT * FROM outcome_labels'):
            events = self.connection.execute('SELECT * FROM label_events WHERE label_id=? ORDER BY revision',
                (label['label_id'],)).fetchall()
            if not events:
                raise PersistenceUnavailable('outcome label has no event history')
            previous = None
            for revision, event in enumerate(events):
                payload = strict_json(event['payload_json'])
                if event['kind'] not in ('pending', 'resolved', 'corrected'):
                    raise PersistenceUnavailable('unknown label projection event kind')
                expected_id = 'label-event-' + content_hash((label['label_id'], revision,
                    event['kind'], event['payload_hash']))
                if (event['revision'] != revision or event['previous_event_id'] != previous
                        or event['label_event_id'] != expected_id):
                    raise PersistenceUnavailable('label event lineage integrity failure')
                previous = event['label_event_id']
            latest = events[-1]
            payload = strict_json(latest['payload_json'])
            first_payload = strict_json(events[0]['payload_json'])
            forecast = self.connection.execute('''SELECT f.horizon_candles,d.recorded_at_us,s.candle_close_us
                FROM forecasts f JOIN decisions d USING(decision_id)
                JOIN snapshots s USING(snapshot_id) WHERE f.forecast_id=?''',
                (label['forecast_id'],)).fetchone()
            from .calibration import fixed_horizon_maturity
            if (events[0]['kind'] != 'pending' or forecast is None
                    or forecast['horizon_candles'] != 4
                    or label['label_version'] != first_payload.get('label_version')
                    or label['maturity_us'] != first_payload.get('maturity_us')
                    or label['maturity_us'] != fixed_horizon_maturity(forecast['candle_close_us'],4)):
                raise PersistenceUnavailable('label maturity/version does not match originating event')
            expected = (latest['kind'], payload.get('net_positive_outcome'),
                payload.get('net_return'), payload.get('resolved_at_us'),
                payload.get('execution_manifest_hash'), payload.get('missing_reason'),
                latest['revision'])
            actual = (label['status'], label['outcome'], label['net_return'],
                label['resolved_at_us'], label['execution_manifest_hash'],
                label['missing_reason'], label['version'])
            if actual != expected:
                raise PersistenceUnavailable('label projection does not match event history')
        for row in self.connection.execute('SELECT release_hash,manifest_json FROM release_bundles'):
            manifest = strict_json(row['manifest_json'])
            if type(manifest) is not dict:
                raise PersistenceUnavailable('release manifest is not an object')
            # Gate approvals bind the canonical core, avoiding self-reference.
            core = {key:value for key,value in manifest.items() if key!='gate_approvals'}
            # Registry bundles store the approval-free CandidateBundle (empty gate_approvals), whose
            # identity is CandidateBundle.bundle_hash; both forms bind the same approval-free core.
            registry = manifest.get('gate_approvals')==[] and content_hash(manifest)==row['release_hash']
            if content_hash(core)!=row['release_hash'] and not registry:
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

    def inventory_as_of(self, account_id: str, cutoffs: CausalCutoffs) -> HistoricalInventory:
        """Rebuild native inventory from the committed causal journal prefix.

        Revision history supplies the transactionally committed table watermarks.
        Mutable balance projections and current accounting state are intentionally
        not consulted. This returns only `inventory:<asset>` accounts; USD cash is
        journaled separately under `cash` and is deliberately not represented here.
        """
        _identifier(account_id)
        if type(cutoffs) is not CausalCutoffs:
            raise ContractViolation('typed causal cutoffs required')
        try:
            history=self.connection.execute('''SELECT append_sequence,journal_sequence,version,
                    evidence_watermarks_json,known_at_us,recorded_at_us
                FROM account_revision_history WHERE account_id=?
                AND known_at_us<=? AND recorded_at_us<=?
                ORDER BY append_sequence DESC LIMIT 1''',
                (account_id,cutoffs.knowledge_cutoff_us,cutoffs.knowledge_cutoff_us)).fetchone()
            if history is None:
                raise ContractViolation('no cutoff-visible account revision history')
            for name in ('append_sequence','journal_sequence','version','known_at_us','recorded_at_us'):
                if type(history[name]) is not int or history[name]<0:
                    raise ContractViolation('malformed account revision history')
            if history['known_at_us']<history['recorded_at_us']:
                raise ContractViolation('incoherent account revision visibility')
            watermarks=strict_json(history['evidence_watermarks_json'])
            expected=set(_EXPORT_TIMES)-{'account_revision_history'}
            if type(watermarks) is not dict:
                raise ContractViolation('incomplete historical evidence watermarks')
            # Migration 001 predates these six explicitly introduced accounting
            # tables. Its exact, canonical key set proves their committed prefix
            # was empty; do not generalize this compatibility rule to other keys.
            legacy_accounting_prefix=set(watermarks)==expected-_ACCOUNTING_EXPORT_TABLES
            if legacy_accounting_prefix:
                if canonical_bytes(watermarks).decode()!=history['evidence_watermarks_json']:
                    raise ContractViolation('noncanonical legacy evidence watermarks')
                watermarks={**watermarks,**{table:0 for table in _ACCOUNTING_EXPORT_TABLES}}
            elif set(watermarks)!=expected:
                raise ContractViolation('incomplete historical evidence watermarks')
            for table,value in watermarks.items():
                if type(value) is not int or value<0:
                    raise ContractViolation('malformed historical evidence watermark')
            if not legacy_accounting_prefix and canonical_bytes(watermarks).decode()!=history['evidence_watermarks_json']:
                raise ContractViolation('noncanonical historical evidence watermarks')
            tx_watermark=watermarks['journal_transactions']
            posting_watermark=watermarks['journal_postings']
            txs=self.connection.execute('''SELECT rowid,transaction_id,sequence,account_id,event_at_us,recorded_at_us
                FROM journal_transactions WHERE rowid<=? ORDER BY sequence''',(tx_watermark,)).fetchall()
            # The journal sequence is global and append-only. A hole means the
            # watermark cannot prove a complete prefix.
            if any(type(row['sequence']) is not int or row['sequence']!=i
                    for i,row in enumerate(txs,1)):
                raise ContractViolation('gap in committed journal sequence prefix')
            account_txs=[row for row in txs if row['account_id']==account_id]
            max_sequence=max((row['sequence'] for row in account_txs),default=0)
            if max_sequence!=history['journal_sequence']:
                raise ContractViolation('account revision does not match committed journal prefix')
            if self.connection.execute('''SELECT 1 FROM journal_transactions
                    WHERE rowid>? AND account_id=? AND sequence<=? LIMIT 1''',
                    (tx_watermark,account_id,history['journal_sequence'])).fetchone():
                raise ContractViolation('journal sequence lies beyond its committed row watermark')
            quantities: dict[str,Fraction]={}
            posting_lineage=[]
            for tx in account_txs:
                if (type(tx['sequence']) is not int or tx['sequence']<=0
                        or type(tx['transaction_id']) is not str or not tx['transaction_id']
                        or type(tx['event_at_us']) is not int or tx['event_at_us']<0
                        or type(tx['recorded_at_us']) is not int or tx['recorded_at_us']<0):
                    raise ContractViolation('malformed journal transaction timestamps')
                if tx['recorded_at_us']>cutoffs.knowledge_cutoff_us:
                    raise ContractViolation('journal prefix exceeds knowledge cutoff')
                postings=self.connection.execute('''SELECT rowid,line_no,ledger_account,asset,native_delta,
                        usd_delta_u,recorded_at_us FROM journal_postings
                    WHERE transaction_id=? ORDER BY line_no''',(tx['transaction_id'],)).fetchall()
                if any(type(p['rowid']) is not int or p['rowid']>posting_watermark for p in postings):
                    raise ContractViolation('journal transaction has postings beyond its committed watermark')
                if len(postings)<2 or any(type(p['line_no']) is not int or p['line_no']!=i
                        for i,p in enumerate(postings)):
                    raise ContractViolation('incomplete journal postings at historical watermark')
                usd_total=0
                tx_native: dict[str,Fraction]={}
                native_conservation: dict[str,Fraction]={}
                line_payload=[]
                for posting in postings:
                    if type(posting['ledger_account']) is not str or not posting['ledger_account']:
                        raise ContractViolation('malformed journal account')
                    if type(posting['usd_delta_u']) is not int or type(posting['recorded_at_us']) is not int or posting['recorded_at_us']<0:
                        raise ContractViolation('malformed journal posting amount or timestamp')
                    checked_money(posting['usd_delta_u'])
                    if posting['recorded_at_us']>cutoffs.knowledge_cutoff_us:
                        raise ContractViolation('journal postings exceed knowledge cutoff')
                    usd_total+=posting['usd_delta_u']
                    native=None
                    if posting['native_delta'] is None:
                        if posting['asset'] is not None:
                            raise ContractViolation('malformed native posting identity')
                    else:
                        if (type(posting['asset']) is not str or not posting['asset']
                                or type(posting['native_delta']) is not str):
                            raise ContractViolation('malformed native posting asset')
                        native=decimal_value(posting['native_delta'])
                        if decimal_text(native)!=posting['native_delta']:
                            raise ContractViolation('noncanonical native posting quantity')
                        native_conservation[posting['asset']]=native_conservation.get(posting['asset'],Fraction(0))+Fraction(native)
                        if posting['ledger_account'].startswith('inventory:'):
                            asset=posting['ledger_account'][len('inventory:'):]
                            if not asset or asset!=posting['asset']:
                                raise ContractViolation('inventory posting asset mismatch')
                            tx_native[asset]=tx_native.get(asset,Fraction(0))+Fraction(native)
                    line_payload.append((posting['rowid'],posting['line_no'],posting['ledger_account'],
                        posting['asset'],posting['native_delta'],posting['usd_delta_u'],posting['recorded_at_us']))
                if usd_total!=0:
                    raise ContractViolation('unbalanced historical journal transaction')
                if any(quantity for quantity in native_conservation.values()):
                    raise ContractViolation('unbalanced native asset postings')
                if tx['event_at_us']<=cutoffs.candle_close_us:
                    posting_lineage.append((tx['rowid'],tx['transaction_id'],tx['sequence'],tx['event_at_us'],
                        tx['recorded_at_us'],tuple(line_payload)))
                    for asset,delta in tx_native.items():
                        quantities[asset]=quantities.get(asset,Fraction(0))+delta
            result=[]
            for asset,quantity in sorted(quantities.items()):
                if quantity==0:
                    continue
                denominator=quantity.denominator
                while denominator%2==0: denominator//=2
                while denominator%5==0: denominator//=5
                if denominator!=1:
                    raise ContractViolation('native inventory is not a finite decimal')
                d=quantity.denominator;twos=fives=0
                while d%2==0: d//=2;twos+=1
                while d%5==0: d//=5;fives+=1
                scale=max(twos,fives)
                scaled=quantity.numerator*10**scale//quantity.denominator
                sign='-' if scaled<0 else ''
                digits=str(abs(scaled)).rjust(scale+1,'0')
                text=(sign+digits if scale==0 else sign+digits[:-scale]+'.'+digits[-scale:])
                value=decimal_value(text)
                result.append((asset,value))
            revision=AccountRevision(history['journal_sequence'],history['version'])
            lineage_hash=content_hash({'account_id':account_id,'cutoffs':cutoffs.to_dict(),
                'revision':revision.to_dict(),'postings':tuple(posting_lineage)})
            return HistoricalInventory(cutoffs,account_id,revision,tuple(result),lineage_hash)
        except ContractViolation:
            raise
        except Exception as exc:
            raise ContractViolation('historical inventory prefix unavailable or malformed') from exc

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
        reservations = tuple(self._reservation(r) for r in self.connection.execute('SELECT r.*, i.instrument, i.side FROM reservations r JOIN order_intents i USING(intent_id) WHERE i.account_id=? AND (r.cash_usd_u>0 OR r.gross_usd_u>0 OR r.loss_budget_usd_u>0 OR r.remaining_qty!=\'0\') ORDER BY r.intent_id',(account_id,)))
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
        return Reservation(row['intent_id'],row['cash_usd_u'],decimal_value(row['base_qty']),row['gross_usd_u'],row['loss_budget_usd_u'],decimal_value(row['remaining_qty']),IntentState(row['state']),row['version'],row['instrument'],row['side'])

    def _prepared(self, intent_id: str) -> PreparedIntent:
        row = self.connection.execute('SELECT * FROM order_intents WHERE intent_id=?',(intent_id,)).fetchone()
        if row is None:
            raise ContractViolation('unknown intent')
        reservation = self.connection.execute('SELECT r.*, i.instrument, i.side FROM reservations r JOIN order_intents i USING(intent_id) WHERE r.intent_id=?',(intent_id,)).fetchone()
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
        if not proposal.reducing_only and not self.entries_allowed_now(proposal.account_id):
            raise ContractViolation('release entries not allowed')
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
                held_asset = sum(h.liquidation_value_usd_u for h in account.holdings if h.base_asset == market.base_asset)
                asset_pending = sum(row['gross_usd_u'] for row in self.connection.execute('SELECT i.instrument, r.gross_usd_u FROM reservations r JOIN order_intents i USING(intent_id) WHERE i.account_id=? AND i.side=\'buy\' AND i.intent_id!=?',(proposal.account_id,exclude_intent_id or '')) if str(row['instrument']).split('-', 1)[0] == market.base_asset)
                held_gross = sum(h.liquidation_value_usd_u for h in account.holdings)
                if notional+asset_pending+held_asset>equity*limits.max_position_fraction or held_gross+notional+sum(r.gross_usd_u for r in pending)>equity*limits.max_gross_fraction:
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
        verdict = assess(proposal,assess_account,control,limits,market,now,now_mono_ns=mono,current_boot_id=boot_id)
        if type(verdict) is not RiskVerdict or not verdict.allowed or verdict.account_revision!=account.revision or verdict.proposal_hash!=content_hash(proposal) or verdict.halt_transitions or verdict.escalation_codes:
            raise ContractViolation('current risk recheck refused/unbound')
        return account, cash, notional

    def _require_reduction_slot(self, proposal: OrderProposal) -> None:
        slot = proposal.action_slot
        parent_id, sep, ordinal_text = slot.rpartition(":")
        if not parent_id.startswith("reduce:") or not sep or not ordinal_text.isdigit() or ordinal_text.startswith("0"):
            raise ContractViolation("authority-allocated reduction/replacement slot unavailable")
        parent_id = parent_id.removeprefix("reduce:")
        if not parent_id or any(ch in parent_id for ch in "%_"):
            raise ContractViolation("authority-allocated reduction/replacement slot unavailable")
        ordinal = int(ordinal_text)
        parent = self.connection.execute(
            "SELECT account_id,candidate_id,instrument,side,state FROM order_intents WHERE intent_id=?",
            (parent_id,)).fetchone()
        wire_id = candidate_wire_id(proposal.candidate_id)
        if (parent is None or parent["account_id"] != proposal.account_id or parent["instrument"] != proposal.instrument
                or parent["side"] != "buy" or parent["state"] not in ("filled", "partial", "canceled") or parent["candidate_id"] != wire_id):
            raise ContractViolation("reduction parent is not a filled entry")
        # IOC cancellation describes the unfilled remainder. Its accounted fills
        # still created inventory that must remain eligible for a reducing exit.
        # A terminal status alone is not evidence that any base was acquired.
        fills = self.connection.execute(
            "SELECT base_qty FROM fills WHERE intent_id=? AND account_id=? AND side='buy'",
            (parent_id, proposal.account_id)).fetchall()
        if not any(decimal_value(fill["base_qty"]) > 0 for fill in fills):
            raise ContractViolation("reduction parent has no accounted fills")
        prefix = f"reduce:{parent_id}:"
        prior = self.connection.execute(
            "SELECT count(*) FROM order_intents WHERE account_id=? AND action_slot LIKE ?",
            (proposal.account_id, prefix + "%")).fetchone()[0]
        open_prior = self.connection.execute(
            "SELECT count(*) FROM order_intents WHERE account_id=? AND action_slot LIKE ? AND state NOT IN ('filled','canceled','rejected')",
            (proposal.account_id, prefix + "%")).fetchone()[0]
        if prior + 1 != ordinal or open_prior:
            raise ContractViolation("reduction ordinal is not the next authority allocation")

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

    def _latch_current_losses(self, account_id: str) -> None:
        """Persist drawdown/daily latches from current exact NAV in their own commit.

        Called before any reservation so a breach found at order time is durable even
        though the reservation itself is refused and rolled back. No model is involved.
        """
        account = self._account_view(account_id)
        evidence = account.exact_nav
        if evidence is None or evidence.account_id != account_id:
            return
        with self.transaction():
            self._latch_losses(account_id, evidence.mark_id, evidence.nav.fraction(),
                evidence.high_water_nav.fraction(), evidence.day_open_nav.fraction())

    def reserve_intent(self, proposal: OrderProposal, expected: AccountRevision,
            generation: int, decision: DecisionReference | None) -> PreparedIntent:
        if type(proposal) is not OrderProposal or type(expected) is not AccountRevision:
            raise ContractViolation('typed proposal/revision required')
        if not proposal.reducing_only:self._check_source_health()
        self._latch_current_losses(proposal.account_id)
        with self.transaction() as c:
            wire_id = candidate_wire_id(proposal.candidate_id)
            existing = c.execute('SELECT intent_id,proposal_json,generation FROM order_intents WHERE account_id=? AND candle_close_us=? AND candidate_id=? AND action_slot=?',(proposal.account_id,proposal.candle_close_us,wire_id,proposal.action_slot)).fetchone()
            if existing:
                if existing['proposal_json']!=proposal.to_json() or existing['generation']!=generation or generation!=self._require_account(proposal.account_id)['generation']:
                    raise ContractViolation('economic action identity conflict')
                return self._prepared(existing['intent_id'])
            # The next reduction slot is a pure function of the filled parent entry.
            # A caller-chosen string is not an allocation.
            if proposal.reducing_only:
                self._require_reduction_slot(proposal)
            elif proposal.action_slot!='entry':
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
            # Status never journals money or releases capacity; see reject_undispatched.
            # A fill or an unknown venue outcome makes current account state unproven.
            if event.result.filled_quantity>0 or event.kind is IntentState.UNKNOWN:
                self.invalidate_account(account_id)
            return AppendResult(True,event.event_id,self._revision(account_id))

    def reject_undispatched(self, intent_id: str, reason: str) -> bool:
        """Reject an intent that never left RESERVED and release its capacity atomically.

        Returns False, changing nothing, if the intent is no longer RESERVED: then some
        dispatch may have reached a venue and only venue evidence or reconciliation decides.
        """
        if type(reason) is not str or not reason:
            raise ContractViolation('rejection reason required')
        with self.transaction() as c:
            intent = self._prepared(intent_id)
            if intent.state is not IntentState.RESERVED:
                return False
            p = intent.proposal
            now = self._clock_us()
            reference = OrderReference(p.account_id, intent_id, intent.client_order_id, None)
            result = VenueOrderResult(reference, IntentState.REJECTED, Decimal(0), p.base_quantity, now,
                'authority-rejected', reason)
            event = OrderEvent('reject-'+intent_id, intent_id, IntentState.REJECTED, result, now, now, 'authority-rejected-'+intent_id)
            self._append_order_evidence(event.event_id, intent_id, 'rejected', event.to_json(), now, now, now, event.venue_event_key)
            c.execute("UPDATE order_intents SET state='rejected',version=version+1 WHERE intent_id=?", (intent_id,))
            c.execute("UPDATE reservations SET state='rejected',cash_usd_u=0,gross_usd_u=0,loss_budget_usd_u=0,remaining_qty='0',version=version+1,updated_at_us=? WHERE intent_id=?", (now, intent_id))
            return True

    def settle_reconciled_intent(self, evidence_id: str, intent_id: str) -> bool:
        """Release paper capacity only against current complete settlement proof.

        A terminal event proves order status; the economic replay proves accounted
        quantity/final fees, and an independent current mark proves account balances
        and open orders. Live evidence still requires the authenticated importer.
        """
        with self.transaction() as c:
            evidence, payload = self._evidence(evidence_id, 'mark')
            intent = self._prepared(intent_id)
            account_id = intent.proposal.account_id
            if evidence['account_id'] != account_id or intent.state not in _TERMINAL:
                raise ContractViolation('settlement requires account-bound terminal intent')
            account = self._account_view(account_id)
            state = self._state(account_id)
            mark = c.execute('SELECT * FROM exact_marks WHERE mark_id=?', (state['last_mark_id'],)).fetchone()
            if (not account.ready or account.unknown_intent_ids or mark is None or mark['evidence_id'] != evidence_id
                    or not mark['ready'] or mark['reconcile_id'] != account.reconcile_id):
                raise ContractViolation('settlement requires current complete reconciliation')
            balances = {r['asset']: r['native_qty'] for r in c.execute(
                'SELECT asset,native_qty FROM balance_positions WHERE account_id=?', (account_id,))}
            if payload['balances'] != balances or tuple(payload['open_intent_ids']) != account.open_intent_ids:
                raise ContractViolation('settlement observation differs from current account')
            row = c.execute('SELECT payload_json,recorded_at_us FROM order_events WHERE intent_id=? ORDER BY append_sequence DESC LIMIT 1',
                (intent_id,)).fetchone()
            if row is None or row['recorded_at_us'] > evidence['known_at_us']:
                raise ContractViolation('settlement lacks causal terminal event')
            event = OrderEvent.from_json(row['payload_json'])
            if (event.kind is not intent.state or event.result.received_at_us > evidence['event_at_us']):
                raise ContractViolation('settlement terminal evidence disagrees')
            replay = self._replay_economics(account_id)
            if replay[5] or Fraction(event.result.filled_quantity) != replay[4].get(intent_id, Fraction(0)):
                raise ContractViolation('settlement fills or final fees incomplete')
            reservation = intent.reservation
            if not any((reservation.cash_usd_u, reservation.gross_usd_u,
                    reservation.loss_budget_usd_u, reservation.base_quantity, reservation.remaining_quantity)):
                return False
            now = self._clock_us()
            c.execute("UPDATE reservations SET cash_usd_u=0,gross_usd_u=0,loss_budget_usd_u=0,base_qty='0',remaining_qty='0',version=version+1,updated_at_us=? WHERE intent_id=?",
                (now, intent_id))
            key = 'settlement-' + content_hash((account_id, intent_id, evidence_id))
            epoch = c.execute('SELECT permit_epoch FROM account_control WHERE account_id=?', (account_id,)).fetchone()[0]
            c.execute("INSERT INTO control_events VALUES (?,?,?,?,'recovery','reconciler',?,?)",
                (key, account_id, key, epoch, canonical_bytes({'intent_id': intent_id,
                    'mark_evidence_id': evidence_id, 'terminal_event_id': event.event_id}).decode(), now))
            return True

    def _append_order_evidence(self, event_id: str, intent_id: str, kind: str,
            payload_json: str, received_at_us: int, recorded_at_us: int,
            event_at_us: int | None = None, venue_event_key: str | None = None) -> None:
        if not self.connection.in_transaction:
            raise ContractViolation('order evidence requires an immediate transaction')
        self.connection.execute('INSERT INTO order_events(event_id,append_sequence,intent_id,kind,venue_event_key,payload_json,event_at_us,received_at_us,recorded_at_us) SELECT ?,coalesce(max(append_sequence),0)+1,?,?,?,?,?,?,? FROM order_events',
            (event_id,intent_id,kind,venue_event_key,payload_json,event_at_us,received_at_us,recorded_at_us))

    def append_forecast(self, *, forecast_id: str, decision_id: str, candidate_id: int,
            side: str, direction_probability: float | None = None, probabilities: dict | None = None,
            execution_policy_hash: str, eligibility: bool, calibrated_p: float | None = None,
            calibrator_hash: str | None = None, recorded_at_us: int | None = None) -> bool:
        """Record one immutable four-candle forecast and its pending label.

        Candidate status is deliberately not filtered: HOLD, vetoed, rejected,
        and eligible forecasts all enter the outcome cohort. The probability
        must be the proposed-direction probability, never a confidence score.
        """
        import math
        from .contracts import DecisionBatch, CANDIDATE_WIRE_IDS
        from .calibration import fixed_horizon_maturity
        if (type(forecast_id) is not str or not forecast_id or type(decision_id) is not str
                or not decision_id or type(candidate_id) is not int or not 1 <= candidate_id <= 6
                or type(side) is not str or side not in ('buy', 'sell')
                or type(eligibility) is not bool
                or type(execution_policy_hash) is not str or len(execution_policy_hash) != 64
                or any(ch not in '0123456789abcdef' for ch in execution_policy_hash)):
            raise ContractViolation('invalid forecast evidence')
        if calibrated_p is not None and (type(calibrated_p) not in (int, float)
                or not math.isfinite(calibrated_p) or not 0 <= calibrated_p <= 1):
            raise ContractViolation('invalid calibrated probability')
        if (calibrator_hash is not None and (type(calibrator_hash) is not str
                or len(calibrator_hash) != 64 or any(ch not in '0123456789abcdef' for ch in calibrator_hash))
                or (calibrated_p is None) != (calibrator_hash is None)):
            raise ContractViolation('calibration identity mismatch')
        if calibrated_p is not None:
            # A caller-supplied probability/hash pair is not proof of a
            # reviewed calibrator or its exact supported forecast input.
            raise PersistenceUnavailable('reviewed calibration estimate verification unavailable')
        now = self._clock_us() if recorded_at_us is None else recorded_at_us
        if type(now) is not int or now < 0:
            raise ContractViolation('invalid forecast timestamp')
        with self.transaction() as c:
            row = c.execute('''SELECT d.model_id,d.status,d.response_json,d.response_hash,d.request_id,
                d.received_at_us AS decision_received_at_us,d.recorded_at_us AS decision_recorded_at_us,
                s.recorded_at_us AS snapshot_recorded_at_us,
                s.candle_close_us,s.release_hash,s.snapshot_id,s.valid,s.state_json
                FROM decisions d JOIN snapshots s USING(snapshot_id) WHERE d.decision_id=?''',
                (decision_id,)).fetchone()
            if row is None or row['status'] != 'completed':
                raise ContractViolation('forecast decision is not complete')
            if not row['valid'] or now < row['candle_close_us'] or now >= row['candle_close_us'] + 4*3_600_000_000:
                raise ContractViolation('forecast timestamp is invalid or after fixed horizon maturity')
            if (content_hash(strict_json(row['response_json'])) != row['response_hash']):
                raise PersistenceUnavailable('persisted decision batch hash mismatch')
            try:
                batch = DecisionBatch.from_json(row['response_json'])
            except (TypeError, ValueError) as exc:
                raise PersistenceUnavailable('persisted typed DecisionBatch unavailable') from exc
            if (batch.request_id != row['request_id'] or batch.model_id != row['model_id']
                    or batch.bundle_hash != row['release_hash']
                    or batch.state_hash != content_hash(strict_json(row['state_json']))):
                raise ContractViolation('DecisionBatch does not match persisted decision/snapshot')
            if now < max(row['decision_received_at_us'],row['decision_recorded_at_us'],
                    row['snapshot_recorded_at_us'],batch.received_at_us):
                raise ContractViolation('forecast timestamp is before persisted decision knowledge')
            internal_id = next((name for name, wire in CANDIDATE_WIRE_IDS if wire == candidate_id), None)
            selected = next((item for item in batch.decisions if item.candidate_id == internal_id), None)
            if selected is None:
                raise ContractViolation('forecast candidate is absent from persisted judgment')
            class_probability = getattr(selected.direction_probabilities, selected.direction.value)
            binary_probability = (None if selected.direction.value == 'neutral'
                else getattr(selected.direction_probabilities, 'long' if side == 'buy' else 'short'))
            wire_probabilities = {'direction_probability': binary_probability,
                'direction_class_probability': class_probability,
                'selected_direction': selected.direction.value,
                'direction_probabilities': {'long': selected.direction_probabilities.long,
                    'short': selected.direction_probabilities.short,
                    'neutral': selected.direction_probabilities.neutral},
                'regime': selected.regime.value, 'decision_hash': row['response_hash']}
            if (direction_probability is not None and direction_probability != binary_probability
                    or probabilities is not None and probabilities.get('direction_probability') != binary_probability):
                raise ContractViolation('caller probability disagrees with immutable decision')
            probabilities_json = canonical_bytes(wire_probabilities).decode()
            candidate = c.execute('SELECT status FROM candidate_results WHERE decision_id=? AND candidate_id=?',
                (decision_id,candidate_id)).fetchone()
            if candidate is None:
                raise ContractViolation('forecast candidate result missing')
            maturity_us = fixed_horizon_maturity(row['candle_close_us'], 4)
            inserted = c.execute('''INSERT OR IGNORE INTO forecasts
                (forecast_id,decision_id,candidate_id,side,horizon_candles,probabilities_json,
                calibrated_p,calibrator_hash,eligibility,execution_policy_hash,recorded_at_us)
                VALUES (?,?,?,?,4,?,?,?,?,?,?)''',
                (forecast_id,decision_id,candidate_id,side,probabilities_json,
                 None if calibrated_p is None else str(calibrated_p),calibrator_hash,
                 int(eligibility),execution_policy_hash,now)).rowcount
            existing = c.execute('SELECT * FROM forecasts WHERE forecast_id=?',(forecast_id,)).fetchone()
            if existing is None:
                raise ContractViolation('forecast conflicts with an existing economic identity')
            expected = (decision_id,candidate_id,side,4,probabilities_json,
                None if calibrated_p is None else str(calibrated_p),calibrator_hash,
                int(eligibility),execution_policy_hash,now)
            actual = tuple(existing[k] for k in ('decision_id','candidate_id','side','horizon_candles',
                'probabilities_json','calibrated_p','calibrator_hash','eligibility','execution_policy_hash','recorded_at_us'))
            if actual != expected:
                raise ContractViolation('forecast identity/content conflict')
            label_id = 'label-' + content_hash((forecast_id,0))
            label = c.execute('SELECT * FROM outcome_labels WHERE label_id=?',(label_id,)).fetchone()
            if label is None:
                c.execute('''INSERT INTO outcome_labels(label_id,forecast_id,label_version,maturity_us,
                    resolved_at_us,status,net_return,outcome,execution_manifest_hash,missing_reason,version)
                    VALUES (?,?,0,?,NULL,'pending',NULL,NULL,NULL,'awaiting_first_executable_quote_and_four_candle_evidence',0)''',
                    (label_id,forecast_id,maturity_us))
                payload = {'label_version':0,'status':'pending','maturity_us':maturity_us,
                    'missing_reason':'awaiting_first_executable_quote_and_four_candle_evidence',
                    'requires_verified_execution_evidence':True}
                self._append_label_event(c,label_id,0,None,'pending',payload,now)
            elif label['forecast_id'] != forecast_id or label['maturity_us'] != maturity_us:
                raise ContractViolation('label identity conflict')
        return bool(inserted)

    def calibration_rows(self, *, before_us: int, after_us: int | None = None, instrument: str, regime,
                         side: str, model_id: str, schema_hash: str, dictionary_hash: str,
                         label_hash: str, cost_hash: str) -> tuple[dict, ...]:
        """Calibration rows built only from ledger-verified label events known by `before_us`.

        Each label contributes its latest resolved/corrected event recorded at or before the
        cutoff (an as-of view: later corrections are invisible). Forecasts without a binary
        proposed-direction probability (neutral) are excluded. `after_us` keeps decisions
        strictly after a training cutoff. Rows outside the exact cohort and versions are
        dropped, then overlapping four-hour windows are purged deterministically: rows are
        taken in quote order and any row starting before the last kept horizon is skipped.
        """
        from .calibration import label_definition_hash
        if type(before_us) is not int or (after_us is not None and type(after_us) is not int):
            raise ContractViolation('integer calibration block cutoffs required')
        names = {wire: name for name, wire in CANDIDATE_WIRE_IDS}
        rows = []
        for label in self.connection.execute('SELECT label_id,forecast_id FROM outcome_labels ORDER BY label_id').fetchall():
            event = self.connection.execute('SELECT * FROM label_events WHERE label_id=? AND recorded_at_us<=? '
                'ORDER BY revision DESC LIMIT 1', (label['label_id'], before_us)).fetchone()
            if event is None or event['kind'] not in ('resolved', 'corrected'):
                continue
            context = self.connection.execute('SELECT f.candidate_id,f.side,f.probabilities_json,f.execution_policy_hash,'
                'f.recorded_at_us AS forecast_at_us,d.response_json,d.model_id,s.field_hash,s.candle_close_us '
                'FROM forecasts f JOIN decisions d USING(decision_id) JOIN snapshots s USING(snapshot_id) '
                'WHERE f.forecast_id=?', (label['forecast_id'],)).fetchone()
            payload = strict_json(event['payload_json'])
            probabilities = strict_json(context['probabilities_json'])
            if probabilities.get('direction_probability') is None:
                continue
            if after_us is not None and context['candle_close_us'] <= after_us:
                continue
            batch = DecisionBatch.from_json(context['response_json'])
            rows.append({
                'forecast_id': label['forecast_id'], 'label_event_id': event['label_event_id'],
                'decision_at_us': context['candle_close_us'], 'forecast_at_us': context['forecast_at_us'],
                'first_executable_quote_us': payload['first_executable_quote_us'],
                'horizon_close_us': payload['horizon_close_us'], 'resolved_at_us': payload['resolved_at_us'],
                'direction_probability': probabilities['direction_probability'],
                'net_return': decimal_value(payload['net_return']),
                'net_positive_outcome': payload['net_positive_outcome'],
                'terminal_direction_outcome': payload.get('terminal_direction_outcome'),
                'costs_included': payload['costs_included'], 'execution_manifest_hash': payload['execution_manifest_hash'],
                'observations': payload['observations'], 'side': context['side'],
                'instrument': names[context['candidate_id']].upper() + '-USD', 'regime': probabilities['regime'],
                'model_id': context['model_id'], 'schema_hash': batch.schema_hash, 'dictionary_hash': context['field_hash'],
                'label_hash': label_definition_hash(payload), 'cost_hash': context['execution_policy_hash']})
        wanted = {'instrument': instrument, 'regime': getattr(regime, 'value', regime), 'side': side,
            'model_id': model_id, 'schema_hash': schema_hash, 'dictionary_hash': dictionary_hash,
            'label_hash': label_hash, 'cost_hash': cost_hash}
        kept, horizon = [], None
        for row in sorted((r for r in rows if all(r[k] == v for k, v in wanted.items())),
                key=lambda r: (r['first_executable_quote_us'], r['forecast_id'], r['label_event_id'])):
            if horizon is not None and row['first_executable_quote_us'] <= horizon:
                continue
            kept.append(row);horizon = row['horizon_close_us']
        return tuple(kept)

    def record_calibration_attestation(self, *, release_hash: str, **build_args) -> str:
        """Build, attest and append a calibration from ledger label events only.

        Rows come from `calibration_rows` at the block cutoffs, never from the caller.
        The release manifest must name the attested artifact as its calibrator. One
        calibrator may be attested for several releases; each pairing is its own run.
        """
        from .calibration import ATTESTATION_METHOD, attest_calibration_artifact, build_calibration_artifact
        training_cutoff, calibration_cutoff = build_args.get('training_cutoff_us'), build_args.get('calibration_cutoff_us')
        now = self._clock_us()
        if type(calibration_cutoff) is not int or calibration_cutoff > now:
            raise ContractViolation('calibration cutoff must not be in the future')
        cohort = {key: build_args.get(key) for key in ('instrument', 'regime', 'model_id', 'schema_hash',
            'dictionary_hash', 'label_hash', 'cost_hash')}
        cohort['side'] = build_args.get('side', 'buy')
        training = self.calibration_rows(before_us=training_cutoff, **cohort)
        calibration = self.calibration_rows(before_us=calibration_cutoff, after_us=training_cutoff, **cohort)
        base = build_calibration_artifact(training=training, calibration=calibration, **build_args)
        attested, metrics = attest_calibration_artifact(base, training=training, calibration=calibration, **build_args)
        run_id = content_hash((ATTESTATION_METHOD, attested.artifact_hash, release_hash))
        with self.transaction() as c:
            release = c.execute('SELECT manifest_json FROM release_bundles WHERE release_hash=?', (release_hash,)).fetchone()
            manifest = strict_json(release['manifest_json']) if release else None
            if type(manifest) is not dict or manifest.get('calibrator_hash') != attested.artifact_hash:
                raise ContractViolation('release does not name the attested calibrator')
            payload = canonical_bytes({'artifact': attested.to_dict(), 'metrics': metrics,
                'calibration_cutoff_us': calibration_cutoff}).decode()
            values = (run_id, release_hash, metrics['training_rows_hash'],
                metrics['calibration_rows_hash'], ATTESTATION_METHOD, payload, 'completed')
            existing = c.execute('SELECT * FROM evaluation_runs WHERE run_id=?', (run_id,)).fetchone()
            if existing is not None:
                if tuple(existing)[:7] != values:
                    raise ContractViolation('calibration attestation identity conflict')
            else:
                c.execute('INSERT INTO evaluation_runs VALUES (?,?,?,?,?,?,?,?)', values + (now,))
        return attested.artifact_hash

    def attested_calibration(self, artifact_hash: str, release_hash: str) -> CalibrationArtifact | None:
        """The recorded attested artifact for this release, or None (fail closed)."""
        from .calibration import ATTESTATION_METHOD
        run_id = content_hash((ATTESTATION_METHOD, artifact_hash, release_hash))
        row = self.connection.execute("SELECT * FROM evaluation_runs WHERE run_id=? AND release_hash=? AND holdout_id=? AND status='completed'",
            (run_id, release_hash, ATTESTATION_METHOD)).fetchone()
        release = self.connection.execute('SELECT manifest_json FROM release_bundles WHERE release_hash=?', (release_hash,)).fetchone()
        if row is None or release is None:
            return None
        try:
            manifest = strict_json(release['manifest_json'])
            artifact = CalibrationArtifact.from_dict(strict_json(row['metrics_json'])['artifact'])
        except (TypeError, ValueError, KeyError, ContractViolation):
            return None
        if (type(manifest) is not dict or manifest.get('calibrator_hash') != artifact_hash
                or artifact.artifact_hash != artifact_hash
                or not (artifact.uncertainty_passed and artifact.tail_checks_passed)):
            return None
        return artifact

    def verified_decision_snapshot(self, batch: DecisionBatch, snapshot: NumericSnapshot) -> dict | None:
        """Persisted completed decision equal to `batch`, joined to the persisted valid `snapshot`."""
        if type(batch) is not DecisionBatch or type(snapshot) is not NumericSnapshot or self.connection.in_transaction:
            return None
        row = self.connection.execute('SELECT * FROM decisions WHERE request_id=?', (batch.request_id,)).fetchone()
        if row is None or row['status'] != 'completed' or row['response_json'] is None:
            return None
        try:
            if content_hash(strict_json(row['response_json'])) != row['response_hash']:
                return None
            if DecisionBatch.from_json(row['response_json']) != batch or row['model_id'] != batch.model_id:
                return None
        except (TypeError, ValueError, ContractViolation):
            return None
        anchor = self.connection.execute('SELECT * FROM snapshots WHERE snapshot_id=?', (row['snapshot_id'],)).fetchone()
        persisted = self.connection.execute('SELECT * FROM snapshots WHERE snapshot_id=?', (snapshot.snapshot_id,)).fetchone()
        if anchor is None or persisted is None:
            return None
        if (persisted['state_json'] != anchor['state_json'] or not persisted['valid']
                or persisted['release_hash'] != batch.bundle_hash or persisted['account_id'] != snapshot.account_id
                or persisted['candle_close_us'] != snapshot.cutoffs.candle_close_us
                or persisted['knowledge_cutoff_us'] != snapshot.cutoffs.knowledge_cutoff_us
                or persisted['lineage_hash'] != snapshot.lineage_hash
                or content_hash(strict_json(persisted['state_json'])) != batch.state_hash
                or self._source_lineage_invalid(snapshot.snapshot_id)):
            return None
        # Bind the caller's snapshot to the exact candidate row the judgment saw.
        from .state import _wire_values
        from .contracts import default_field_dictionary
        try:
            wire_id = candidate_wire_id(snapshot.candidate_id)
            expected = _wire_values(snapshot.features, default_field_dictionary().candidate_fields, snapshot.candidate_id)
            rows = [row for row in strict_json(persisted['state_json'])[1] if row and row[0] == wire_id]
        except (ContractViolation, TypeError, ValueError, KeyError, IndexError):
            return None
        if snapshot.instrument != snapshot.candidate_id.upper() + '-USD' or rows != [expected]:
            return None
        return {'decision_id': row['decision_id'], 'field_hash': persisted['field_hash']}

    def _append_label_event(self, connection: sqlite3.Connection, label_id: str,
            revision: int, previous_event_id: str | None, kind: str,
            payload: dict, recorded_at_us: int) -> str:
        payload_json = canonical_bytes(payload).decode()
        payload_hash = content_hash(payload)
        event_id = 'label-event-' + content_hash((label_id,revision,kind,payload_hash))
        connection.execute('INSERT INTO label_events VALUES (?,?,?,?,?,?,?,?)',
            (event_id,label_id,revision,previous_event_id,kind,payload_json,payload_hash,recorded_at_us))
        return event_id

    def revise_outcome_label(self, label_id: str, kind: str, payload: dict,
            *, recorded_at_us: int | None = None) -> str:
        """Append an immutable label revision and rebuild its mutable projection.

        A missing executable observation stays `pending`; it is never converted
        to a negative label. Corrections append a new event and retain history.
        """
        from .calibration import label_mature
        if type(label_id) is not str or not label_id or type(payload) is not dict:
            raise ContractViolation('invalid label revision')
        if kind not in ('pending','resolved','corrected'):
            raise ContractViolation('unsupported label revision kind')
        if kind in ('resolved', 'corrected') and self._outcome_verifier is None:
            # This ledger does not yet have an evidence resolver that binds
            # executable quotes, depth, fills, fees and slippage to source
            # events. Caller assertions and hashes alone cannot resolve labels.
            raise PersistenceUnavailable('verified causal outcome evidence resolver unavailable; label remains pending')
        now = self._clock_us() if recorded_at_us is None else recorded_at_us
        if type(now) is not int or now < 0:
            raise ContractViolation('invalid label event timestamp')
        payload_json = canonical_bytes(payload).decode()
        with self.transaction() as c:
            label = c.execute('''SELECT l.*,f.horizon_candles FROM outcome_labels l
                JOIN forecasts f USING(forecast_id) WHERE l.label_id=?''',(label_id,)).fetchone()
            if label is None:
                raise ContractViolation('unknown outcome label')
            latest = c.execute('SELECT * FROM label_events WHERE label_id=? ORDER BY revision DESC LIMIT 1',(label_id,)).fetchone()
            if latest is None:
                raise PersistenceUnavailable('label event lineage missing')
            if now < latest['recorded_at_us']:
                raise ContractViolation('label revision timestamp precedes immutable predecessor')
            # Label time is append-only: never later than the clock, never before any existing
            # label event, and never inside an already attested calibration block.
            if now > self._clock_us():
                raise ContractViolation('label revision timestamp is in the future')
            if now < (c.execute('SELECT max(recorded_at_us) FROM label_events').fetchone()[0] or 0):
                raise ContractViolation('label revision timestamp precedes latest label event')
            attested_cutoff = c.execute("SELECT max(json_extract(metrics_json,'$.calibration_cutoff_us')) FROM evaluation_runs WHERE holdout_id LIKE 'calibration-attestation-%'").fetchone()[0]
            if attested_cutoff is not None and now <= attested_cutoff:
                raise ContractViolation('label revision timestamp precedes an attested calibration cutoff')
            if kind == 'corrected' and latest['kind'] not in ('resolved','corrected'):
                raise ContractViolation('only a resolved label can be corrected')
            if kind == 'resolved' and latest['kind'] not in ('pending',):
                raise ContractViolation('resolved label requires pending predecessor')
            outcome = net_return = resolved_at = manifest_hash = None
            missing_reason = payload.get('missing_reason')
            if kind in ('resolved','corrected'):
                from dataclasses import asdict
                from .contracts import DecisionBatch
                from .calibration import OutcomeVerificationRequest, verified_outcome_payload
                forecast = c.execute('''SELECT f.forecast_id,f.decision_id,f.candidate_id,f.side,
                    f.execution_policy_hash,f.recorded_at_us AS forecast_at_us,
                    d.response_json,d.response_hash,d.model_id,s.field_hash,s.candle_close_us
                    FROM forecasts f JOIN decisions d USING(decision_id)
                    JOIN snapshots s USING(snapshot_id) WHERE f.forecast_id=?''',
                    (label['forecast_id'],)).fetchone()
                if (forecast is None or label['horizon_candles'] != 4
                        or content_hash(strict_json(forecast['response_json'])) != forecast['response_hash']):
                    raise PersistenceUnavailable('forecast execution context unavailable')
                batch = DecisionBatch.from_json(forecast['response_json'])
                if batch.schema_hash is None or batch.model_id != forecast['model_id']:
                    raise PersistenceUnavailable('exact outcome model/schema context unavailable')
                request = OutcomeVerificationRequest(label_id,label['forecast_id'],forecast['decision_id'],
                    forecast['candidate_id'],forecast['side'],forecast['model_id'],batch.schema_hash,
                    forecast['field_hash'],forecast['execution_policy_hash'],forecast['candle_close_us'],
                    forecast['forecast_at_us'],now,latest['label_event_id'],payload_json)
                try:
                    evidence = self._outcome_verifier.verify(request)
                    payload = verified_outcome_payload(request,evidence,self._outcome_verifier)
                except Exception as exc:
                    raise PersistenceUnavailable('verified causal execution outcome unavailable') from exc
                payload['verification_context'] = asdict(request)
                outcome = payload['net_positive_outcome']
                net_return = payload['net_return']
                resolved_at = payload['resolved_at_us']
                manifest_hash = payload['execution_manifest_hash']
                missing_reason = None
            else:
                if any(k in payload for k in ('net_positive_outcome','terminal_direction_outcome',
                        'net_return','resolved_at_us','execution_manifest_hash')):
                    raise ContractViolation('pending label cannot carry an outcome')
                if type(missing_reason) is not str or not missing_reason:
                    raise ContractViolation('pending label needs a missingness reason')
            revision = latest['revision'] + 1
            event_id = self._append_label_event(c,label_id,revision,latest['label_event_id'],kind,payload,now)
            status = 'corrected' if kind == 'corrected' else kind
            c.execute('''UPDATE outcome_labels SET resolved_at_us=?,status=?,net_return=?,outcome=?,
                execution_manifest_hash=?,missing_reason=?,version=version+1 WHERE label_id=?''',
                (resolved_at,status,net_return,outcome,manifest_hash,missing_reason,label_id))
        return event_id

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

    @staticmethod
    def _review_incident_key(account_id: str, candidate_id: int, release_hash: str,
            incident_epoch: int, kind: str) -> str:
        return canonical_bytes((account_id,candidate_id,release_hash,incident_epoch,kind)).decode()

    def pause_candidate_for_review(self, account_id: str, candidate_id: int,
            release_hash: str, incident_epoch: int, kind: str, job: ReviewJob) -> AppendResult:
        """Atomically persist a provenance-bound, append-only sticky review pause.

        No clear or recovery policy is authorized by the current contract; this
        method only records the pause and does not provide a clearing operation.
        """
        if (type(account_id) is not str or not account_id or type(candidate_id) is not int
                or not 1 <= candidate_id <= 6 or type(release_hash) is not str
                or len(release_hash)!=64 or any(ch not in '0123456789abcdef' for ch in release_hash)
                or type(incident_epoch) is not int
                or incident_epoch < 0 or type(kind) is not str or not kind or len(kind)>64
                or type(job) is not ReviewJob):
            raise ContractViolation('invalid candidate review pause')
        if kind not in ('low_confidence','crisis'):
            raise ContractViolation('unsupported candidate review incident kind')
        expected_key=self._review_incident_key(account_id,candidate_id,release_hash,incident_epoch,kind)
        if (job.dedupe_key!=expected_key or job.kind!=kind
                or job.decision_id is None or job.snapshot_id is None):
            raise ContractViolation('review job does not match candidate incident identity')
        event_id='candidate-review-'+content_hash((account_id,candidate_id,release_hash,incident_epoch,kind))
        with self.transaction() as c:
            self._require_account(account_id)
            from .contracts import DecisionBatch, CANDIDATE_WIRE_IDS
            evidence_row=c.execute('''SELECT d.decision_id,d.request_id,d.model_id,d.response_json,
                d.response_hash,d.status,d.received_at_us,d.recorded_at_us,
                s.snapshot_id,s.account_id,s.release_hash,s.state_json,s.valid
                FROM decisions d JOIN snapshots s USING(snapshot_id) WHERE d.decision_id=?''',
                (job.decision_id,)).fetchone()
            if (evidence_row is None or evidence_row['snapshot_id']!=job.snapshot_id
                    or evidence_row['account_id']!=account_id
                    or evidence_row['release_hash']!=release_hash
                    or evidence_row['status']!='completed' or not evidence_row['valid']
                    or evidence_row['response_json'] is None
                    or evidence_row['response_hash'] is None):
                raise ContractViolation('candidate review lacks same-account persisted decision evidence')
            if content_hash(strict_json(evidence_row['response_json']))!=evidence_row['response_hash']:
                raise PersistenceUnavailable('persisted review decision hash mismatch')
            try:
                batch=DecisionBatch.from_json(evidence_row['response_json'])
            except (TypeError, ValueError) as exc:
                raise PersistenceUnavailable('persisted typed review decision unavailable') from exc
            if (batch.request_id!=evidence_row['request_id']
                    or batch.model_id!=evidence_row['model_id']
                    or batch.bundle_hash!=release_hash
                    or batch.state_hash!=content_hash(strict_json(evidence_row['state_json']))
                    or evidence_row['received_at_us'] is None
                    or incident_epoch!=evidence_row['received_at_us']):
                raise ContractViolation('candidate review decision/snapshot provenance mismatch')
            candidate_name=next((name for name,wire in CANDIDATE_WIRE_IDS if wire==candidate_id),None)
            selected=next((item for item in batch.decisions if item.candidate_id==candidate_name),None)
            result=c.execute('SELECT status FROM candidate_results WHERE decision_id=? AND candidate_id=?',
                (job.decision_id,candidate_id)).fetchone()
            if selected is None or result is None or result['status']!='hold':
                raise ContractViolation('candidate review evidence is absent or not held')
            if kind=='low_confidence':
                critical={'direction':selected.direction_confidence,'regime':selected.regime_confidence,
                    'quality':selected.quality_confidence,'risk':selected.risk_confidence}
                low={name:value for name,value in critical.items() if value<0.60}
                if not low:
                    raise ContractViolation('low-confidence review lacks threshold evidence')
                evidence={'critical_confidences':critical,'below_threshold':low}
            else:
                if selected.regime.value!='crisis':
                    raise ContractViolation('crisis review lacks persisted crisis evidence')
                evidence={'regime':selected.regime.value,
                    'direction':selected.direction.value,
                    'direction_confidences':{'long':selected.direction_probabilities.long,
                        'short':selected.direction_probabilities.short,
                        'neutral':selected.direction_probabilities.neutral}}
            incident_details={'account_id':account_id,'candidate_id':candidate_id,
                'release_hash':release_hash,'incident_epoch':incident_epoch,'kind':kind,
                'decision_id':job.decision_id,'snapshot_id':job.snapshot_id,
                'decision_hash':evidence_row['response_hash'],
                'snapshot_hash':content_hash(strict_json(evidence_row['state_json'])),
                'evidence':evidence}
            if job.payload_hash!=content_hash(incident_details):
                raise ContractViolation('review job payload hash does not bind canonical incident evidence')
            previous=c.execute('SELECT job_id,job_json FROM review_jobs WHERE job_id=? OR dedupe_key=?',
                (job.job_id,job.dedupe_key)).fetchall()
            if previous and (len(previous)!=1 or previous[0]['job_json']!=job.to_json()):
                raise ContractViolation('review job identity/content conflict')
            for table,key,value in (('review_sessions','session_id',job.session_id),
                    ('snapshots','snapshot_id',job.snapshot_id),('decisions','decision_id',job.decision_id)):
                if value is not None and not c.execute('SELECT 1 FROM '+table+' WHERE '+key+'=?',(value,)).fetchone():
                    raise ContractViolation('missing review job reference')
            if job.session_id is not None:
                session=c.execute('SELECT account_id FROM review_sessions WHERE session_id=?',
                    (job.session_id,)).fetchone()
                if session is None or session['account_id']!=account_id:
                    raise ContractViolation('cross-account review session reference')
            control=c.execute('SELECT permit_epoch FROM account_control WHERE account_id=?',(account_id,)).fetchone()
            event=c.execute('SELECT account_id,request_id,reason FROM control_events WHERE event_id=? OR request_id=?',
                (event_id,event_id)).fetchall()
            reason='candidate review pause:'+str(candidate_id)+':'+kind
            if event and (len(event)!=1 or tuple(event[0])!=(account_id,event_id,reason)):
                raise ContractViolation('candidate review event identity conflict')
            if not event:
                c.execute("INSERT INTO control_events VALUES (?,?,?,?,'review','policy',?,?)",
                    (event_id,account_id,event_id,control[0],reason,self._clock_us()))
            if not previous:
                c.execute('INSERT INTO review_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (job.job_id,job.dedupe_key,job.kind,job.session_id,job.snapshot_id,job.decision_id,
                     job.status.value,job.payload_hash,job.attempts,job.next_attempt_us,job.to_json(),self._clock_us()))
            existing=c.execute('SELECT review_paused,incident_epoch,review_job_id FROM candidate_control WHERE account_id=? AND candidate_id=?',
                (account_id,candidate_id)).fetchone()
            if existing and existing['incident_epoch']>incident_epoch:
                raise ContractViolation('candidate incident epoch regressed')
            if existing:
                # A pause is sticky. A later signal can advance its evidence link,
                # but neither confidence nor release changes can clear it.
                if (not existing['review_paused'] or existing['review_job_id']!=job.job_id
                        or existing['incident_epoch']!=max(existing['incident_epoch'],incident_epoch)):
                    c.execute('UPDATE candidate_control SET review_paused=1,review_job_id=?,incident_epoch=?,version=version+1 WHERE account_id=? AND candidate_id=?',
                        (job.job_id,max(existing['incident_epoch'],incident_epoch),account_id,candidate_id))
            else:
                c.execute('INSERT INTO candidate_control VALUES (?, ?, 1, ?, ?, 0)',
                    (account_id,candidate_id,job.job_id,incident_epoch))
            result_id=previous[0]['job_id'] if previous else job.job_id
            return AppendResult(not bool(previous),result_id,self._revision(account_id))

    def latch_account_crisis(self, account_id: str, incident_id: str, reason: str) -> bool:
        """Durably halt account entries and invalidate permits, idempotently."""
        if (type(account_id) is not str or not account_id or type(incident_id) is not str
                or not incident_id or len(incident_id)>256 or type(reason) is not str
                or not reason or len(reason)>256):
            raise ContractViolation('invalid account crisis incident')
        event_id='crisis-'+content_hash((account_id,incident_id))
        event_reason='crisis:'+reason
        with self.transaction() as c:
            self._require_account(account_id)
            row=c.execute('SELECT * FROM account_control WHERE account_id=?',(account_id,)).fetchone()
            previous=c.execute('SELECT account_id,request_id,reason FROM control_events WHERE event_id=? OR request_id=?',
                (event_id,event_id)).fetchall()
            if previous:
                if len(previous)!=1 or tuple(previous[0])!=(account_id,event_id,event_reason):
                    raise ContractViolation('crisis event identity/content conflict')
                if not row['crisis_halted'] or row['permit']!='none' or row['lease_boot_id'] is not None:
                    now=self._clock_us()
                    c.execute("UPDATE account_control SET permit='none',permit_epoch=permit_epoch+1,crisis_halted=1,lease_boot_id=NULL,lease_deadline_mono_ns=NULL,version=version+1,halt_reason=?,updated_at_us=? WHERE account_id=?",
                        (reason,now,account_id))
                return False
            now=self._clock_us()
            c.execute("INSERT INTO control_events VALUES (?,?,?,?,'review','policy',?,?)",
                (event_id,account_id,event_id,row['permit_epoch'],event_reason,now))
            c.execute("UPDATE account_control SET permit='none',permit_epoch=permit_epoch+1,crisis_halted=1,lease_boot_id=NULL,lease_deadline_mono_ns=NULL,version=version+1,halt_reason=?,updated_at_us=? WHERE account_id=?",
                (reason,now,account_id))
            return True

    def latch_operator_kill(self, account_id: str) -> bool:
        """Set the hard kill that order admission reads. This never clears it."""
        if type(account_id) is not str or not account_id:
            raise ContractViolation("invalid operator kill")
        event_id = "hard-kill-" + content_hash((account_id,))
        reason = "operator_kill"
        with self.transaction() as c:
            self._require_account(account_id)
            row = c.execute(
                "SELECT hard_killed, permit_epoch FROM account_control WHERE account_id=?",
                (account_id,)).fetchone()
            previous = c.execute(
                "SELECT account_id, request_id, reason FROM control_events WHERE event_id=?",
                (event_id,)).fetchall()
            if previous:
                if len(previous) != 1 or tuple(previous[0]) != (account_id, event_id, reason):
                    raise ContractViolation("operator kill identity conflict")
                if not row["hard_killed"]:
                    now = self._clock_us()
                    c.execute(
                        """UPDATE account_control SET hard_killed=1, version=version+1,
                           halt_reason=?, updated_at_us=? WHERE account_id=?""",
                        (reason, now, account_id))
                self._release_killed_reservations(account_id)
                return False
            now = self._clock_us()
            c.execute(
                "INSERT INTO control_events VALUES (?,?,?,?,'hard_kill','operator',?,?)",
                (event_id, account_id, event_id, row["permit_epoch"], reason, now))
            c.execute(
                """UPDATE account_control SET hard_killed=1, version=version+1,
                   halt_reason=?, updated_at_us=? WHERE account_id=?""",
                (reason, now, account_id))
            self._release_killed_reservations(account_id)
            return True

    # ---------- Task 14: operator paper arm and the deadman lease ----------
    MAX_LEASE_NS = 600_000_000_000
    REDUCE_LEASE_NS = 60_000_000_000

    def arm(self, account_id: str, actor: str, *, mode: str | None = None, boot_id: str, lease_ns: int,
            release_hash: str) -> str | None:
        """Operator PAPER arm with a bounded deadman lease. Returns a refusal code or None when armed.

        Requires a reconciled account, the named release active and unexpired, no unknown intents and no latch."""
        account = self._require_account(account_id)
        if mode == 'live' or account['mode'] != 'paper':
            return 'LIVE_ARM_REQUIRES_TASK22'
        if type(lease_ns) is not int or not 0 < lease_ns <= self.MAX_LEASE_NS:
            raise ContractViolation('lease must be bounded')
        _identifier(boot_id)
        row = self.connection.execute('SELECT * FROM account_control WHERE account_id=?', (account_id,)).fetchone()
        for latch in ('hard_killed', 'drawdown_latched', 'daily_halted', 'crisis_halted'):
            if row[latch]:
                return 'LATCHED:' + latch
        if row['active_release_hash'] is None:
            return 'NO_ACTIVE_RELEASE'
        if release_hash != row['active_release_hash']:
            return 'RELEASE_MISMATCH'
        release = self.connection.execute('SELECT expires_at_us FROM release_bundles WHERE release_hash=?',
                                          (row['active_release_hash'],)).fetchone()
        if release is None or release[0] <= self._clock_us() or not self.entries_allowed_now(account_id):
            return 'RELEASE_EXPIRED'
        view = self.account_view(account_id)
        if view.unknown_intent_ids:
            return 'PENDING_UNKNOWN_ORDER'
        if not view.ready:
            return 'UNRECONCILED'
        with self.transaction() as c:
            now, deadline = self._clock_us(), self._monotonic_ns() + lease_ns
            c.execute("UPDATE account_control SET permit='entry',permit_epoch=permit_epoch+1,lease_boot_id=?,"
                      "lease_deadline_mono_ns=?,version=version+1,updated_at_us=? WHERE account_id=?",
                      (boot_id, deadline, now, account_id))
            key = 'arm-' + content_hash((account_id, actor, now, deadline))
            c.execute("INSERT INTO control_events VALUES (?,?,?,?,'arm',?,?,?)",
                      (key, account_id, key, row['permit_epoch'] + 1, actor, 'operator paper arm', now))
        return None

    def renew_lease(self, account_id: str, actor: str, *, boot_id: str, lease_ns: int) -> str | None:
        """Authority-worker renewal of a live (unexpired) lease; an expired lease is never revived."""
        if type(lease_ns) is not int or not 0 < lease_ns <= self.MAX_LEASE_NS:
            raise ContractViolation('lease must be bounded')
        with self.transaction() as c:
            row = c.execute('SELECT * FROM account_control WHERE account_id=?', (account_id,)).fetchone()
            mono = self._monotonic_ns()
            if row['hard_killed']:
                return 'HARD_KILL'
            if row['permit'] == 'none':
                return 'NOT_ARMED'
            if row['lease_boot_id'] != boot_id:
                return 'LEASE_BOOT_MISMATCH'
            if row['lease_deadline_mono_ns'] is None or mono >= row['lease_deadline_mono_ns']:
                return 'LEASE_EXPIRED'
            now = self._clock_us()
            c.execute('UPDATE account_control SET lease_deadline_mono_ns=?,version=version+1,updated_at_us=? WHERE account_id=?',
                      (mono + lease_ns, now, account_id))
            key = 'renew-' + content_hash((account_id, actor, now, mono))
            c.execute("INSERT INTO control_events VALUES (?,?,?,?,'renew',?,?,?)",
                      (key, account_id, key, row['permit_epoch'], actor, 'deadman lease renewal', now))
        return None

    def enforce_lease(self, account_id: str) -> bool:
        """Deadman: a lapsed entry lease disarms entries (reduce_only, new epoch); exits keep a bounded reduce lease."""
        with self.transaction() as c:
            row = c.execute('SELECT * FROM account_control WHERE account_id=?', (account_id,)).fetchone()
            mono = self._monotonic_ns()
            lapsed = row['lease_deadline_mono_ns'] is None or mono >= row['lease_deadline_mono_ns']
            if row['permit'] == 'none' or row['hard_killed'] or not lapsed:
                return False
            now = self._clock_us()
            c.execute("UPDATE account_control SET permit='reduce_only',permit_epoch=permit_epoch+?,lease_deadline_mono_ns=?,"
                      "version=version+1,updated_at_us=? WHERE account_id=?",
                      (1 if row['permit'] == 'entry' else 0, mono + self.REDUCE_LEASE_NS, now, account_id))
            if row['permit'] == 'entry':
                key = 'deadman-' + content_hash((account_id, now, mono))
                c.execute("INSERT INTO control_events VALUES (?,?,?,?,'disarm','authority',?,?)",
                          (key, account_id, key, row['permit_epoch'] + 1, 'deadman lease expired: entries disarmed', now))
            return True

    def disarm(self, account_id: str, actor: str) -> None:
        with self.transaction() as c:
            row = c.execute('SELECT permit_epoch FROM account_control WHERE account_id=?', (account_id,)).fetchone()
            now = self._clock_us()
            c.execute("UPDATE account_control SET permit='none',permit_epoch=permit_epoch+1,version=version+1,updated_at_us=? "
                      "WHERE account_id=?", (now, account_id))
            key = 'disarm-' + content_hash((account_id, actor, now))
            c.execute("INSERT INTO control_events VALUES (?,?,?,?,'disarm',?,?,?)",
                      (key, account_id, key, row[0] + 1, actor, 'operator disarm', now))

    # ---------- Task 15: approval-bound release registry and boundary activation ----------
    # Activations are operator plans from release.plan_activation, stored as append-only
    # control_events ('activate_release') and applied only at their hourly boundary.
    # Latches, order intents and history are never reset; there is no auto-activation.

    def _activation_events(self, account_id: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT event_id, reason, recorded_at_us FROM control_events WHERE account_id=? AND kind='activate_release' "
            "ORDER BY rowid", (account_id,)).fetchall()
        return [dict(strict_json(r['reason']), event_id=r['event_id']) for r in rows]

    def register_release(self, approved) -> str:
        """Append an approved, approval-free bundle and its approval records in one transaction (idempotent).

        The commit point re-checks approvals itself: all six gates, no model/process approver, every approval bound."""
        from .contracts import ApprovedBundle
        from .release import approval_refusal
        if type(approved) is not ApprovedBundle:
            raise ContractViolation('typed approved bundle required')
        if (refusal := approval_refusal(approved)) is not None:
            raise ContractViolation(refusal)
        bundle = approved.bundle
        with self.transaction() as c:
            existing = c.execute('SELECT manifest_json FROM release_bundles WHERE release_hash=?', (approved.bundle_hash,)).fetchone()
            manifest = canonical_bytes(bundle.to_dict()).decode()
            if existing:
                if existing[0] != manifest:
                    raise ContractViolation('release identity/content conflict')
                return approved.bundle_hash
            parent = bundle.parent_hash if bundle.parent_hash and c.execute(
                'SELECT 1 FROM release_bundles WHERE release_hash=?', (bundle.parent_hash,)).fetchone() else None
            c.execute('INSERT INTO release_bundles VALUES (?,?,?,?,?)',
                      (approved.bundle_hash, manifest, parent, bundle.expires_at_us, self._clock_us()))
            for approval in approved.approvals:
                c.execute("INSERT INTO release_events VALUES (?,?,'approval',?,?,?)",
                          ('approval-' + content_hash((approved.bundle_hash, approval)), approved.bundle_hash,
                           approval.actor, content_hash(approval), self._clock_us()))
        return approved.bundle_hash

    def active_approved(self, account_id: str):
        """The ApprovedBundle of the last applied activation, if it is still the active release."""
        from .contracts import ApprovedBundle
        active = self.connection.execute('SELECT active_release_hash FROM account_control WHERE account_id=?',
                                         (account_id,)).fetchone()[0]
        applied = [e for e in self._activation_events(account_id) if e['phase'] == 'applied']
        if not applied or applied[-1]['bundle_hash'] != active:
            return None
        return ApprovedBundle.from_json(applied[-1]['approved_json'])

    def entries_allowed_now(self, account_id: str) -> bool:
        """Entries consult release.entries_allowed whenever the active release came from the registry.

        An active release set outside the registry (legacy fixtures) keeps the existing expiry check."""
        from .release import entries_allowed
        if not any(e['phase'] == 'applied' for e in self._activation_events(account_id)):
            return True
        return entries_allowed(self.active_approved(account_id), self._clock_us())

    def activation_state(self, account_id: str):
        from .release import ActivationState
        account = self._account_view(account_id) if self.connection.in_transaction else self.account_view(account_id)
        row = self.connection.execute('SELECT * FROM account_control WHERE account_id=?', (account_id,)).fetchone()
        events = self._activation_events(account_id)
        closed = {e['plan_hash'] for e in events if e['phase'] in ('applied', 'refused')}
        scheduled = [e for e in events if e['phase'] == 'scheduled' and e['plan_hash'] not in closed]
        latches = tuple(name for name in ('hard_killed', 'drawdown_latched', 'daily_halted', 'crisis_halted') if row[name])
        pending = tuple(r[0] for r in self.connection.execute(
            "SELECT intent_id FROM order_intents WHERE account_id=? AND state NOT IN ('filled','canceled','rejected') ORDER BY intent_id",
            (account_id,)))
        in_flight = tuple(r[0] for r in self.connection.execute(
            "SELECT d.decision_id FROM decisions d JOIN snapshots s USING(snapshot_id) WHERE s.account_id=? AND d.status='pending' "
            "ORDER BY d.decision_id", (account_id,)))
        return ActivationState(account_id=account_id, active=self.active_approved(account_id), armed=row['permit'] != 'none',
            reconciled=account.ready, unknown_intent_ids=account.unknown_intent_ids, pending_action_keys=pending,
            in_flight_judgment_ids=in_flight, risk_latches=latches,
            history=tuple(e['bundle_hash'] for e in events if e['phase'] == 'applied'),
            boundary_in_progress=bool(scheduled))

    def schedule_activation(self, plan, request_id: str, *, account_id: str) -> str:
        """Durably record one operator plan; nothing changes until its boundary.

        Re-checks at the commit point: approvals (gates, operator-only approvers and actor, bound hashes), and the
        current account state (unarmed, no unknown intents, reconciled, history unchanged). Registration and the
        scheduled event commit together."""
        from .release import ActivationPlan, HOUR_US, approval_refusal
        if type(plan) is not ActivationPlan or type(request_id) is not str or not request_id:
            raise ContractViolation('typed activation plan required')
        self._require_account(account_id)
        if (refusal := approval_refusal(plan.approved, plan.actor)) is not None:
            raise ContractViolation(refusal)
        if plan.effective_at_us % HOUR_US or plan.effective_at_us <= self._clock_us():
            raise ContractViolation('activation boundary must be a future hour')
        state = self.activation_state(account_id)
        if state.boundary_in_progress:
            raise ContractViolation('an activation is already scheduled')
        for failed, code in ((state.armed, 'ARMED'), (bool(state.unknown_intent_ids), 'PENDING_UNKNOWN_ORDER'),
                             (not state.reconciled, 'UNRECONCILED')):
            if failed:
                raise ContractViolation(code)
        if (plan.previous_hash != (state.active.bundle_hash if state.active else None)
                or plan.history != state.history + (plan.approved.bundle_hash,)):
            raise ContractViolation('activation plan does not extend current history')
        with self.transaction() as c:
            self.register_release(plan.approved)
            epoch = c.execute('SELECT permit_epoch FROM account_control WHERE account_id=?', (account_id,)).fetchone()[0]
            payload = {'phase': 'scheduled', 'plan_hash': plan.plan_hash, 'bundle_hash': plan.approved.bundle_hash,
                       'previous_hash': plan.previous_hash, 'effective_at_us': plan.effective_at_us, 'actor': plan.actor,
                       'rollback': plan.rollback, 'invalidated_judgment_ids': list(plan.invalidated_judgment_ids),
                       'retained_action_keys': list(plan.retained_action_keys), 'risk_latches': list(plan.risk_latches),
                       'history': list(plan.history), 'approved_json': plan.approved.to_json()}
            c.execute("INSERT INTO control_events VALUES (?,?,?,?,'activate_release',?,?,?)",
                      ('activation-scheduled-' + plan.plan_hash, account_id, request_id, epoch, plan.actor,
                       canonical_bytes(payload).decode(), self._clock_us()))
        return plan.plan_hash

    def apply_due_activation(self, account_id: str) -> str | None:
        """Commit a scheduled plan atomically once its boundary has passed; refusals close the plan.

        Re-verifies approvals and requires: unarmed, not hard-killed, reconciled (ready), no unknown intents,
        active release unchanged and bundle unexpired. Callers hold the authority's account lock."""
        from .contracts import ApprovedBundle
        from .release import approval_refusal
        with self.transaction() as c:
            events = self._activation_events(account_id)
            closed = {e['plan_hash'] for e in events if e['phase'] in ('applied', 'refused')}
            due = [e for e in events if e['phase'] == 'scheduled' and e['plan_hash'] not in closed
                   and e['effective_at_us'] <= self._clock_us()]
            if not due:
                return None
            plan = due[0]
            row = c.execute('SELECT * FROM account_control WHERE account_id=?', (account_id,)).fetchone()
            current = self.active_approved(account_id)
            account = self._account_view(account_id)
            try:
                approved = ApprovedBundle.from_json(plan['approved_json'])
                approvals = approval_refusal(approved, plan['actor']) or (
                    None if approved.bundle_hash == plan['bundle_hash'] else 'UNBOUND_APPROVAL')
            except Exception:
                approved, approvals = None, 'UNREADABLE_APPROVAL'
            reason = (approvals or ('HARD_KILL' if row['hard_killed'] else None)
                      or ('ARMED' if row['permit'] != 'none' else None)
                      or ('PENDING_UNKNOWN_ORDER' if account.unknown_intent_ids else None)
                      or ('UNRECONCILED' if not account.ready else None)
                      or ('ACTIVE_CHANGED' if (current.bundle_hash if current else None) != plan['previous_hash'] else None)
                      or ('BUNDLE_EXPIRED' if approved.bundle.expires_at_us <= self._clock_us() else None))
            outcome = dict(plan, phase='refused' if reason else 'applied', reason=reason)
            outcome.pop('event_id')
            c.execute("INSERT INTO control_events VALUES (?,?,?,?,'activate_release',?,?,?)",
                      (f"activation-{outcome['phase']}-" + plan['plan_hash'], account_id,
                       f"activation-{outcome['phase']}-" + plan['plan_hash'], row['permit_epoch'], plan['actor'],
                       canonical_bytes(outcome).decode(), self._clock_us()))
            if reason:
                return 'refused:' + reason
            c.execute('UPDATE account_control SET active_release_hash=?,version=version+1,updated_at_us=? WHERE account_id=?',
                      (plan['bundle_hash'], self._clock_us(), account_id))
            c.execute("INSERT INTO release_events VALUES (?,?,?,?,?,?)",
                      ('activation-' + plan['plan_hash'], plan['bundle_hash'], 'rollback' if plan['rollback'] else 'activation',
                       plan['actor'], plan['plan_hash'], self._clock_us()))
            return 'applied'

    def clear_latch(self, account_id: str, latch: str, actor: str, reason: str = '') -> str | None:
        """Operator latch clearing (reached only through the authenticated control path). Returns a refusal or None.

        Rules: DRAWDOWN is never clearable (LATCH_PERSISTENT); DAILY clears only by UTC rollover
        (CLEARS_AT_ROLLOVER); CRISIS needs an unarmed, ready, freshly marked account with no unknown intents;
        KILL needs no unresolved orders. Every clear needs an operator reason and leaves the account unarmed."""
        columns = {'kill': 'hard_killed', 'drawdown': 'drawdown_latched', 'daily': 'daily_halted', 'crisis': 'crisis_halted'}
        if latch not in columns or type(actor) is not str or not actor:
            raise ContractViolation('invalid latch clear')
        if latch == 'drawdown':
            return 'LATCH_PERSISTENT'
        if latch == 'daily':
            return 'CLEARS_AT_ROLLOVER'
        if type(reason) is not str or not reason.strip():
            return 'REASON_REQUIRED'
        with self.transaction() as c:
            row = c.execute('SELECT * FROM account_control WHERE account_id=?', (account_id,)).fetchone()
            if not row[columns[latch]]:
                return 'NOT_LATCHED'
            if latch == 'kill' and c.execute("SELECT 1 FROM order_intents WHERE account_id=? AND state IN "
                                             "('dispatching','open','partial','unknown') LIMIT 1", (account_id,)).fetchone():
                return 'UNRESOLVED_ORDERS'
            if latch == 'crisis':
                if row['permit'] != 'none':
                    return 'ARMED'
                account = self._account_view(account_id)
                if account.unknown_intent_ids:
                    return 'PENDING_UNKNOWN_ORDER'
                if not account.ready or account.exact_nav is None:  # ready implies an exact NAV mark within 2 s
                    return 'UNRECONCILED'
            now = self._clock_us()
            c.execute(f"UPDATE account_control SET {columns[latch]}=0,permit='none',permit_epoch=permit_epoch+1,"
                      "halt_reason=NULL,version=version+1,updated_at_us=? WHERE account_id=?", (now, account_id))
            key = 'clear-' + content_hash((account_id, latch, now, actor))
            c.execute("INSERT INTO control_events VALUES (?,?,?,?,'recovery',?,?,?)",
                      (key, account_id, key, row['permit_epoch'] + 1, actor, f'operator clear {latch}: {reason}'[:1024], now))
            return None

    def _release_killed_reservations(self, account_id: str) -> None:
        """Reject orders that never left RESERVED. In-flight orders stay for reconciliation."""
        ids = [row[0] for row in self.connection.execute(
            "SELECT intent_id FROM order_intents WHERE account_id=? AND state='reserved'", (account_id,))]
        released = 0
        for intent_id in ids:
            if self.reject_undispatched(intent_id, "hard_kill"):
                released += 1

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
        if drawdown_next or daily_next:
            self._release_inhibited_entries(account_id)

    def _release_inhibited_entries(self, account_id: str) -> None:
        """Drop reserved buys when a loss latch closes new exposure. Sells stay for an exit."""
        ids = [row[0] for row in self.connection.execute(
            "SELECT intent_id FROM order_intents WHERE account_id=? AND state='reserved' AND side='buy'", (account_id,))]
        released = 0
        for intent_id in ids:
            if self.reject_undispatched(intent_id, "risk_halt"):
                released += 1

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
