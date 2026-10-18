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
        'resolve_review', 'activate_release', 'reconcile', 'export_session'}),
    'reflex': frozenset({'submit_judgment'}),
    'brain': frozenset({'import_proposal', 'import_label_events'}),
    'reader': frozenset({'status'}),
}
_PAYLOAD_FIELDS = {
    'status': {}, 'arm': {'reason': str, 'release_hash': str},
    'disarm': {'reason': str}, 'kill': {'reason': str}, 'reduce': {'reason': str, 'incident_id': str},
    'cancel': {'intent_id': str}, 'resolve_review': {'incident_id': str, 'review_id': str,
        'finding_hash': str, 'reason': str},
    'activate_release': {'release_hash': str, 'reason': str}, 'reconcile': {'reason': str},
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

    def __post_init__(self):
        if (any(not _text(v) for v in (self.session_id, self.actor, self.account_id, self.boot_id, self.token))
                or self.role not in _ROLES or self.mode not in {'paper', 'live'}
                or type(self.peer) is not PeerCredentials
                or any(not _integer(v) for v in (self.peer.pid, self.peer.uid, self.peer.gid, self.expires_mono_ns))
                or not self.nonces or any(not _text(k) or not _integer(v) for k, v in self.nonces.items())):
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
        if (session is None or type(peer) is not PeerCredentials or session.peer != peer
                or not secrets.compare_digest(session.token.encode(), request.token.encode())
                or request.actor != session.actor or request.account_id != session.account_id
                or request.mode != session.mode or request.boot_id != session.boot_id
                or request.boot_id != self.boot_id or request.account_id != self.backend.account_id
                or request.mode != self.backend.mode or not _integer(now)
                or now >= min(session.expires_mono_ns, request.deadline_mono_ns,
                    session.nonces.get(request.nonce, 0))):
            raise ControlRejected('AUTHENTICATION_FAILED')
        if not command_allowed(session.role, request.kind, False):
            raise ControlRejected('ROLE_FORBIDDEN')
        def authorize():
            now = self.monotonic_ns()
            if not _integer(now) or now >= min(request.deadline_mono_ns, session.expires_mono_ns,
                    session.nonces[request.nonce]):
                raise ControlRejected('EXPIRED')
        def operation():
            try:
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

    def __init__(self, ledger, authority, *, account_id: str, mode: str,
            handlers: Mapping[str, Callable[[ControlRequest], ControlReceipt]] | None = None):
        if authority.ledger is not ledger or mode not in {'paper', 'live'} or not _text(account_id):
            raise ControlRejected('INVALID_AUTHORITY_CONFIGURATION')
        self.ledger, self.authority, self.account_id, self.mode = ledger, authority, account_id, mode
        self.dispatch_mutex = authority._lock(account_id)
        self.handlers = MappingProxyType(dict(handlers or {}))
        if any(kind not in _PAYLOAD_FIELDS or kind in {'kill', 'status'} for kind in self.handlers):
            raise ControlRejected('INVALID_HANDLER_CONFIGURATION')
        self._state()

    def _state(self):
        row = self.ledger.connection.execute(
            'SELECT a.mode,c.* FROM accounts a JOIN account_control c USING(account_id) WHERE a.account_id=?',
            (self.account_id,)).fetchone()
        if row is None or row['mode'] != self.mode:
            raise ControlRejected('ACCOUNT_MODE_MISMATCH')
        return row

    def dispatch(self, request: ControlRequest, *, role: str, authorize: Callable[[], None]):
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
            handler = self.handlers.get(request.kind)
            if handler is None:
                raise ControlRejected('HANDLER_UNAVAILABLE')
            receipt = handler(request)
            if type(receipt) is not ControlReceipt or receipt.request_id != request.request_id:
                raise ControlRejected('INVALID_HANDLER_RECEIPT')
            return response(request, receipt.status.value, receipt.control_epoch, reason_codes=receipt.reason_codes)

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
