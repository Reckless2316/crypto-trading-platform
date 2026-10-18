"""Browser authentication, fail-closed forwarding, and real local kill integration."""
import dataclasses
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from bot.autonomous.control import (ControlSession, PeerCredentials, ControlService,
    FileControlJournal, LedgerControlBackend, UnixControlClient, UnixControlServer,
    ControlRejected, response)
from dashboard.backend import app as web
from dashboard.backend.authority_client import AuthorityGateway, BrowserSession, COOKIE_NAME


class DashboardAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.db_patch = patch.object(web, 'DB_FILE', self.path / 'display.sqlite3')
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        web.init_db()
        self.previous_gateway = getattr(web.app.state, 'authority_gateway', None)
        web.app.state.authority_gateway = None
        self.addCleanup(setattr, web.app.state, 'authority_gateway', self.previous_gateway)
        self.client = TestClient(web.app)
        self.addCleanup(self.client.close)
        self.calls = []
        owner = self
        class FakeClient:
            def request(self, raw):
                value = json.loads(raw)
                owner.calls.append(value)
                return dict(version=1, request_id=value['request_id'], status='completed',
                    control_epoch=value['expected_epoch'], reason_codes=[], local_inhibited=value['kind']=='kill',
                    cancellations_pending=[], data={'hard_killed': value['kind']=='kill'})
        self.transport = FakeClient()
        self.control_session = ControlSession('web-control', 'operator', 'operator', 'paper-test',
            'paper', 'boot', 'server-only-token', PeerCredentials(os.getpid(), os.getuid(), os.getgid()),
            10_000_000_000, {'nonce-'+str(i): 10_000_000_000 for i in range(20)})
        self.browser = BrowserSession('a'*32, 'operator', 'operator', 'http://testserver',
            'b'*32, 10_000_000_000, self.control_session)
        self.headers = {'Origin':'http://testserver', 'X-CSRF-Token':'b'*32,
            'X-Request-ID':'kill-1', 'X-Control-Epoch':'1'}

    def install(self, *, client=None, browser=None):
        self.gateway = AuthorityGateway(client=client or self.transport,
            sessions=[browser or self.browser], monotonic_ns=lambda: 10)
        web.app.state.authority_gateway = self.gateway
        self.client.cookies.set(COOKIE_NAME, (browser or self.browser).session_id)

    def mutation_cases(self):
        return [('/api/orders', {'symbol':'BTC-USD','side':'buy','usd_amount':100}),
            ('/api/orders', {'symbol':'BTC-USD','side':'sell','usd_amount':100}),
            ('/api/control/arm', {'duration_minutes':5}), ('/api/control/kill', None),
            ('/api/control/flatten', None), ('/api/live/arm', {'acknowledgement':'yes'}),
            ('/api/live/disarm', None), ('/api/live/orders', {'symbol':'BTC/USD','side':'buy','quote_amount':25}),
            ('/api/automation', {'enabled':True}), ('/api/strategy/run?execute=true', None)]

    def test_all_managed_mutations_reject_anonymous_before_local_work(self):
        with patch.object(web, 'db', side_effect=AssertionError('local account DB used')):
            for url, body in self.mutation_cases():
                with self.subTest(url=url, body=body):
                    result = self.client.post(url, json=body)
                    self.assertEqual(401, result.status_code, result.text)
        self.assertFalse(self.calls)

    def test_operator_origin_and_csrf_required_for_every_mutation(self):
        self.install()
        for url, body in self.mutation_cases():
            for changes in ({'Origin':'http://evil.test'}, {'X-CSRF-Token':'wrong'}):
                with self.subTest(url=url, changes=changes):
                    result = self.client.post(url, json=body, headers={**self.headers, **changes})
                    self.assertEqual(403, result.status_code, result.text)
        reader_control = dataclasses.replace(self.control_session, role='reader')
        reader = dataclasses.replace(self.browser, role='reader', control=reader_control)
        self.install(browser=reader)
        for url, body in self.mutation_cases():
            result = self.client.post(url, json=body, headers=self.headers)
            self.assertEqual(403, result.status_code)
        self.assertFalse(self.calls)

    def test_unsupported_mutations_never_write_or_construct_legacy_adapter(self):
        self.install()
        with (patch.object(web, 'db', side_effect=AssertionError('local account DB used')),
                patch.object(web, 'live_config', side_effect=AssertionError('venue config read'))):
            for url, body in self.mutation_cases():
                if url == '/api/control/kill':
                    continue
                with self.subTest(url=url):
                    result = self.client.post(url, json=body, headers=self.headers)
                    self.assertEqual(409, result.status_code, result.text)
        self.assertFalse(self.calls)

    def test_status_kill_and_cookie_flags_keep_authority_token_server_side(self):
        self.install()
        session = self.client.get('/api/session')
        cookie = session.headers['set-cookie'].lower()
        self.assertIn('httponly', cookie)
        self.assertIn('samesite=strict', cookie)
        self.assertNotIn('server-only-token', session.text)
        status = self.client.get('/api/control/status', headers={'X-Control-Epoch':'1'})
        self.assertEqual(200, status.status_code, status.text)
        with patch.object(web, 'db', side_effect=AssertionError('web ledger write')):
            killed = self.client.post('/api/control/kill', headers=self.headers)
        self.assertEqual(200, killed.status_code, killed.text)
        self.assertTrue(killed.json()['local_inhibited'])
        self.assertEqual(['status','kill'], [call['kind'] for call in self.calls])
        self.assertEqual('operator', self.calls[-1]['actor'])
        self.assertNotIn('server-only-token', killed.text)

    def test_pending_unknown_retries_identical_envelope_and_changed_epoch_rejects(self):
        owner = self
        class TimeoutClient:
            def request(self, raw):
                owner.calls.append(raw)
                raise TimeoutError('send may have succeeded')
        self.install(client=TimeoutClient())
        for _ in range(2):
            result = self.client.post('/api/control/kill', headers=self.headers)
            self.assertEqual(202, result.status_code)
            self.assertEqual('pending', result.json()['status'])
            self.assertFalse(result.json()['local_inhibited'])
        self.assertEqual(self.calls[0], self.calls[1])
        changed = self.client.post('/api/control/kill', headers={**self.headers,'X-Control-Epoch':'2'})
        self.assertEqual(409, changed.status_code)
        self.assertEqual(2,len(self.calls))

    def test_unavailable_and_rejected_responses_have_no_success_fallback(self):
        class MissingClient:
            def request(self, raw):
                raise FileNotFoundError('no authority socket')
        self.install(client=MissingClient())
        self.assertEqual(503,self.client.post('/api/control/kill',headers=self.headers).status_code)
        owner = self
        class RejectedClient:
            def request(self, raw):
                reply = owner.transport.request(raw)
                reply.update(status='rejected',local_inhibited=False,reason_codes=['STALE_EPOCH'])
                return reply
        self.install(client=RejectedClient())
        rejected = self.client.post('/api/control/kill',headers=self.headers)
        self.assertEqual(409,rejected.status_code)
        self.assertEqual(['STALE_EPOCH'],rejected.json()['reason_codes'])

    def test_no_gateway_or_wrong_response_cannot_open_default_ledger(self):
        self.client.cookies.set(COOKIE_NAME,'a'*32)
        with patch.object(web,'db',side_effect=AssertionError('fallback database')):
            self.assertEqual(503,self.client.post('/api/control/kill',headers=self.headers).status_code)
        owner = self
        class WrongClient:
            def request(self,raw):
                reply=owner.transport.request(raw)
                reply['request_id']='different'
                return reply
        self.install(client=WrongClient())
        result=self.client.post('/api/control/kill',headers=self.headers)
        self.assertEqual(202,result.status_code)
        self.assertFalse(result.json()['local_inhibited'])

    def test_legacy_automation_cannot_schedule_managed_orders_and_status_reads_no_keys(self):
        with patch.object(web,'db',side_effect=AssertionError('hidden scheduler')):
            self.assertIsNone(web.run_automation_for_new_candle())
            self.assertFalse(self.client.get('/api/automation').json()['enabled'])
        with patch.object(web,'live_config',side_effect=AssertionError('venue secrets')):
            self.assertFalse(self.client.get('/api/live/status').json()['configured'])

    def test_http_to_unix_service_commits_real_ledger_kill(self):
        from bot.autonomous.execution import OrderAuthority
        from tests.autonomous.test_ledger import LedgerTests
        ready = threading.Event()
        errors=[]
        results=[]
        socket_path=self.path/'socket'/'control.sock'
        capability=self.control_session
        def serve():
            fixture=LedgerTests()
            try:
                fixture.setUp()
                fixture.ready()
                ledger=fixture.ledger
                authority=OrderAuthority(ledger,None,book_source=lambda _:None,
                    market_source=lambda _:None,entry_verifier=lambda *a:False,clock_us=lambda:1)
                backend=LedgerControlBackend(ledger,authority,account_id='paper-test',mode='paper')
                service=ControlService(backend=backend,journal=FileControlJournal(self.path/'journal'),
                    sessions=[capability],boot_id='boot',monotonic_ns=lambda:10)
                with UnixControlServer(socket_path,service) as server:
                    ready.set()
                    server.serve_once()
                    results.append(ledger.connection.execute('SELECT hard_killed FROM account_control').fetchone()[0])
            except BaseException as exc:
                errors.append(exc)
                ready.set()
            finally:
                fixture.tearDown()
        thread=threading.Thread(target=serve)
        thread.start()
        try:
            self.assertTrue(ready.wait(3))
            if errors:
                raise errors[0]
            self.install(client=UnixControlClient(socket_path))
            result=self.client.post('/api/control/kill',headers=self.headers)
            self.assertEqual(200,result.status_code,result.text)
            self.assertTrue(result.json()['local_inhibited'])
        finally:
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors,errors)
        self.assertEqual([1],results)


