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
from dashboard.backend.authority_client import (AuthorityGateway, BrowserSession, COOKIE_NAME,
    DurableNonceStore, NonceStoreUnsafe, bootstrap_token_hash)

RELEASE = 'e' * 64


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
        self.previous_hosts = getattr(web.app.state, 'allowed_hosts', None)
        web.app.state.allowed_hosts = frozenset({'testserver'})
        self.addCleanup(setattr, web.app.state, 'allowed_hosts', self.previous_hosts)
        self.store_path = self.path / 'web-state' / 'nonces.json'
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

    def install(self, *, client=None, browser=None, bootstrap=None):
        self.gateway = AuthorityGateway(client=client or self.transport,
            sessions=[browser or self.browser], nonce_store=DurableNonceStore(self.store_path),
            bootstrap_tokens=bootstrap, monotonic_ns=lambda: 10)
        web.app.state.authority_gateway = self.gateway
        self.client.cookies.set(COOKIE_NAME, (browser or self.browser).session_id)

    def mutation_cases(self):
        return [('/api/orders', {'symbol':'BTC-USD','side':'buy','usd_amount':100}),
            ('/api/orders', {'symbol':'BTC-USD','side':'sell','usd_amount':100}),
            ('/api/control/arm', {'release_hash': RELEASE}), ('/api/control/kill', None),
            ('/api/control/disarm', None), ('/api/control/flatten', None),
            ('/api/live/arm', {'acknowledgement': web.LIVE_ACKNOWLEDGEMENT, 'release_hash': RELEASE}),
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

    UNSUPPORTED = ('/api/orders', '/api/live/orders', '/api/automation', '/api/strategy/run?execute=true')

    def test_unsupported_mutations_are_typed_unavailable_and_never_write(self):
        self.install()
        with (patch.object(web, 'db', side_effect=AssertionError('local account DB used')),
                patch.object(web, 'live_config', side_effect=AssertionError('venue config read'))):
            for url, body in self.mutation_cases():
                if url not in self.UNSUPPORTED:
                    continue
                with self.subTest(url=url, body=body):
                    result = self.client.post(url, json=body, headers=self.headers)
                    self.assertEqual(409, result.status_code, result.text)
                    self.assertEqual('unavailable', result.json()['receipt'])
                    self.assertFalse(result.json()['local_inhibited'])
        self.assertFalse(self.calls)

    def test_arm_disarm_flatten_and_live_routes_forward_to_authority(self):
        live_control = dataclasses.replace(self.control_session, mode='live')
        self.install(browser=dataclasses.replace(self.browser, control=live_control))
        cases = (('/api/control/arm', {'release_hash': RELEASE}, 'arm', {'reason': 'operator arm', 'release_hash': RELEASE}),
            ('/api/control/disarm', None, 'disarm', {'reason': 'operator disarm'}),
            ('/api/control/flatten', {'incident_id': 'inc-1'}, 'reduce', {'reason': 'operator flatten', 'incident_id': 'inc-1'}),
            ('/api/live/arm', {'acknowledgement': web.LIVE_ACKNOWLEDGEMENT, 'release_hash': RELEASE}, 'arm',
                {'reason': 'operator live arm', 'release_hash': RELEASE}),
            ('/api/live/disarm', None, 'disarm', {'reason': 'operator disarm'}))
        with patch.object(web, 'db', side_effect=AssertionError('local mutation')):
            for i, (url, body, kind, payload) in enumerate(cases):
                with self.subTest(url=url):
                    result = self.client.post(url, json=body, headers={**self.headers, 'X-Request-ID': f'r{i}'})
                    self.assertEqual(200, result.status_code, result.text)
                    self.assertEqual('completed', result.json()['receipt'])
                    self.assertEqual((kind, payload, 'live'), (self.calls[-1]['kind'], self.calls[-1]['payload'],
                        self.calls[-1]['mode']))
        self.assertNotIn('server-only-token', result.text)

    def test_live_routes_need_a_live_session_and_exact_acknowledgement(self):
        self.install()
        refused = self.client.post('/api/live/arm', json={'acknowledgement': web.LIVE_ACKNOWLEDGEMENT,
            'release_hash': RELEASE}, headers=self.headers)
        self.assertEqual(409, refused.status_code)
        self.assertEqual(['MODE_UNAVAILABLE'], refused.json()['reason_codes'])
        wrong = self.client.post('/api/live/arm', json={'acknowledgement': 'yes', 'release_hash': RELEASE},
            headers=self.headers)
        self.assertEqual(422, wrong.status_code)
        self.assertEqual(422, self.client.post('/api/control/arm', json={'release_hash': 'nothex'},
            headers=self.headers).status_code)
        self.assertFalse(self.calls)

    def test_handler_unavailable_and_hard_kill_rejections_are_returned_as_is(self):
        owner = self
        def rejecting(reason):
            class Client:
                def request(self, raw):
                    reply = owner.transport.request(raw)
                    reply.update(status='rejected', local_inhibited=False, reason_codes=[reason], data={})
                    return reply
            return Client()
        self.install(client=rejecting('HANDLER_UNAVAILABLE'))
        result = self.client.post('/api/control/arm', json={'release_hash': RELEASE}, headers=self.headers)
        self.assertEqual(409, result.status_code)
        self.assertEqual('unavailable', result.json()['receipt'])
        self.install(client=rejecting('COMMAND_FORBIDDEN'))
        flatten = self.client.post('/api/control/flatten', headers={**self.headers, 'X-Request-ID': 'flat-1'})
        self.assertEqual(409, flatten.status_code)
        self.assertEqual('rejected', flatten.json()['receipt'])
        self.assertEqual(['COMMAND_FORBIDDEN'], flatten.json()['reason_codes'])

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


class DurableNonceAndSessionTests(DashboardAuthorityTests):
    def test_used_nonces_survive_a_web_restart_and_retries_rebuild_the_same_envelope(self):
        self.install()
        self.assertEqual(200, self.client.post('/api/control/kill', headers=self.headers).status_code)
        first = self.calls[-1]
        mode = os.stat(self.store_path).st_mode & 0o777
        self.assertEqual(0o600, mode)
        self.assertNotIn('server-only-token', self.store_path.read_text())
        self.install()  # simulated restart: a fresh gateway over the same store
        self.client.post('/api/control/kill', headers=self.headers)
        self.assertEqual(first, self.calls[-1])  # same request id -> identical envelope
        self.client.post('/api/control/kill', headers={**self.headers, 'X-Request-ID': 'kill-2'})
        self.assertNotEqual(first['nonce'], self.calls[-1]['nonce'])

    def test_unsafe_store_permissions_are_refused(self):
        self.install()
        self.client.post('/api/control/kill', headers=self.headers)
        os.chmod(self.store_path, 0o644)
        with self.assertRaises(NonceStoreUnsafe):
            DurableNonceStore(self.store_path)

    def test_gateway_requires_a_durable_store(self):
        with self.assertRaises(ValueError):
            AuthorityGateway(client=self.transport, sessions=[self.browser], nonce_store=None)

    def test_bootstrap_token_is_single_use_and_origin_bound(self):
        token = 'T' * 40
        self.install(bootstrap={bootstrap_token_hash(token): self.browser.session_id})
        self.client.cookies.clear()
        bad = self.client.post('/api/session/bootstrap', json={'token': token}, headers={'Origin': 'http://evil.test'})
        self.assertEqual(403, bad.status_code)
        good = self.client.post('/api/session/bootstrap', json={'token': token}, headers={'Origin': 'http://testserver'})
        self.assertEqual(200, good.status_code, good.text)
        cookie = good.headers['set-cookie'].lower()
        self.assertIn('httponly', cookie)
        self.assertIn('samesite=strict', cookie)
        self.assertEqual('b' * 32, good.json()['csrf_token'])
        again = self.client.post('/api/session/bootstrap', json={'token': token}, headers={'Origin': 'http://testserver'})
        self.assertEqual(401, again.status_code)
        self.assertEqual(401, self.client.post('/api/session/bootstrap', json={'token': 'x' * 40},
            headers={'Origin': 'http://testserver'}).status_code)


class LocalPostGuardTests(DashboardAuthorityTests):
    def test_non_managed_posts_need_same_origin_host_and_csrf(self):
        token = self.client.get('/api/csrf').json()['csrf_token']
        url = '/api/strategy/run'
        self.assertEqual(403, self.client.post(url).status_code)
        self.assertEqual(403, self.client.post(url, headers={'Origin': 'http://testserver'}).status_code)
        self.assertEqual(403, self.client.post(url, headers={'Origin': 'http://evil.test',
            'X-CSRF-Token': token}).status_code)
        ok = self.client.post(url, headers={'Origin': 'http://testserver', 'X-CSRF-Token': token})
        self.assertEqual(409, ok.status_code, ok.text)  # passed the guard; no candles stored
        rebinding = self.client.post(url, headers={'Origin': 'http://evil.test', 'Host': 'evil.test',
            'X-CSRF-Token': token})
        self.assertEqual(403, rebinding.status_code)
        self.assertEqual(403, self.client.get('/api/csrf', headers={'Host': 'evil.test'}).status_code)

    def test_every_post_route_is_guarded(self):
        guarded = web._post_paths() - web._SELF_CHECKED_POSTS
        for path in sorted(guarded):
            with self.subTest(path=path):
                self.assertIn(self.client.post(path).status_code, {401, 403})

    def test_get_only_routes_still_return_405(self):
        self.assertEqual(405, self.client.post('/api/autonomous/runtime').status_code)


class ReadOnlyDiagnosticsTests(unittest.TestCase):
    def test_every_diagnostic_sqlite_reader_opens_read_only(self):
        import ast
        source = Path(web.__file__).read_text()
        tree = ast.parse(source)
        calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for inner in ast.walk(node):
                    if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                            and inner.func.attr == 'connect'
                            and isinstance(inner.func.value, ast.Name) and inner.func.value.id == 'sqlite3'):
                        calls.append((node.name, ast.get_source_segment(source, inner)))
        self.assertTrue(calls)
        for function, call in calls:
            with self.subTest(function=function):
                if function == 'db':  # the display database owned by this web process
                    continue
                self.assertIn('mode=ro', call)
                self.assertIn('uri=True', call)


