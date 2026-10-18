"""Deny-by-default browser sessions and a bounded authority client.

The operator bootstrap supplies browser sessions, one-time bootstrap tokens and
separate control capabilities inside the web process. Nothing here reads provider
credentials, issues trading permits, opens a ledger, or supplies a default user.
Managed mutations are forwarded to the authority control protocol; when the
authority has no handler the result is an explicit 409 "unavailable" receipt and
nothing is mutated locally.

HANDOFF(claude-opus-5.5 2026-10-18): the control protocol has no session
issuance or epoch-discovery command (control.py is owned elsewhere). Browser and
control sessions are still provisioned by trusted launcher code; a status read
with a stale X-Control-Epoch is rejected (STALE_EPOCH) without revealing the
current epoch, so the UI cannot learn it from the protocol yet.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from bot.autonomous.control import ControlRejected, ControlSession, decode_request, MAX_MESSAGE_BYTES

COOKIE_NAME = 'surge_session'
KILL_RESERVED_NONCES = 1
LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})
FORWARDED_KINDS = frozenset({'status', 'kill', 'arm', 'disarm', 'reduce'})
# Client-side refusals raised before anything is written to the socket. Every
# other client failure may follow a send, so its outcome is unknown.
_PRE_SEND_REFUSALS = frozenset({'WRONG_SERVER_PEER', 'INVALID_TIMEOUT'})
_MAX_STORE_BYTES = 4_194_304


def _receipt(status_code, request_id, status, receipt, reason_codes, **extra):
    body = {'version': 1, 'request_id': request_id, 'status': status, 'receipt': receipt,
        'local_inhibited': False, 'reason_codes': list(reason_codes), 'cancellations_pending': [],
        'data': {}}
    body.update(extra)
    return JSONResponse(status_code=status_code, content=body)


def _unknown(request_id):
    return _receipt(202, request_id, 'pending', 'unknown', ['AUTHORITY_OUTCOME_UNKNOWN'])


def unavailable(detail: str, *, request_id: str | None = None, reason: str = 'HANDLER_UNAVAILABLE'):
    """Typed 409 for a managed command the authority does not implement. No local effect."""
    return _receipt(409, request_id, 'rejected', 'unavailable', [reason], detail=detail)


class NonceStoreUnsafe(RuntimeError):
    pass


class DurableNonceStore:
    """File-backed record of consumed control nonces, request envelopes and bootstrap tokens.

    The file is 0600 in a 0700 directory owned by this user and is replaced
    atomically with fsync. It never stores the control token or the raw envelope:
    a retried request is rebuilt from its stored fields plus the in-memory token.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.stat(directory, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise NonceStoreUnsafe('nonce store directory must be private to this user')
        self._lock = threading.Lock()
        self.used, self.requests, self.bootstraps = set(), {}, set()
        self._load()

    def _load(self):
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        with os.fdopen(descriptor, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise NonceStoreUnsafe('nonce store file must be 0600 and owned by this user')
            raw = handle.read(_MAX_STORE_BYTES + 1)
        if len(raw) > _MAX_STORE_BYTES:
            raise NonceStoreUnsafe('nonce store too large')
        try:
            value = json.loads(raw.decode('utf-8'))
            self.used = {tuple(item) for item in value['used']}
            self.requests = {tuple(json.loads(key)): item for key, item in value['requests'].items()}
            self.bootstraps = set(value['bootstraps'])
        except (ValueError, KeyError, TypeError) as exc:
            raise NonceStoreUnsafe('nonce store unreadable') from exc

    def _save(self):
        value = {'version': 1, 'used': sorted(list(item) for item in self.used),
            'requests': {json.dumps(list(key)): item for key, item in sorted(self.requests.items())},
            'bootstraps': sorted(self.bootstraps)}
        raw = json.dumps(value, separators=(',', ':'), sort_keys=True).encode()
        descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix='.nonces-')
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def claim(self, nonce_key: tuple[str, str], request_key: tuple[str, str], record: dict):
        """Durably consume a nonce for one request before anything is sent."""
        with self._lock:
            if nonce_key in self.used:
                raise NonceStoreUnsafe('nonce already consumed')
            self.used.add(nonce_key)
            self.requests[request_key] = record
            try:
                self._save()
            except BaseException:
                self.used.discard(nonce_key)
                self.requests.pop(request_key, None)
                raise

    def claim_bootstrap(self, token_hash: str) -> bool:
        with self._lock:
            if token_hash in self.bootstraps:
                return False
            self.bootstraps.add(token_hash)
            try:
                self._save()
            except BaseException:
                self.bootstraps.discard(token_hash)
                raise
            return True


@dataclass(frozen=True)
class BrowserSession:
    session_id: str
    actor: str
    role: str
    origin: str
    csrf_token: str
    expires_mono_ns: int
    control: ControlSession

    def __post_init__(self):
        origin = urlsplit(self.origin)
        if (not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', self.session_id)
                or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', self.csrf_token)
                or origin.scheme not in {'http', 'https'} or not origin.netloc
                or origin.path or origin.query or origin.fragment or origin.username
                or self.role not in {'operator', 'reader'}
                or type(self.expires_mono_ns) is not int or self.expires_mono_ns < 0
                or type(self.control) is not ControlSession
                or self.actor != self.control.actor or self.role != self.control.role):
            raise ValueError('Invalid server-provisioned browser session')


def set_session_cookie(response, session: BrowserSession):
    """For trusted bootstrap code; requires a separately provisioned session."""
    response.set_cookie(COOKIE_NAME, session.session_id, httponly=True,
        samesite='strict', secure=session.origin.startswith('https://'), path='/')


def bootstrap_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _host(request: Request) -> str:
    host = request.headers.get('host', '')
    parsed = urlsplit('//' + host)
    return (parsed.hostname or '').lower()


def allowed_host(request: Request) -> bool:
    allowed = getattr(request.app.state, 'allowed_hosts', None) or LOOPBACK_HOSTS
    return _host(request) in allowed


def same_origin(request: Request) -> bool:
    origin = request.headers.get('origin')
    host = request.headers.get('host')
    return bool(origin and host) and origin == f'{request.url.scheme}://{host}'


class AuthorityGateway:
    """Authenticated forwarding of status/kill/arm/disarm/reduce to the control protocol.

    Every forwarded request keeps its original identity across retry/timeout and
    web restarts (via the durable nonce store). Control nonces are finite,
    preprovisioned capabilities; exhaustion requires trusted session renewal,
    never a guessed or locally generated authority nonce.
    """
    def __init__(self, *, client, sessions: list[BrowserSession], nonce_store: DurableNonceStore,
            bootstrap_tokens: Mapping[str, str] | None = None, monotonic_ns=time.monotonic_ns):
        if (not sessions or any(type(s) is not BrowserSession for s in sessions)
                or len({s.session_id for s in sessions}) != len(sessions)):
            raise ValueError('Explicit unique browser sessions required')
        if type(nonce_store) is not DurableNonceStore:
            raise ValueError('Durable nonce store required')
        self.client = client
        self.sessions = MappingProxyType({s.session_id: s for s in sessions})
        tokens = dict(bootstrap_tokens or {})
        if any(not re.fullmatch(r'[0-9a-f]{64}', k) or v not in self.sessions for k, v in tokens.items()):
            raise ValueError('Bootstrap tokens must be sha256 hashes bound to provisioned sessions')
        self.bootstrap_tokens = MappingProxyType(tokens)
        self.nonce_store = nonce_store
        self.monotonic_ns = monotonic_ns
        self._lock = threading.Lock()
        self.control_epoch: int | None = None  # last epoch confirmed by a completed authority reply

    def probe_status(self, epoch: int) -> tuple[dict | None, str | None]:
        """One authenticated status read (one nonce) to confirm the authority epoch. Never retries or guesses."""
        session = next((s for s in self.sessions.values() if s.role == 'operator'), None) or next(iter(self.sessions.values()))
        request_id = 'launch-status-' + uuid.uuid4().hex
        try:
            raw = self._envelope(session, request_id, 'status', epoch, {})
        except HTTPException as exc:
            return None, f'AUTHORITY_UNAVAILABLE:{exc.detail}'
        try:
            reply = self.client.request(raw)
            self._validate_reply(reply, request_id)
        except Exception as exc:  # unavailable socket, protocol failure or unbound reply
            return None, 'AUTHORITY_UNAVAILABLE:' + type(exc).__name__
        if reply['status'] == 'completed':
            self.control_epoch = reply['control_epoch']
            return reply, None
        return reply, 'EPOCH_UNKNOWN:' + ','.join(reply['reason_codes'] or ['REJECTED'])

    def session_alive(self, session, now=None):
        now = self.monotonic_ns() if now is None else now
        return type(now) is int and 0 <= now < min(session.expires_mono_ns, session.control.expires_mono_ns)

    def bootstrap(self, request: Request, token: str) -> BrowserSession:
        """Exchange a one-time launcher-printed token for the session cookie."""
        if type(token) is not str or not 32 <= len(token) <= 256:
            raise HTTPException(401, 'Bootstrap token rejected')
        digest = bootstrap_token_hash(token)
        session_id = next((v for k, v in self.bootstrap_tokens.items()
            if secrets.compare_digest(k, digest)), None)
        session = self.sessions.get(session_id) if session_id else None
        if session is None or not self.session_alive(session):
            raise HTTPException(401, 'Bootstrap token rejected')
        if request.headers.get('origin') != session.origin:
            raise HTTPException(403, 'Origin rejected')
        if not self.nonce_store.claim_bootstrap(digest):
            raise HTTPException(401, 'Bootstrap token already used')
        return session

    def authenticate(self, request: Request, *, mutation=False):
        session = self.sessions.get(request.cookies.get(COOKIE_NAME, ''))
        if session is None or not self.session_alive(session):
            raise HTTPException(401, 'Operator session required')
        if mutation:
            if session.role != 'operator':
                raise HTTPException(403, 'Operator role required')
            if request.headers.get('origin') != session.origin:
                raise HTTPException(403, 'Origin rejected')
            csrf = request.headers.get('x-csrf-token', '')
            if not secrets.compare_digest(csrf.encode(), session.csrf_token.encode()):
                raise HTTPException(403, 'CSRF token rejected')
        return session

    def _encode(self, capability, record):
        value = dict(version=1, request_id=record['request_id'], account_id=capability.account_id,
            mode=capability.mode, kind=record['kind'], actor=capability.actor,
            expected_epoch=record['epoch'], boot_id=capability.boot_id,
            deadline_mono_ns=record['deadline_mono_ns'], session_id=capability.session_id,
            token=capability.token, nonce=record['nonce'], payload=record['payload'])
        raw = json.dumps(value, separators=(',', ':')).encode()
        decode_request(raw)
        return raw

    def _envelope(self, session, request_id, kind, epoch, payload):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id):
            raise HTTPException(422, 'Bounded X-Request-ID required')
        capability = session.control
        key = (session.session_id, request_id)
        identity = [kind, epoch, json.dumps(payload, sort_keys=True, separators=(',', ':'))]
        with self._lock:
            previous = self.nonce_store.requests.get(key)
            if previous is not None:
                if previous['identity'] != identity:
                    raise HTTPException(409, 'Request identity conflict')
                return self._encode(capability, previous)
            now = self.monotonic_ns()
            available = [(nonce, expiry) for nonce, expiry in capability.nonces.items()
                if (capability.session_id, nonce) not in self.nonce_store.used and now < expiry]
            # The final capability is reserved for kill: read-only status polling
            # and other commands must never leave the operator unable to stop.
            if not available or kind != 'kill' and len(available) <= KILL_RESERVED_NONCES:
                raise HTTPException(503, 'Authority session renewal required')
            nonce, expiry = available[0]
            record = dict(request_id=request_id, kind=kind, epoch=epoch, payload=payload, nonce=nonce,
                deadline_mono_ns=min(now + 2_000_000_000, expiry, capability.expires_mono_ns,
                    session.expires_mono_ns), identity=identity)
            try:
                raw = self._encode(capability, record)
            except ControlRejected:
                raise HTTPException(422, 'Invalid control payload') from None
            # Durable before send: a restart can never reuse this nonce.
            self.nonce_store.claim((capability.session_id, nonce), key, record)
            return raw

    def forward(self, request: Request, kind: str, payload: dict | None = None, *,
            require_mode: str | None = None):
        mutation = kind != 'status'
        session = self.authenticate(request, mutation=mutation)
        if kind not in FORWARDED_KINDS:
            return unavailable('Authority handler unavailable')
        if require_mode is not None and session.control.mode != require_mode:
            return unavailable(f'No {require_mode} authority session', reason='MODE_UNAVAILABLE')
        if payload is None:
            payload = {} if kind == 'status' else {'reason': f'authenticated operator {kind}'}
        request_id = request.headers.get('x-request-id') if mutation else 'status-' + uuid.uuid4().hex
        epoch_text = request.headers.get('x-control-epoch', '')
        if not re.fullmatch(r'0|[1-9][0-9]{0,18}', epoch_text) or int(epoch_text) > 2**63 - 1:
            raise HTTPException(422, 'X-Control-Epoch required')
        if request_id is None:
            raise HTTPException(422, 'X-Request-ID required')
        epoch = int(epoch_text)
        raw = self._envelope(session, request_id, kind, epoch, payload)
        try:
            reply = self.client.request(raw)
        except ControlRejected as exc:
            if str(exc) in _PRE_SEND_REFUSALS:
                raise HTTPException(503, 'Authority unavailable') from None
            # Truncated/oversized/invalid replies arrive after the send; an
            # authority rejection is only ever a bound 'rejected' reply.
            return _unknown(request_id)
        except (ConnectionRefusedError, FileNotFoundError, PermissionError):
            raise HTTPException(503, 'Authority unavailable') from None
        except Exception:
            # A send may have happened. The same request identity must be queried
            # or retried; never infer completion or create another economic act.
            return _unknown(request_id)
        try:
            self._validate_reply(reply, request_id)
        except (ValueError, TypeError):
            return _unknown(request_id)
        if reply['status'] == 'completed':
            self.control_epoch = reply['control_epoch']
        if reply['status'] == 'rejected' and 'HANDLER_UNAVAILABLE' in reply['reason_codes']:
            receipt, code = 'unavailable', 409
        else:
            receipt = reply['status']
            code = 409 if receipt == 'rejected' else 202 if receipt == 'pending' else 200
        return JSONResponse(status_code=code, content={**reply, 'receipt': receipt})

    @staticmethod
    def _validate_reply(reply, request_id):
        if type(reply) is not dict or len(json.dumps(reply, allow_nan=False).encode()) > MAX_MESSAGE_BYTES:
            raise ValueError('Invalid response')
        if (set(reply) != {'version', 'request_id', 'status', 'control_epoch', 'reason_codes',
                'local_inhibited', 'cancellations_pending', 'data'}
                or type(reply['version']) is not int or reply['version'] != 1
                or reply['request_id'] != request_id
                or reply['status'] not in {'completed', 'pending', 'rejected'}
                or type(reply['control_epoch']) is not int or not 0 <= reply['control_epoch'] <= 2**63-1
                or type(reply['local_inhibited']) is not bool
                or type(reply['reason_codes']) is not list
                or any(type(item) is not str for item in reply['reason_codes'])
                or type(reply['cancellations_pending']) is not list
                or any(type(item) is not str for item in reply['cancellations_pending'])
                or type(reply['data']) is not dict):
            raise ValueError('Unbound response')