class GatewaySafetyTests(DashboardAuthorityTests):
    """Acceptance-review regressions (2026-10-17)."""

    def test_status_polling_cannot_exhaust_the_kill_capability(self):
        few = dataclasses.replace(self.control_session, nonces={'n0': 10_000_000_000, 'n1': 10_000_000_000})
        self.install(browser=dataclasses.replace(self.browser, control=few))
        codes = [self.client.get('/api/control/status', headers={'X-Control-Epoch': '1'}).status_code
            for _ in range(3)]
        self.assertEqual([200, 503, 503], codes)
        killed = self.client.post('/api/control/kill', headers=self.headers)
        self.assertEqual(200, killed.status_code, killed.text)
        self.assertEqual(['status', 'kill'], [call['kind'] for call in self.calls])

    def test_post_send_client_protocol_failure_is_unknown_not_rejected(self):
        owner = self
        class TruncatedClient:
            def request(self, raw):
                owner.calls.append(raw)
                raise ControlRejected('TRUNCATED_FRAME')
        self.install(client=TruncatedClient())
        result = self.client.post('/api/control/kill', headers=self.headers)
        self.assertEqual(202, result.status_code, result.text)
        self.assertEqual('pending', result.json()['status'])
        self.assertIn('AUTHORITY_OUTCOME_UNKNOWN', result.json()['reason_codes'])
        self.assertFalse(result.json()['local_inhibited'])

    def test_wrong_server_peer_is_unavailable_before_send(self):
        class WrongPeer:
            def request(self, raw):
                raise ControlRejected('WRONG_SERVER_PEER')
        self.install(client=WrongPeer())
        self.assertEqual(503, self.client.post('/api/control/kill', headers=self.headers).status_code)

    def test_unexpected_client_error_is_unknown_not_server_error(self):
        class Broken:
            def request(self, raw):
                raise RuntimeError('client bug after send')
        self.install(client=Broken())
        result = self.client.post('/api/control/kill', headers=self.headers)
        self.assertEqual(202, result.status_code, result.text)
        self.assertFalse(result.json()['local_inhibited'])
