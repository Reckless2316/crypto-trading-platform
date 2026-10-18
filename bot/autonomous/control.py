"""Authenticated, bounded local control transport; construction never arms accounts.

Sessions are trusted process wiring, not deserialized role claims. The file journal
owns protocol request/nonce receipts; the ledger remains the authority for account
state. Interrupted callbacks remain unknown and are never automatically repeated.
No browser/session issuance, live wiring, or automatic kill reset is provided.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import socket
import stat
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .contracts import ControlCommand, ControlKind, ControlReceipt, content_hash

MAX_MESSAGE_BYTES = 16_384
_ROLES = {
    'operator': frozenset({'status', 'arm', 'disarm', 'kill', 'reduce', 'cancel',
        'resolve_review', 'activate_release', 'reconcile', 'export_session', 'clear_latch'}),
    'reflex': frozenset({'submit_judgment'}),
    'brain': frozenset({'import_proposal', 'import_label_events'}),
    'reader': frozenset({'status'}),
    # The authority worker's own permit: it can only renew a live deadman lease.
    'authority': frozenset({'renew'}),
}
MAX_LEASE_NS = 120_000_000_000
MAX_SESSION_TTL_S = 3_600
# Session issuance for a separate operator CLI process. Never model/reflex/brain/authority roles.
ISSUABLE_ROLES = ('operator', 'reader')
_PAYLOAD_FIELDS = {
    'status': {}, 'arm': {'reason': str, 'release_hash': str},
    'disarm': {'reason': str}, 'kill': {'reason': str}, 'reduce': {'reason': str, 'incident_id': str},
    'cancel': {'intent_id': str}, 'resolve_review': {'incident_id': str, 'review_id': str,
        'finding_hash': str, 'reason': str},
    'activate_release': {'release_hash': str, 'reason': str}, 'reconcile': {'reason': str},
    'clear_latch': {'latch': str, 'reason': str}, 'renew': {'reason': str},
    'export_session': {'cutoff_us': int}, 'submit_judgment': {'artifact_hash': str},
    'import_proposal': {'artifact_hash': str}, 'import_label_events': {'artifact_hash': str},
}


class ControlRejected(ValueError):
    """A protocol or authorization refusal; the message is a stable reason code."""


def command_allowed(role: str, kind: str, hard_killed: bool) -> bool:
    return (type(role) is str and type(kind) is str and type(hard_killed) is bool
        and kind in _ROLES.get(role, ())
        and not (hard_killed and kind in {'arm', 'reduce'}))


def _text(value, maximum=256):
    return type(value) is str and 0 < len(value) <= maximum and all(ord(c) >= 32 for c in value)


def _integer(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def _json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ControlRejected('DUPLICATE_FIELD')
            result[key] = value
        return result
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_MESSAGE_BYTES:
        raise ControlRejected('MESSAGE_SIZE')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ControlRejected('INVALID_JSON')))
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ControlRejected('INVALID_JSON') from exc


def _encoded(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise ControlRejected('INVALID_RESPONSE') from exc
    if not 0 < len(raw) <= MAX_MESSAGE_BYTES:
        raise ControlRejected('MESSAGE_SIZE')
    return raw


@dataclass(frozen=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int


@dataclass(frozen=True)
class ControlSession:
    session_id: str
    actor: str
    role: str
    account_id: str
    mode: str
    boot_id: str
    token: str
    peer: PeerCredentials
    expires_mono_ns: int
    nonces: Mapping[str, int]
    peer_match: str = 'pid'  # 'uid' only for sessions issued to a separate operator CLI process

    def __post_init__(self):
        if (any(not _text(v) for v in (self.session_id, self.actor, self.account_id, self.boot_id, self.token))
                or self.role not in _ROLES or self.mode not in {'paper', 'live'}
                or type(self.peer) is not PeerCredentials
                or any(not _integer(v) for v in (self.peer.pid, self.peer.uid, self.peer.gid, self.expires_mono_ns))
                or not self.nonces or any(not _text(k) or not _integer(v) for k, v in self.nonces.items())
                or self.peer_match not in ('pid', 'uid')):
            raise ControlRejected('INVALID_SESSION_CONFIGURATION')
        object.__setattr__(self, 'nonces', MappingProxyType(dict(self.nonces)))


@dataclass(frozen=True)
class ControlRequest:
    request_id: str
    account_id: str
    mode: str
    kind: str
    actor: str
    expected_epoch: int
    boot_id: str
    deadline_mono_ns: int
    session_id: str
    token: str
    nonce: str
    payload: Mapping[str, Any]

    @property
    def semantic_hash(self):
        return content_hash({key: dict(value) if key == 'payload' else value
            for key, value in self.__dict__.items() if key not in {'token', 'session_id', 'nonce'}})

    def control_command(self) -> ControlCommand:
        """Adapt existing typed command kinds; broader handlers remain unavailable."""
        kinds = {'arm': ControlKind.ARM, 'disarm': ControlKind.DISARM,
            'kill': ControlKind.HARD_KILL, 'activate_release': ControlKind.ACTIVATE_RELEASE}
        if self.kind not in kinds:
            raise ControlRejected('CONTROL_COMMAND_UNAVAILABLE')
        return ControlCommand(self.request_id, self.account_id, kinds[self.kind], self.actor,
            self.payload.get('reason', self.kind), self.expected_epoch, self.boot_id,
            self.deadline_mono_ns, self.payload.get('release_hash'))


def decode_request(raw: bytes) -> ControlRequest:
    value = _json(raw)
    expected = set(ControlRequest.__dataclass_fields__) | {'version'}
    if type(value) is not dict or set(value) != expected or type(value['version']) is not int or value['version'] != 1:
        raise ControlRejected('INVALID_ENVELOPE')
    if (any(not _text(value[key]) for key in ('request_id', 'account_id', 'kind', 'actor',
                'boot_id', 'session_id', 'token', 'nonce'))
            or value['mode'] not in ('paper', 'live')
            or not _integer(value['expected_epoch']) or not _integer(value['deadline_mono_ns'])):
        raise ControlRejected('INVALID_ENVELOPE')
    kind, payload = value['kind'], value['payload']
    schema = _PAYLOAD_FIELDS.get(kind)
    if schema is None or type(payload) is not dict or set(payload) - set(schema):
        raise ControlRejected('INVALID_PAYLOAD')
    for key, item in payload.items():
        if (schema[key] is str and not _text(item, 1024)
                or schema[key] is int and not _integer(item)):
            raise ControlRejected('INVALID_PAYLOAD')
        if key.endswith('_hash') and (len(item) != 64 or any(c not in '0123456789abcdef' for c in item)):
            raise ControlRejected('INVALID_PAYLOAD')
    value.pop('version')
    value['payload'] = MappingProxyType(dict(payload))
    return ControlRequest(**value)


def response(request, status, epoch, *, reason_codes=(), local_inhibited=False,
        cancellations_pending=(), data=None):
    return {'version': 1, 'request_id': request.request_id, 'status': status,
        'control_epoch': epoch, 'reason_codes': list(reason_codes),
        'local_inhibited': local_inhibited, 'cancellations_pending': list(cancellations_pending),
        'data': {} if data is None else data}


def _private_directory(path: Path):
    missing, current = [], path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        for durable in (directory, directory.parent):
            descriptor = os.open(durable, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ControlRejected('UNSAFE_DIRECTORY')


class FileControlJournal:
    """Durable local request/nonce claims and exact receipts, shared across restarts.

    Claim fsync precedes the callback. A crash between claim and receipt yields
    OUTCOME_UNKNOWN, never another callback. This is deliberately conservative;
    recovery must inspect authority state and use a fresh authorized request.
    The runtime directory is trusted configuration and must be authority-private.
    Tokens and raw commands are never stored. This journal cannot grant permits.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        _private_directory(self.directory)

    def _read(self, path):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ControlRejected('UNSAFE_JOURNAL')
            return _json(handle.read(MAX_MESSAGE_BYTES + 1))

    def _write(self, path, value):
        raw = _encoded(value)
        descriptor, name = tempfile.mkstemp(dir=self.directory, prefix='.pending-')
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def execute_once(self, request: ControlRequest, operation: Callable[[], dict]) -> dict:
        key = content_hash((request.account_id, request.request_id))
        nonce_key = content_hash((request.session_id, request.nonce))
        lock_fd = os.open(self.directory / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd, 'a+b') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            request_path, nonce_path = self.directory / ('request-' + key), self.directory / ('nonce-' + nonce_key)
            previous, nonce = self._read(request_path), self._read(nonce_path)
            if previous is not None:
                if previous.get('request_hash') != request.semantic_hash:
                    raise ControlRejected('REQUEST_CONFLICT')
                return previous['receipt']
            if nonce is not None and nonce.get('request_key') != key:
                raise ControlRejected('NONCE_REPLAY')
            self._write(nonce_path, {'request_key': key})
            pending = response(request, 'pending', request.expected_epoch, reason_codes=('OUTCOME_UNKNOWN',))
            self._write(request_path, {'request_hash': request.semantic_hash, 'receipt': pending})
            receipt = operation()
            self._write(request_path, {'request_hash': request.semantic_hash, 'receipt': receipt})
            return receipt


