"""Offline authenticated local controls; no trading or external service calls."""
import dataclasses
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('bot.autonomous.control'),
            'authenticated control boundary is missing')
        from bot.autonomous import control
        self.c = control
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.peer = control.PeerCredentials(os.getpid(), os.getuid(), os.getgid())
        self.session = control.ControlSession('session-1', 'operator-1', 'operator',
            'account-1', 'paper', 'boot-1', 'secret-token', self.peer, 1000,
            {'nonce-1': 1000, 'nonce-2': 1000})
        self.events = []
        owner = self
        class FakeBackend:
            account_id, mode = 'account-1', 'paper'
            def __init__(self):
                self.epoch, self.killed = 0, False
                self.dispatch_mutex = threading.Lock()
            def dispatch(self, request, *, role, authorize):
                with self.dispatch_mutex:
                    authorize()
                    owner.events.append(request.kind)
                    if request.expected_epoch != self.epoch:
                        raise control.ControlRejected('STALE_EPOCH')
                    if not control.command_allowed('operator', request.kind, self.killed):
                        raise control.ControlRejected('HARD_KILLED')
                    if request.kind == 'kill':
                        self.killed = True
                        return control.response(request, 'completed', self.epoch,
                            local_inhibited=True)
                    if request.kind == 'status':
                        return control.response(request, 'completed', self.epoch,
                            data={'hard_killed': self.killed})
                    raise control.ControlRejected('HANDLER_UNAVAILABLE')
        self.backend = FakeBackend()
        self.journal = control.FileControlJournal(self.root / 'journal')
        self.service = self.make_service()

    def make_service(self, session=None):
        return self.c.ControlService(backend=self.backend, journal=self.journal,
            sessions=[session or self.session], boot_id='boot-1', monotonic_ns=lambda: 10)

    def wire(self, **changes):
        value = dict(version=1, request_id='request-1', account_id='account-1', mode='paper',
            kind='kill', actor='operator-1', expected_epoch=0, boot_id='boot-1',
            deadline_mono_ns=100, session_id='session-1', token='secret-token', nonce='nonce-1',
            payload={'reason': 'operator request'})
        value.update(changes)
        return json.dumps(value).encode()

    def test_roles_and_hard_kill(self):
        for role, kind, killed in [('brain', 'arm', False), ('reflex', 'activate_release', False),
                ('operator', 'reduce', True), ('reader', 'kill', False), ('brain', 'resolve_review', False)]:
            self.assertFalse(self.c.command_allowed(role, kind, killed))
        self.assertTrue(self.c.command_allowed('operator', 'kill', True))
        self.assertTrue(self.c.command_allowed('reader', 'status', True))

    def test_authenticated_kill_replay_and_changed_request_rejection(self):
        first = self.service.handle(self.wire(), self.peer)
        restarted = self.make_service()
        self.assertEqual(first, restarted.handle(self.wire(), self.peer))
        self.assertTrue(first['local_inhibited'])
        self.assertEqual(['kill'], self.events)
        with self.assertRaisesRegex(self.c.ControlRejected, 'REQUEST_CONFLICT'):
            restarted.handle(self.wire(payload={'reason': 'changed'}), self.peer)
        with self.assertRaisesRegex(self.c.ControlRejected, 'NONCE_REPLAY'):
            restarted.handle(self.wire(request_id='request-2'), self.peer)

    def test_wrong_peer_token_actor_boot_mode_and_expired_nonce_reject(self):
        cases = [dict(token='wrong'), dict(actor='brain'), dict(boot_id='other'),
            dict(mode='live'), dict(account_id='other'), dict(session_id='missing'),
            dict(nonce='unknown'), dict(deadline_mono_ns=10)]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(self.c.ControlRejected):
                self.service.handle(self.wire(**changes), self.peer)
        with self.assertRaises(self.c.ControlRejected):
            self.service.handle(self.wire(), dataclasses.replace(self.peer, pid=self.peer.pid + 1))
        expired = self.make_service(dataclasses.replace(self.session, nonces={'nonce-1': 10}))
        with self.assertRaises(self.c.ControlRejected):
            expired.handle(self.wire(), self.peer)
        self.assertEqual([], self.events)

    def test_wire_roles_unknown_fields_duplicate_keys_and_oversize_reject(self):
        malformed = [b'{', b'[]', self.wire(role='operator'), self.wire(payload={'reason': 'ok', 'arm': True}),
            self.wire(version=True), self.wire(expected_epoch=True), self.wire(kind='unknown'),
            self.wire(kind='status', payload={'receipt_request_id': 'unsupported'}),
            self.wire()[:-1] + b',"kind":"arm"}', b'x' * (self.c.MAX_MESSAGE_BYTES + 1)]
        for raw in malformed:
            with self.subTest(raw=raw[:50]), self.assertRaises(self.c.ControlRejected):
                self.service.handle(raw, self.peer)
        self.assertEqual([], self.events)

    def test_server_session_role_controls_authority_and_reader_can_get_status(self):
        for role in ('reader', 'brain', 'reflex'):
            service = self.make_service(dataclasses.replace(self.session, role=role))
            with self.subTest(role=role), self.assertRaises(self.c.ControlRejected):
                service.handle(self.wire(), self.peer)
        service = self.make_service(dataclasses.replace(self.session, role='reader'))
        result = service.handle(self.wire(kind='status', payload={}), self.peer)
        self.assertEqual('completed', result['status'])
        self.assertEqual(['status'], self.events)

    def test_stale_epoch_and_unimplemented_handler_fail_closed(self):
        stale = self.service.handle(self.wire(expected_epoch=1), self.peer)
        self.assertEqual(['STALE_EPOCH'], stale['reason_codes'])
        unavailable = self.service.handle(self.wire(request_id='request-2', nonce='nonce-2',
            kind='arm', payload={'reason': 'request'}), self.peer)
        self.assertEqual(['HANDLER_UNAVAILABLE'], unavailable['reason_codes'])
        self.assertFalse(self.backend.killed)

    def test_durable_in_progress_claim_never_reexecutes_unknown_callback(self):
        request = self.c.decode_request(self.wire())
        calls = []
        def uncertain():
            calls.append(1)
            raise RuntimeError('crash after possible effect')
        with self.assertRaises(RuntimeError):
            self.journal.execute_once(request, uncertain)
        restarted = self.c.FileControlJournal(self.root / 'journal')
        result = restarted.execute_once(request, lambda: self.fail('repeated unknown handler'))
        self.assertEqual([1], calls)
        self.assertEqual('pending', result['status'])
        self.assertFalse(result['local_inhibited'])
        self.assertIn('OUTCOME_UNKNOWN', result['reason_codes'])

    def test_socket_frame_roundtrip_permissions_and_existing_path_refusal(self):
        server = self.c.UnixControlServer(self.root / 'sockets' / 'control.sock', self.service)
        with server:
            self.assertEqual(0o600, server.path.stat().st_mode & 0o777)
            thread = threading.Thread(target=server.serve_once)
            thread.start()
            try:
                reply = self.c.UnixControlClient(server.path).request(self.wire())
            finally:
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(reply['local_inhibited'])
            with self.assertRaises(self.c.ControlRejected):
                self.c.UnixControlServer(server.path, self.service).open()
        self.assertFalse(server.path.exists())