class EpochResyncTests(DashboardAuthorityTests):
    """STALE_EPOCH carries data.current_epoch: status retries once; mutations never auto-retry."""

    def stale_client(self, epoch=5):
        owner = self
        class Client:
            def request(self, raw):
                value = json.loads(raw)
                owner.calls.append(value)
                if value["expected_epoch"] != epoch:
                    return dict(version=1, request_id=value["request_id"], status="rejected", control_epoch=epoch,
                                reason_codes=["STALE_EPOCH"], local_inhibited=False, cancellations_pending=[],
                                data={"current_epoch": epoch})
                return dict(version=1, request_id=value["request_id"], status="completed", control_epoch=epoch,
                            reason_codes=[], local_inhibited=value["kind"] == "kill", cancellations_pending=[],
                            data={})
        return Client()

    def test_status_adopts_the_current_epoch_and_retries_once(self):
        self.install(client=self.stale_client())
        result = self.client.get("/api/control/status", headers={"X-Control-Epoch": "1"})
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual([1, 5], [c["expected_epoch"] for c in self.calls])
        self.assertEqual(5, self.gateway.control_epoch)

    def test_mutations_return_a_stale_epoch_receipt_without_retrying(self):
        self.install(client=self.stale_client())
        for url, body in (("/api/control/kill", None), ("/api/control/arm", {"release_hash": RELEASE}),
                          ("/api/control/flatten", None)):
            with self.subTest(url=url):
                before = len(self.calls)
                result = self.client.post(url, json=body, headers={**self.headers, "X-Request-ID": url.split("/")[-1]})
                self.assertEqual(409, result.status_code, result.text)
                self.assertEqual("stale_epoch", result.json()["receipt"])
                self.assertEqual(5, result.json()["current_epoch"])
                self.assertEqual(1, len(self.calls) - before)
        self.assertEqual(5, self.gateway.control_epoch)

    def test_runtime_file_switches_are_covered_by_origin_and_csrf(self):
        runtime = self.path / "rt"
        with patch.dict(os.environ, {"AUTONOMOUS_RUNTIME_DIR": str(runtime)}):
            for headers in ({}, {"Origin": "http://evil.test", "X-CSRF-Token": web.local_csrf_token(web.app)},
                            {"Origin": "http://testserver", "X-CSRF-Token": "wrong"}):
                with self.subTest(headers=headers):
                    reply = self.client.post("/api/autonomous/runtime/command", json={"action": "kill"}, headers=headers)
                    self.assertEqual(403, reply.status_code, reply.text)
                    self.assertFalse((runtime / "KILL").exists())
            token = self.client.get("/api/csrf").json()["csrf_token"]
            ok = self.client.post("/api/autonomous/runtime/command", json={"action": "stop"},
                                  headers={"Origin": "http://testserver", "X-CSRF-Token": token})
            self.assertEqual(200, ok.status_code, ok.text)
            self.assertTrue((runtime / "STOP").exists())