class ControlService:
    def __init__(self, *, backend, journal: FileControlJournal, sessions: list[ControlSession],
            boot_id: str, monotonic_ns: Callable[[], int]):
        if (not _text(boot_id) or not sessions or any(type(s) is not ControlSession for s in sessions)
                or len({s.session_id for s in sessions}) != len(sessions)):
            raise ControlRejected('INVALID_SERVICE_CONFIGURATION')
        self.backend, self.journal, self.boot_id = backend, journal, boot_id
        self.sessions = MappingProxyType({session.session_id: session for session in sessions})
        self.monotonic_ns = monotonic_ns

    def handle(self, raw: bytes, peer: PeerCredentials):
        request = decode_request(raw)
        session, now = self.sessions.get(request.session_id), self.monotonic_ns()
        peer_ok = type(peer) is PeerCredentials and session is not None and (
            session.peer == peer if session.peer_match == 'pid' else session.peer.uid == peer.uid)
        if (session is None or not peer_ok
                or not secrets.compare_digest(session.token.encode(), request.token.encode())
                or request.actor != session.actor or request.account_id != session.account_id
                or request.mode != session.mode or request.boot_id != session.boot_id
                or request.boot_id != self.boot_id or request.account_id != self.backend.account_id
                or request.mode != self.backend.mode or not _integer(now)
                or now >= min(session.expires_mono_ns, request.deadline_mono_ns,
                    session.nonces.get(request.nonce, 0))):
            raise ControlRejected('AUTHENTICATION_FAILED')
        if request.kind == 'arm' and request.mode == 'live':
            raise ControlRejected('LIVE_ARM_REQUIRES_TASK22')  # whatever the role; live arming is Task 22
        if not command_allowed(session.role, request.kind, False):
            raise ControlRejected('ROLE_FORBIDDEN')
        def authorize():
            now = self.monotonic_ns()
            if not _integer(now) or now >= min(request.deadline_mono_ns, session.expires_mono_ns,
                    session.nonces[request.nonce]):
                raise ControlRejected('EXPIRED')
        def operation():
            try:
                if getattr(self.backend, 'accepts_session', False):
                    return self.backend.dispatch(request, role=session.role, authorize=authorize, session=session)
                return self.backend.dispatch(request, role=session.role, authorize=authorize)
            except ControlRejected as exc:
                return response(request, 'rejected', request.expected_epoch, reason_codes=(str(exc),))
        return self.journal.execute_once(request, operation)