class LedgerControlTests(unittest.TestCase):
    def setUp(self):
        from bot.autonomous import control
        from bot.autonomous.execution import OrderAuthority
        from tests.autonomous.test_ledger import LedgerTests
        self.c = control
        self.fixture = LedgerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.proposal = self.fixture.ready()
        self.ledger = self.fixture.ledger
        self.authority = OrderAuthority(self.ledger, None, book_source=lambda _: None,
            market_source=lambda _: None, entry_verifier=lambda *a: False, clock_us=lambda: 1)
        self.backend = control.LedgerControlBackend(self.ledger, self.authority,
            account_id='paper-test', mode='paper')

    def request(self, kind='kill'):
        return self.c.decode_request(json.dumps(dict(version=1, request_id='real-kill',
            account_id='paper-test', mode='paper', kind=kind, actor='operator', expected_epoch=1,
            boot_id='test-boot', deadline_mono_ns=100, session_id='session', token='token',
            nonce='nonce', payload={'reason': 'requested'} if kind != 'status' else {})).encode())

    def test_expiry_authorization_rechecked_inside_shared_mutex(self):
        import inspect
        self.assertIn('authorize', inspect.signature(self.backend.dispatch).parameters,
            'expiry must be checked after acquiring the authority mutex')
        seen = []
        def expired():
            seen.append(self.authority._lock('paper-test').locked())
            raise self.c.ControlRejected('EXPIRED')
        with self.assertRaisesRegex(self.c.ControlRejected, 'EXPIRED'):
            self.backend.dispatch(self.request(), role='operator', authorize=expired)
        self.assertEqual([True], seen)
        self.assertEqual(0, self.ledger.connection.execute('SELECT hard_killed FROM account_control').fetchone()[0])

    def test_real_ledger_kill_commits_under_mutex_and_blocks_dispatch(self):
        import sqlite3
        from bot.autonomous.contracts import ContractViolation
        prepared = self.fixture.reserve(self.proposal)
        original = self.ledger.latch_operator_kill
        def observed(account):
            self.assertTrue(self.authority._lock(account).locked())
            original(account)
            with sqlite3.connect(self.fixture.path) as observer:
                self.assertEqual(1, observer.execute('SELECT hard_killed FROM account_control').fetchone()[0])
        self.ledger.latch_operator_kill = observed
        result = self.backend.dispatch(self.request(), role='operator', authorize=lambda: None)
        self.assertTrue(result['local_inhibited'])
        self.assertEqual('completed', result['status'])
        self.assertEqual('rejected', self.ledger.connection.execute('SELECT state FROM order_intents').fetchone()[0])
        with self.assertRaises(ContractViolation):
            self.ledger.mark_dispatching(prepared.intent_id, prepared.generation)
        from tests.autonomous.test_ledger import RiskAuthorityTests
        fresh = RiskAuthorityTests.next_decision(self.fixture, self.proposal, 1)
        with self.assertRaisesRegex(ContractViolation, 'disabled/stale control'):
            self.fixture.reserve(fresh)

    def test_inflight_intent_stays_visible_and_cancellation_is_pending(self):
        prepared = self.fixture.reserve(self.proposal)
        self.ledger.mark_dispatching(prepared.intent_id, prepared.generation)
        result = self.backend.dispatch(self.request(), role='operator', authorize=lambda: None)
        self.assertTrue(result['local_inhibited'])
        self.assertEqual('pending', result['status'])
        self.assertEqual([prepared.intent_id], result['cancellations_pending'])
        self.assertEqual('dispatching', self.ledger.connection.execute('SELECT state FROM order_intents').fetchone()[0])
        self.assertGreater(self.ledger.connection.execute('SELECT cash_usd_u FROM reservations').fetchone()[0], 0)

    def test_status_and_unavailable_policies_use_real_account(self):
        status = self.backend.dispatch(self.request('status'), role='reader', authorize=lambda: None)
        self.assertFalse(status['data']['hard_killed'])
        self.assertIn('lease_deadline_mono_ns', status['data'])
        with self.assertRaisesRegex(self.c.ControlRejected, 'HANDLER_UNAVAILABLE'):
            self.backend.dispatch(self.request('arm'), role='operator', authorize=lambda: None)