def gateway_for(request: Request, *, mutation=False):
    gateway = getattr(request.app.state, 'authority_gateway', None)
    if gateway is None:
        # Absence of a configured gateway is never a default authenticated user.
        if not request.cookies.get(COOKIE_NAME):
            raise HTTPException(401, 'Operator session required')
        raise HTTPException(503, 'Authority gateway unavailable')
    if type(gateway) is not AuthorityGateway:
        raise HTTPException(503, 'Authority gateway unavailable')
    gateway.authenticate(request, mutation=mutation)
    return gateway


def local_csrf_token(app) -> str:
    token = getattr(app.state, 'local_csrf_token', None)
    if token is None:
        token = secrets.token_urlsafe(32)
        app.state.local_csrf_token = token
    return token


def check_local_post(request: Request):
    """Origin/Host/CSRF guard for non-managed POSTs (display refreshes, research).

    Host must be loopback (or explicitly configured), Origin must equal this
    server's own origin, and X-CSRF-Token must equal either the per-process local
    token (GET /api/csrf, unreadable cross-origin) or the browser session token.
    """
    if not allowed_host(request):
        raise HTTPException(403, 'Host rejected')
    if not same_origin(request):
        raise HTTPException(403, 'Origin rejected')
    supplied = request.headers.get('x-csrf-token', '').encode()
    expected = [local_csrf_token(request.app).encode()]
    gateway = getattr(request.app.state, 'authority_gateway', None)
    if type(gateway) is AuthorityGateway:
        session = gateway.sessions.get(request.cookies.get(COOKIE_NAME, ''))
        if session is not None and gateway.session_alive(session):
            expected.append(session.csrf_token.encode())
    if not supplied or not any(secrets.compare_digest(supplied, token) for token in expected):
        raise HTTPException(403, 'CSRF token rejected')