class LedgerControlBackend:
    """Concrete ledger status/kill boundary sharing the exact authority mutex.

    No permissive arm/renew/release/review implementation is provided. Additional
    handlers are trusted authority code and must perform their own full policy,
    reconciliation and durable epoch checks. The private authority lock accessor
    is a narrow integration seam until execution exposes a public accessor.
    """

    accepts_session = True

    def __init__(self, ledger, authority, *, account_id: str, mode: str,
            handlers: Mapping[str, Callable[[ControlRequest], ControlReceipt]] | None = None,
            release_catalog: Callable[[str], tuple | None] | None = None, release_verifier=None,
            on_clear: Callable[[str], None] | None = None, lease_ns: int = 30_000_000_000):
        """``release_catalog`` (release_hash -> (bundle, approvals, artifacts)) and ``release_verifier`` are trusted
        authority wiring for the built-in ``activate_release``; ``on_clear`` lets the hosting runtime mirror an
        operator latch clear into its own latch log. ``clear_latch`` is built in and operator-only."""
        if authority.ledger is not ledger or mode not in {'paper', 'live'} or not _text(account_id):
            raise ControlRejected('INVALID_AUTHORITY_CONFIGURATION')
        self.ledger, self.authority, self.account_id, self.mode = ledger, authority, account_id, mode
        self.dispatch_mutex = authority._lock(account_id)
        self.handlers = MappingProxyType(dict(handlers or {}))
        self.release_catalog, self.release_verifier, self.on_clear = release_catalog, release_verifier, on_clear
        if type(lease_ns) is not int or lease_ns <= 0:
            raise ControlRejected('INVALID_LEASE_CONFIGURATION')
        self.lease_ns = min(lease_ns, MAX_LEASE_NS)
        builtin = {'kill', 'status', 'clear_latch', 'arm', 'renew', 'disarm'} | ({'activate_release'} if release_catalog else set())
        if any(kind not in _PAYLOAD_FIELDS or kind in builtin for kind in self.handlers):
            raise ControlRejected('INVALID_HANDLER_CONFIGURATION')
        self._state()

    def _state(self):
        row = self.ledger.connection.execute(
            'SELECT a.mode,c.* FROM accounts a JOIN account_control c USING(account_id) WHERE a.account_id=?',
            (self.account_id,)).fetchone()
        if row is None or row['mode'] != self.mode:
            raise ControlRejected('ACCOUNT_MODE_MISMATCH')
        return row

    def dispatch(self, request: ControlRequest, *, role: str, authorize: Callable[[], None], session=None):
        with self.dispatch_mutex:
            authorize()
            row = self._state()
            if request.account_id != self.account_id or request.mode != self.mode:
                raise ControlRejected('ACCOUNT_MODE_MISMATCH')
            if request.expected_epoch != row['permit_epoch']:
                raise ControlRejected('STALE_EPOCH')
            if not command_allowed(role, request.kind, bool(row['hard_killed'])):
                raise ControlRejected('COMMAND_FORBIDDEN')
            if request.kind == 'status':
                return response(request, 'completed', row['permit_epoch'], data={
                    'hard_killed': bool(row['hard_killed']), 'permit': row['permit'],
                    'drawdown_latched': bool(row['drawdown_latched']),
                    'daily_halted': bool(row['daily_halted']), 'crisis_halted': bool(row['crisis_halted']),
                    'lease_boot_id': row['lease_boot_id'], 'lease_deadline_mono_ns': row['lease_deadline_mono_ns'],
                    'active_release_hash': row['active_release_hash'], 'unresolved_intent_ids': self._unresolved()})
            if request.kind == 'kill':
                # latch_operator_kill commits before returning. The same mutex
                # guards final order dispatch; no cancellation call precedes it.
                self.ledger.latch_operator_kill(self.account_id)
                after = self._state()
                if not after['hard_killed'] or self.ledger.connection.in_transaction:
                    raise ControlRejected('KILL_NOT_DURABLE')
                pending = self._unresolved()
                return response(request, 'pending' if pending else 'completed', after['permit_epoch'],
                    local_inhibited=True, cancellations_pending=pending,
                    reason_codes=('CANCELLATION_RECONCILIATION_REQUIRED',) if pending else ())
            if request.kind == 'clear_latch':
                return self._clear_latch(request)
            if request.kind in ('arm', 'renew', 'disarm'):
                return self._lease_command(request)
            if request.kind == 'activate_release' and self.release_catalog is not None:
                return self._activate_release(request, session)
            handler = self.handlers.get(request.kind)
            if handler is None:
                raise ControlRejected('HANDLER_UNAVAILABLE')
            receipt = handler(request)
            if type(receipt) is not ControlReceipt or receipt.request_id != request.request_id:
                raise ControlRejected('INVALID_HANDLER_RECEIPT')
            return response(request, receipt.status.value, receipt.control_epoch, reason_codes=receipt.reason_codes)

    def _authority_boot(self, request):
        config = getattr(self.ledger, '_risk_config', None)
        return config[3] if config else request.boot_id

    def _lease_command(self, request):
        """Operator paper arm / disarm and authority-role lease renewal; all bounded and durable in the ledger."""
        boot = self._authority_boot(request)
        if boot != request.boot_id:
            raise ControlRejected('BOOT_MISMATCH')
        if request.kind == 'arm':
            refusal = self.ledger.arm(self.account_id, request.actor, mode=self.mode, boot_id=boot,
                lease_ns=self.lease_ns, release_hash=request.payload.get('release_hash'))
        elif request.kind == 'renew':
            refusal = self.ledger.renew_lease(self.account_id, request.actor, boot_id=boot, lease_ns=self.lease_ns)
        else:
            refusal = self.ledger.disarm(self.account_id, request.actor)
        after = self._state()
        if refusal is not None:
            return response(request, 'rejected', after['permit_epoch'], reason_codes=(refusal,))
        return response(request, 'completed', after['permit_epoch'], data={
            'permit': after['permit'], 'lease_deadline_mono_ns': after['lease_deadline_mono_ns']})

    def _clear_latch(self, request):
        latch = request.payload.get('latch')
        if latch not in ('kill', 'drawdown', 'daily', 'crisis'):
            raise ControlRejected('INVALID_PAYLOAD')
        refusal = self.ledger.clear_latch(self.account_id, latch, request.actor, request.payload.get('reason', ''))
        after = self._state()
        if refusal is not None:
            return response(request, 'rejected', after['permit_epoch'], reason_codes=(refusal,))
        notes = ()
        if self.on_clear is not None:
            try:
                self.on_clear(latch)
            except Exception as exc:  # the ledger clear is committed; report the mirror failure, never hide it
                notes = ('MIRROR_FAILED:' + type(exc).__name__,)
        return response(request, 'completed', after['permit_epoch'], reason_codes=notes, data={'cleared': latch})

    def _activate_release(self, request, session):
        """Plan (never apply) an operator activation at the next hourly boundary; the ledger applies it there."""
        from . import release
        found = self.release_catalog(request.payload.get('release_hash'))
        row = self._state()
        if found is None:
            return response(request, 'rejected', row['permit_epoch'], reason_codes=('RELEASE_UNKNOWN',))
        bundle, approvals, artifacts = found
        source = getattr(self.authority, 'reconciliation_source', None)
        if source is not None:  # a fresh paper reconciliation; planning requires a reconciled account
            self.ledger.record_mark(source(self.account_id))
        now = self.ledger._clock_us()
        plan = release.plan_activation(bundle=bundle, approvals=approvals, artifacts=artifacts,
            verifier=self.release_verifier, now_us=now, state=self.ledger.activation_state(self.account_id),
            session=session, boundary_us=(now // release.HOUR_US + 1) * release.HOUR_US)
        if type(plan) is not release.ActivationPlan:
            return response(request, 'rejected', row['permit_epoch'], reason_codes=plan.reason_codes)
        try:
            self.ledger.schedule_activation(plan, request.request_id, account_id=self.account_id)
        except Exception as exc:
            return response(request, 'rejected', row['permit_epoch'], reason_codes=(str(exc)[:64] or type(exc).__name__,))
        return response(request, 'pending', row['permit_epoch'], reason_codes=('SCHEDULED_AT_BOUNDARY',),
            data={'plan_hash': plan.plan_hash, 'effective_at_us': plan.effective_at_us, 'bundle_hash': plan.approved.bundle_hash})

    def _unresolved(self):
        return [row[0] for row in self.ledger.connection.execute(
            "SELECT intent_id FROM order_intents WHERE account_id=? AND state IN ('dispatching','open','partial','unknown') ORDER BY intent_id",
            (self.account_id,))]


def _peer(connection):
    if not hasattr(socket, 'SO_PEERCRED'):
        raise ControlRejected('PEER_CREDENTIALS_UNAVAILABLE')
    return PeerCredentials(*struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)))


