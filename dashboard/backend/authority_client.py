"""Deny-by-default browser sessions and a bounded authority client.

The operator bootstrap supplies both browser sessions and separate control
capabilities inside the web process. Nothing here reads provider credentials,
issues trading permits, opens a ledger, or supplies a default authenticated user.

HANDOFF(claude-opus-5.5 2026-10-17): only status and kill forward. Consumed
nonces live in process memory, so after a web restart the authority journal sees
NONCE_REPLAY (reported here as outcome unknown) until sessions are re-provisioned;
per-boot capability issuance is still needed. Session bootstrap and the remaining
Task 17 routes (flatten/reduce permits, orders, arm) live in app.py.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from bot.autonomous.control import ControlRejected, ControlSession, decode_request, MAX_MESSAGE_BYTES

COOKIE_NAME = 'surge_session'
KILL_RESERVED_NONCES = 1
# Client-side refusals raised before anything is written to the socket. Every
# other client failure may follow a send, so its outcome is unknown.
_PRE_SEND_REFUSALS = frozenset({'WRONG_SERVER_PEER', 'INVALID_TIMEOUT'})


def _unknown(request_id):
    return JSONResponse(status_code=202, content={'version': 1,
        'request_id': request_id, 'status': 'pending', 'local_inhibited': False,
        'reason_codes': ['AUTHORITY_OUTCOME_UNKNOWN'], 'cancellations_pending': [],
        'data': {}})


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


class AuthorityGateway:
    """Only status and kill are integrated. Other mutations fail closed.

    Every forwarded request retains its original envelope across retry/timeout.
    Control nonces are finite, preprovisioned capabilities; exhaustion requires
    trusted session renewal, never a guessed or locally generated authority nonce.
    """
    def __init__(self, *, client, sessions: list[BrowserSession], monotonic_ns=time.monotonic_ns):
        if (not sessions or any(type(s) is not BrowserSession for s in sessions)
                or len({s.session_id for s in sessions}) != len(sessions)):
            raise ValueError('Explicit unique browser sessions required')
        self.client = client
        self.sessions = MappingProxyType({s.session_id: s for s in sessions})
        self.monotonic_ns = monotonic_ns
        self._lock = threading.Lock()
        self._requests = {}
        self._nonces = set()

    def authenticate(self, request: Request, *, mutation=False):
        session = self.sessions.get(request.cookies.get(COOKIE_NAME, ''))
        now = self.monotonic_ns()
        if session is None or type(now) is not int or not 0 <= now < min(
                session.expires_mono_ns, session.control.expires_mono_ns):
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

    def _envelope(self, session, request_id, kind, epoch):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id):
            raise HTTPException(422, 'Bounded X-Request-ID required')
        key = (session.session_id, request_id)
        identity = (kind, epoch)
        with self._lock:
            previous = self._requests.get(key)
            if previous is not None:
                if previous[0] != identity:
                    raise HTTPException(409, 'Request identity conflict')
                return previous[1]
            now = self.monotonic_ns()
            capability = session.control
            available = [(nonce, expiry) for nonce, expiry in capability.nonces.items()
                if (capability.session_id, nonce) not in self._nonces and now < expiry]
            # The final capability is reserved for kill: read-only status polling
            # must never leave the operator unable to stop the account.
            if not available or kind != 'kill' and len(available) <= KILL_RESERVED_NONCES:
                raise HTTPException(503, 'Authority session renewal required')
            nonce, expiry = available[0]
            value = dict(version=1, request_id=request_id, account_id=capability.account_id,
                mode=capability.mode, kind=kind, actor=capability.actor,
                expected_epoch=epoch, boot_id=capability.boot_id,
                deadline_mono_ns=min(now + 2_000_000_000, expiry,
                    capability.expires_mono_ns, session.expires_mono_ns),
                session_id=capability.session_id, token=capability.token, nonce=nonce,
                payload={} if kind == 'status' else {'reason': 'authenticated operator kill'})
            raw = json.dumps(value, separators=(',', ':')).encode()
            decode_request(raw)
            self._nonces.add((capability.session_id, nonce))
            self._requests[key] = (identity, raw)
            return raw

    def forward(self, request: Request, kind: str):
        mutation = kind != 'status'
        session = self.authenticate(request, mutation=mutation)
        if kind not in {'status', 'kill'}:
            raise HTTPException(409, 'Authority handler unavailable')
        request_id = request.headers.get('x-request-id') if mutation else 'status-' + uuid.uuid4().hex
        epoch_text = request.headers.get('x-control-epoch', '')
        if not re.fullmatch(r'0|[1-9][0-9]{0,18}', epoch_text) or int(epoch_text) > 2**63 - 1:
            raise HTTPException(422, 'X-Control-Epoch required')
        if request_id is None:
            raise HTTPException(422, 'X-Request-ID required')
        epoch = int(epoch_text)
        raw = self._envelope(session, request_id, kind, epoch)
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
        code = 409 if reply['status'] == 'rejected' else 202 if reply['status'] == 'pending' else 200
        return JSONResponse(status_code=code, content=reply)

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