def _receive(connection):
    def exact(size):
        chunks = bytearray()
        while len(chunks) < size:
            part = connection.recv(size - len(chunks))
            if not part:
                raise ControlRejected('TRUNCATED_FRAME')
            chunks.extend(part)
        return bytes(chunks)
    length = struct.unpack('!I', exact(4))[0]
    if not 0 < length <= MAX_MESSAGE_BYTES:
        raise ControlRejected('MESSAGE_SIZE')
    return exact(length)


def _send(connection, raw):
    if not 0 < len(raw) <= MAX_MESSAGE_BYTES:
        raise ControlRejected('MESSAGE_SIZE')
    connection.sendall(struct.pack('!I', len(raw)) + raw)


class UnixControlServer:
    def __init__(self, path: Path, service: ControlService, *, timeout: float = 2):
        if not 0 < timeout <= 10:
            raise ControlRejected('INVALID_TIMEOUT')
        self.path, self.service, self.timeout = Path(path), service, timeout
        self.socket, self._inode = None, None

    def open(self):
        _private_directory(self.path.parent)
        if self.path.exists() or self.path.is_symlink():
            raise ControlRejected('SOCKET_PATH_EXISTS')
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self._inode = self.path.lstat().st_ino
            connection.settimeout(self.timeout)
            connection.listen(8)
        except BaseException:
            connection.close()
            raise
        self.socket = connection
        return self

    def serve_once(self):
        if self.socket is None:
            raise ControlRejected('SERVER_CLOSED')
        connection, _ = self.socket.accept()
        with connection:
            connection.settimeout(self.timeout)
            try:
                result = self.service.handle(_receive(connection), _peer(connection))
            except ControlRejected as exc:
                result = {'version': 1, 'status': 'rejected', 'reason_codes': [str(exc)]}
            except Exception:
                result = {'version': 1, 'status': 'pending', 'reason_codes': ['AUTHORITY_UNAVAILABLE']}
            _send(connection, _encoded(result))

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None
            if self.path.exists() and self.path.lstat().st_ino == self._inode:
                self.path.unlink()

    def __enter__(self):
        return self.open()

    def __exit__(self, *_):
        self.close()


class UnixControlClient:
    def __init__(self, path: Path, *, timeout: float = 2):
        if not 0 < timeout <= 10:
            raise ControlRejected('INVALID_TIMEOUT')
        self.path, self.timeout = Path(path), timeout

    def request(self, raw: bytes):
        decode_request(raw)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(str(self.path))
            if _peer(connection).uid != os.getuid():
                raise ControlRejected('WRONG_SERVER_PEER')
            _send(connection, raw)
            return _json(_receive(connection))


# ---------- uid-bound session issuance for a separate operator CLI process ----------

def issue_session(directory: Path, *, role: str, actor: str, account_id: str, mode: str, boot_id: str,
        ttl_s: int, nonces: int = 16) -> Path:
    """Mint a short-lived session bound to the requesting uid (not pid) in a private 0600 file.

    Only operator/reader roles are issuable; model, reflex, brain and the authority worker never are.
    The hosting authority loads these with ``load_issued_sessions``."""
    import time
    if role not in ISSUABLE_ROLES or not _text(actor) or not actor.startswith(role + ':'):
        raise ControlRejected('ROLE_NOT_ISSUABLE')
    if type(ttl_s) is not int or not 0 < ttl_s <= MAX_SESSION_TTL_S or type(nonces) is not int or not 0 < nonces <= 256:
        raise ControlRejected('INVALID_SESSION_LIFETIME')
    if mode not in ('paper', 'live') or not _text(account_id) or not _text(boot_id):
        raise ControlRejected('INVALID_SESSION_CONFIGURATION')
    directory = Path(directory)
    _private_directory(directory)
    expires = time.monotonic_ns() + ttl_s * 1_000_000_000
    session = {'session_id': 'issued-' + secrets.token_hex(12), 'token': secrets.token_urlsafe(32), 'actor': actor,
        'role': role, 'account_id': account_id, 'mode': mode, 'boot_id': boot_id, 'uid': os.getuid(), 'gid': os.getgid(),
        'expires_mono_ns': expires, 'nonces': [secrets.token_hex(8) for _ in range(nonces)]}
    path = directory / (session['session_id'] + '.session.json')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(_encoded(session))
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _read_private(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ControlRejected('UNSAFE_SESSION_FILE')
        return _json(handle.read(MAX_MESSAGE_BYTES + 1))


def load_issued_sessions(directory: Path, *, boot_id: str) -> list[ControlSession]:
    """Unexpired issued sessions for this boot, owned by this uid with 0600 files; anything else is ignored."""
    import time
    now, sessions = time.monotonic_ns(), []
    for path in sorted(Path(directory).glob('*.session.json')):
        try:
            raw = _read_private(path)
            if raw['boot_id'] != boot_id or raw['expires_mono_ns'] <= now or raw['role'] not in ISSUABLE_ROLES:
                continue
            sessions.append(ControlSession(raw['session_id'], raw['actor'], raw['role'], raw['account_id'], raw['mode'],
                raw['boot_id'], raw['token'], PeerCredentials(0, raw['uid'], raw['gid']), raw['expires_mono_ns'],
                {nonce: raw['expires_mono_ns'] for nonce in raw['nonces']}, peer_match='uid'))
        except (ControlRejected, KeyError, TypeError, OSError):
            continue
    return sessions


def send_request(session_file: Path, socket_path: Path, kind: str, payload: dict, *, epoch: int,
        request_id: str | None = None) -> dict:
    """Operator CLI client: one authenticated request with the next unused nonce of an issued session."""
    session_file = Path(session_file)
    issued = _read_private(session_file)
    used_path = session_file.parent / (session_file.name + '.used')
    used = _read_private(used_path) if used_path.exists() else []
    nonce = next((n for n in issued['nonces'] if n not in used), None)
    if nonce is None:
        raise ControlRejected('SESSION_NONCES_EXHAUSTED')
    descriptor = os.open(used_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(_encoded(used + [nonce]))
        handle.flush()
        os.fsync(handle.fileno())
    raw = _encoded({'version': 1, 'request_id': request_id or ('cli-' + secrets.token_hex(8)),
        'account_id': issued['account_id'], 'mode': issued['mode'], 'kind': kind, 'actor': issued['actor'],
        'expected_epoch': epoch, 'boot_id': issued['boot_id'], 'deadline_mono_ns': issued['expires_mono_ns'],
        'session_id': issued['session_id'], 'token': issued['token'], 'nonce': nonce, 'payload': payload})
    return UnixControlClient(socket_path).request(raw)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description='Local authenticated control: session issuance and operator requests.')
    sub = parser.add_subparsers(dest='command', required=True)
    issue = sub.add_parser('issue-session', help='mint a short-lived uid-bound operator/reader session file (0600)')
    issue.add_argument('--role', choices=ISSUABLE_ROLES, required=True)
    issue.add_argument('--actor', required=True)
    issue.add_argument('--account', required=True)
    issue.add_argument('--mode', choices=('paper', 'live'), default='paper')
    issue.add_argument('--boot-id', required=True)
    issue.add_argument('--ttl-s', type=int, default=900)
    issue.add_argument('--dir', required=True)
    req = sub.add_parser('request', help='send one authenticated request using an issued session')
    req.add_argument('--session', required=True)
    req.add_argument('--socket', required=True)
    req.add_argument('--kind', required=True)
    req.add_argument('--payload', default='{}')
    req.add_argument('--epoch', type=int, required=True)
    args = parser.parse_args(argv)
    if args.command == 'issue-session':
        path = issue_session(Path(args.dir), role=args.role, actor=args.actor, account_id=args.account, mode=args.mode,
            boot_id=args.boot_id, ttl_s=args.ttl_s)
        print(json.dumps({'session_file': str(path)}))
    else:
        print(json.dumps(send_request(Path(args.session), Path(args.socket), args.kind, json.loads(args.payload),
            epoch=args.epoch)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
