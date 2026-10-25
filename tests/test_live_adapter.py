import unittest

from bot.live_coinbase_adapter import (
    LiveCoinbaseAccount,
    LiveConfig,
    LiveTradingDisabled,
    LiveTradingRejected,
)


class MockExchange:
    def __init__(self):
        self.created = []

    def amount_to_precision(self, symbol, amount):
        return f"{amount:.5f}"

    def create_order(self, *args):
        self.created.append(args)
        return {"id": "mock-1", "status": "open", "args": args}

    def fetch_balance(self):
        return {"USDT": {"free": 100}}

    def fetch_open_orders(self, symbol=None):
        return []

    def fetch_order(self, order_id, symbol):
        return {"id": order_id, "symbol": symbol}

    def cancel_order(self, order_id, symbol):
        return {"id": order_id, "status": "canceled"}


def enabled_config(**changes):
    values = {
        "exchange_id": "binance",
        "enabled": True,
        "armed": True,
        "api_key": "test-key",
        "api_secret": "test-secret",
        "max_order_quote": 500,
    }
    values.update(changes)
    return LiveConfig(**values)


class LiveAdapterTests(unittest.TestCase):
    def test_disabled_by_default_and_requires_user_arming(self):
        env = {
            "SURGE_LIVE_EXCHANGE": "binance",
            "SURGE_BINANCE_API_KEY": "k",
            "SURGE_BINANCE_API_SECRET": "s",
            "SURGE_LIVE_MAX_ORDER_QUOTE": "100",
        }
        config = LiveConfig.from_env(env)
        self.assertFalse(config.enabled)
        self.assertFalse(config.armed)
        with self.assertRaises(LiveTradingDisabled):
            LiveCoinbaseAccount(config, user_armed=True, client_factory=lambda _: self.fail("factory called"))
        with self.assertRaises(LiveTradingDisabled):
            LiveCoinbaseAccount(enabled_config(), user_armed=False, client_factory=lambda _: self.fail("factory called"))

    def test_env_selects_supported_exchange_credentials(self):
        config = LiveConfig.from_env({
            "SURGE_LIVE_EXCHANGE": "coinbaseadvanced",
            "SURGE_LIVE_TRADING_ENABLED": "true",
            "SURGE_LIVE_TRADING_ARMED": "I_ACCEPT_LIVE_TRADING_RISK",
            "SURGE_COINBASE_API_KEY": "cb-key",
            "SURGE_COINBASE_API_SECRET": "cb-secret",
            "SURGE_LIVE_MAX_ORDER_QUOTE": "250",
        })
        self.assertEqual(config.api_key, "cb-key")
        self.assertEqual(config.api_secret, "cb-secret")
        self.assertTrue(config.enabled and config.armed)

    def test_legacy_market_order_refuses_without_explicit_opt_in(self):
        exchange = MockExchange()
        account = LiveCoinbaseAccount(enabled_config(), user_armed=True, client=exchange)
        with self.assertRaises(LiveTradingRejected):
            account.place_market_order("BTC/USDT", "buy", 100, 20_000,
                risk_check=lambda _: self.fail("risk callback reached without opt-in"))
        self.assertEqual([], exchange.created)

    def test_legacy_opt_in_and_managed_are_exclusive_and_managed_is_read_only(self):
        with self.assertRaises(LiveTradingDisabled):
            LiveCoinbaseAccount(enabled_config(), user_armed=True, client=MockExchange(),
                managed=True, legacy_opt_in=True)
        account = LiveCoinbaseAccount(enabled_config(), user_armed=True, client=MockExchange(), managed=True)
        with self.assertRaises(AttributeError):
            account.managed = False
        with self.assertRaises(AttributeError):
            account.legacy_opt_in = True

    def test_order_requires_risk_approval_and_uses_sized_amount(self):
        exchange = MockExchange()
        account = LiveCoinbaseAccount(enabled_config(), user_armed=True, client=exchange, legacy_opt_in=True)
        with self.assertRaises(LiveTradingRejected):
            account.place_market_order("BTC/USDT", "buy", 100, 20_000)
        with self.assertRaises(LiveTradingRejected):
            account.place_market_order("BTC/USDT", "buy", 501, 20_000, risk_check=lambda _: True)
        order = account.place_market_order(
            "BTC/USDT", "BUY", 100, 20_000,
            risk_check=lambda context: context["order_value"] <= 100,
        )
        self.assertEqual(order["id"], "mock-1")
        self.assertEqual(exchange.created, [("BTC/USDT", "market", "buy", 0.005)])

    def test_account_and_order_operations_use_injected_client(self):
        exchange = MockExchange()
        account = LiveCoinbaseAccount(enabled_config(), user_armed=True, client=exchange)
        self.assertIn("USDT", account.get_balances())
        self.assertEqual(account.get_open_orders("BTC/USDT"), [])
        self.assertEqual(account.get_order("o1", "BTC/USDT")["id"], "o1")
        self.assertEqual(account.cancel_order("o1", "BTC/USDT")["status"], "canceled")


class ManagedTransportTests(unittest.TestCase):
    def setUp(self):
        import dataclasses
        from decimal import Decimal as D
        from bot.autonomous.contracts import (AccountRevision, IntentState, MarketRules,
            OrderProposal, PreparedIntent, Reservation, Side)
        from bot import live_coinbase_adapter as adapter
        self.assertTrue(hasattr(adapter, "ManagedBoundedTransport"), "managed bounded transport is missing")
        self.adapter, self.replace, self.D = adapter, dataclasses.replace, D
        self.market = MarketRules('coinbaseadvanced', 'BTC-USD', 'BTC', 'USD', D('.01'),
            D('.00001'), D('.00001'), D('1'), D('.001'), 2, 'a' * 64, 100,
            spot_allowed=True, limit_ioc_verified=True)
        proposal = OrderProposal('action-1', 'managed-1', 'candidate-1', 'BTC-USD',
            Side.SELL, D('.01001'), D('100.01'), 1002, 'b' * 64, None,
            1, 'reduction-1', True, 0, 100)
        reservation = Reservation('intent-1', 0, proposal.base_quantity, 0, 0,
            proposal.base_quantity, IntentState.RESERVED, instrument='BTC-USD', side='sell')
        self.intent = PreparedIntent('intent-1', proposal, reservation, 'stable-action-1',
            0, AccountRevision(), IntentState.DISPATCHING)
        self.exchange = MockExchange()

    def verifier(self, **changes):
        adapter = self.adapter
        from bot.autonomous.contracts import content_hash
        class FakeVerifier(adapter.VenueCapabilityVerifier):
            def verify(issuer, intent, market, now_us, *, client):
                values = dict(issuer=issuer, client=client, intent_hash=content_hash(intent),
                    market_hash=content_hash(market), verified_at_us=1, expires_at_us=100,
                    source_record_hashes=('c' * 64,),
                    capabilities=adapter.MANAGED_CAPABILITIES, client_id_max_length=64)
                values.update(changes)
                return adapter.VerifiedVenueCapabilities(**values)
        return FakeVerifier()

    def transport(self, **changes):
        values = dict(client=self.exchange, account_id='managed-1',
            capability_verifier=self.verifier(), clock_us=lambda: 10, monotonic_ns=lambda: 10)
        values.update(changes)
        return self.adapter.ManagedBoundedTransport(**values)

    def test_construction_is_inert_and_no_verifier_cannot_submit(self):
        transport = self.transport(capability_verifier=None)
        self.assertEqual([], self.exchange.created)
        with self.assertRaises(LiveTradingRejected):
            transport.submit_prepared(self.intent, self.market)
        self.assertEqual([], self.exchange.created)

    def test_explicit_ioc_limit_client_id_and_exact_decimal_strings(self):
        result = self.transport().submit_prepared(self.intent, self.market)
        self.assertEqual('mock-1', result['id'])
        self.assertEqual([('BTC/USD', 'limit', 'sell', '0.01001', '100.01',
            {'timeInForce': 'IOC', 'clientOrderId': 'stable-action-1'})], self.exchange.created)

    def test_submit_failure_propagates_once_without_retry(self):
        failure = TimeoutError('acceptance unknown')
        def create(*args):
            self.exchange.created.append(args)
            raise failure
        self.exchange.create_order = create
        with self.assertRaises(TimeoutError) as caught:
            self.transport().submit_prepared(self.intent, self.market)
        self.assertIs(failure, caught.exception)
        self.assertEqual(1, len(self.exchange.created))

    def test_unverified_stale_wrong_issuer_or_unbound_capabilities_reject(self):
        cases = [dict(capabilities=frozenset()), dict(capabilities=True),
            dict(source_record_hashes=()), dict(source_record_hashes=('bad',)),
            dict(market_hash='d' * 64), dict(intent_hash='e' * 64),
            dict(issuer=object()), dict(expires_at_us=10), dict(verified_at_us=11),
            dict(client_id_max_length=3)]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(LiveTradingRejected):
                    self.transport(capability_verifier=self.verifier(**changes)).submit_prepared(
                        self.intent, self.market)
        with self.assertRaises(LiveTradingRejected):
            self.transport(capability_verifier=True).submit_prepared(self.intent, self.market)
        self.assertEqual([], self.exchange.created)

    def test_invalid_market_rules_reject_before_venue(self):
        D = self.D
        cases = [dict(venue='binance'), dict(instrument='ETH-USD'), dict(base_asset='ETH'),
            dict(spot_allowed=False), dict(limit_ioc_verified=False), dict(expires_at_us=10),
            dict(tick_size=D('.1')), dict(lot_size=D('.001')),
            dict(minimum_quantity=D('1')), dict(minimum_notional=D('2')),
            dict(fee_bound=D('.1')), dict(fee_bound=D('0')), dict(max_price_precision=1)]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(LiveTradingRejected):
                    self.transport().submit_prepared(self.intent, self.replace(self.market, **changes))
        self.assertEqual([], self.exchange.created)

    def test_wrong_account_state_deadline_and_reservation_reject(self):
        from bot.autonomous.contracts import IntentState
        for intent in (self.replace(self.intent, state=IntentState.RESERVED),
                self.replace(self.intent, client_order_id='bad id'),
                self.replace(self.intent, proposal=self.replace(self.intent.proposal, account_id='foreign')),
                self.replace(self.intent, proposal=self.replace(self.intent.proposal, deadline_mono_ns=10)),
                self.replace(self.intent, reservation=self.replace(self.intent.reservation,
                    remaining_quantity=self.D('.001')))):
            with self.subTest(intent=intent):
                with self.assertRaises(LiveTradingRejected):
                    self.transport().submit_prepared(intent, self.market)
        self.assertEqual([], self.exchange.created)

    def test_exact_increment_and_fee_checks_ignore_ambient_decimal_precision(self):
        from decimal import localcontext
        with localcontext() as context:
            context.prec = 2
            self.transport().submit_prepared(self.intent, self.market)
        self.assertEqual('0.01001', self.exchange.created[0][3])

    def test_client_owned_by_managed_transport_is_unreachable_from_legacy(self):
        self.transport()
        account = LiveCoinbaseAccount(enabled_config(), user_armed=True,
            client=self.exchange, legacy_opt_in=True)
        with self.assertRaises(LiveTradingRejected):
            account.place_market_order('BTC/USD', 'buy', 10, 100,
                risk_check=lambda _: self.fail('managed client reached legacy risk callback'))
        self.assertEqual([], self.exchange.created)

    def test_managed_legacy_market_primitive_rejects_before_risk_or_create(self):
        account = LiveCoinbaseAccount(enabled_config(), user_armed=True,
            client=self.exchange, managed=True)
        with self.assertRaises(LiveTradingRejected):
            account.place_market_order('BTC-USD', 'buy', 10, 100,
                risk_check=lambda _: self.fail('managed legacy called risk callback'))
        self.assertEqual([], self.exchange.created)

    def test_capability_evidence_binds_the_exact_injected_client(self):
        import dataclasses
        self.assertIn('client', {field.name for field in dataclasses.fields(
            self.adapter.VerifiedVenueCapabilities)}, 'capability proof must bind the client')
        with self.assertRaises(LiveTradingRejected):
            self.transport(capability_verifier=self.verifier(client=object())).submit_prepared(
                self.intent, self.market)
        self.assertEqual([], self.exchange.created)

    def test_buy_is_explicit_and_fee_ceiling_cannot_be_rounded_down(self):
        from bot.autonomous.contracts import Side
        proposal = self.replace(self.intent.proposal, side=Side.BUY)
        intent = self.replace(self.intent, proposal=proposal,
            reservation=self.replace(self.intent.reservation, side='buy'))
        self.transport().submit_prepared(intent, self.market)
        self.assertEqual('buy', self.exchange.created[0][2])
        self.exchange.created.clear()
        insufficient = self.replace(intent, proposal=self.replace(proposal, max_fee_usd_u=1001))
        with self.assertRaises(LiveTradingRejected):
            self.transport().submit_prepared(insufficient, self.market)
        self.assertEqual([], self.exchange.created)

    def test_expiry_during_verification_does_not_create(self):
        times = iter((10, 100))
        with self.assertRaises(LiveTradingRejected):
            self.transport(clock_us=lambda: next(times)).submit_prepared(self.intent, self.market)
        self.assertEqual([], self.exchange.created)

    def test_invalid_contract_numeric_and_side_values_never_reach_client(self):
        from bot.autonomous.contracts import ContractViolation
        for changes in ({'base_quantity': self.D('NaN')}, {'base_quantity': self.D('0')},
                {'base_quantity': 0.1}, {'limit_price': self.D('-1')}, {'side': 'buy'}):
            with self.subTest(changes=changes):
                with self.assertRaises(ContractViolation):
                    proposal = self.replace(self.intent.proposal, **changes)
                    self.transport().submit_prepared(self.replace(self.intent, proposal=proposal), self.market)
        self.assertEqual([], self.exchange.created)


if __name__ == '__main__':
    unittest.main()


class BoundedSessionTests(unittest.TestCase):
    """Response bodies are bounded while streaming, before they are fully buffered."""

    def response(self, body, *, length=None, chunked=False):
        import io
        import requests
        test = self
        class CountingRaw(io.BytesIO):
            def read(self, size=-1):
                data = super().read(size)
                test.read_bytes += len(data)
                return data
        self.read_bytes = 0
        response = requests.Response()
        response.status_code = 200
        response.raw = CountingRaw(body)
        response.headers['Content-Type'] = 'application/json'
        if length is not None:
            response.headers['Content-Length'] = str(length)
        self.closed = False
        original = response.close
        def close():
            self.closed = True
            original()
        response.close = close
        return response

    def inner(self, response):
        test = self
        class Inner:
            def __init__(self):
                self.cookies, self.headers = {}, {}
            def request(self, *args, **kwargs):
                test.kwargs = kwargs
                return response
        return Inner()

    def test_small_body_is_readable_and_streamed(self):
        from bot.live_coinbase_adapter import BoundedSession
        session = BoundedSession(self.inner(self.response(b'{"ok":true}')), max_bytes=1024)
        response = session.request('GET', 'https://example.invalid', timeout=1)
        self.assertTrue(self.kwargs['stream'])
        self.assertEqual('{"ok":true}', response.text)

    def test_oversized_body_stops_reading_near_the_bound(self):
        from bot.live_coinbase_adapter import BoundedSession, ResponseTooLarge
        session = BoundedSession(self.inner(self.response(b'x' * 5_000_000)), max_bytes=100_000, chunk_size=8192)
        with self.assertRaises(ResponseTooLarge):
            session.request('GET', 'https://example.invalid')
        self.assertLessEqual(self.read_bytes, 100_000 + 8192)
        self.assertTrue(self.closed)

    def test_declared_oversized_length_is_refused_before_reading(self):
        from bot.live_coinbase_adapter import BoundedSession, ResponseTooLarge
        session = BoundedSession(self.inner(self.response(b'x' * 10, length=10_000_000)), max_bytes=1000)
        with self.assertRaises(ResponseTooLarge):
            session.request('GET', 'https://example.invalid')
        self.assertEqual(0, self.read_bytes)

    def test_managed_transport_installs_bound_into_real_ccxt_fetch(self):
        import ccxt
        from bot.live_coinbase_adapter import BoundedSession, ManagedBoundedTransport, ResponseTooLarge
        client = ccxt.coinbase()  # no credentials; the HTTP session below is a fake
        client.session = self.inner(self.response(b'[' + b'1,' * 1_000_000 + b'1]'))
        transport = ManagedBoundedTransport(client=client, account_id='managed-1', capability_verifier=None,
            clock_us=lambda: 1, monotonic_ns=lambda: 1)
        self.assertIs(BoundedSession, type(client.session))
        self.assertIs(transport.client, client)
        with self.assertRaises(ResponseTooLarge):
            client.fetch('https://example.invalid/api')
        self.assertLessEqual(self.read_bytes, transport.max_response_bytes + 65_536)

    def test_ccxt_fetch_still_decodes_bounded_json(self):
        import ccxt
        from bot.live_coinbase_adapter import ManagedBoundedTransport
        client = ccxt.coinbase()
        client.session = self.inner(self.response(b'{"data":[1,2]}'))
        ManagedBoundedTransport(client=client, account_id='managed-1', capability_verifier=None,
            clock_us=lambda: 1, monotonic_ns=lambda: 1)
        self.assertEqual({'data': [1, 2]}, client.fetch('https://example.invalid/api'))


def test_okx_us_uses_okx_credentials_and_requires_passphrase():
    env = {"SURGE_LIVE_EXCHANGE": "okxus", "SURGE_OKX_API_KEY": "k", "SURGE_OKX_API_SECRET": "s",
           "SURGE_LIVE_MAX_ORDER_QUOTE": "10", "SURGE_LIVE_TRADING_ENABLED": "true",
           "SURGE_LIVE_TRADING_ARMED": "I_ACCEPT_LIVE_TRADING_RISK"}
    cfg = LiveConfig.from_env(env)
    assert (cfg.exchange_id, cfg.api_key, cfg.api_passphrase) == ("okxus", "k", "")
    try:
        LiveCoinbaseAccount(cfg, user_armed=True, client=object())
    except LiveTradingDisabled as exc:
        assert "passphrase" in str(exc)
    else:
        raise AssertionError("OKX US without a passphrase must stay disabled")


def test_okx_config_reads_passphrase():
    cfg = LiveConfig.from_env({
        "SURGE_LIVE_EXCHANGE": "okx", "SURGE_OKX_API_KEY": "k", "SURGE_OKX_API_SECRET": "s",
        "SURGE_OKX_API_PASSPHRASE": "p", "SURGE_LIVE_MAX_ORDER_QUOTE": "10"})
    assert (cfg.exchange_id, cfg.api_key, cfg.api_passphrase) == ("okx", "k", "p")


class VenueCheckTests(unittest.TestCase):
    def test_check_is_read_only_and_flags_risky_keys(self):
        from bot.venue_check import check

        class Client:
            hostname = "us.okx.com"

            def __init__(self):
                self.calls = []

            def privateGetAccountConfig(self):
                self.calls.append("config")
                return {"data": [{"perm": "read_only,withdraw,trade", "ip": "", "acctLv": "1"}]}

            def fetch_trading_fee(self, symbol):
                self.calls.append("fee")
                return {"maker": 0.002, "taker": 0.0035}

            def fetch_balance(self):
                self.calls.append("balance")
                return {"total": {"USDT": 0, "BTC": 0.5}}

            def __getattr__(self, name):   # any order or transfer method would land here
                raise AssertionError(f"venue check must not call {name}")

        client = Client()
        env = {"SURGE_LIVE_EXCHANGE": "okxus", "SURGE_OKX_API_KEY": "KEY-XYZ", "SURGE_OKX_API_SECRET": "SECRET-XYZ",
               "SURGE_OKX_API_PASSPHRASE": "PASS-XYZ", "SURGE_LIVE_TRADING_ENABLED": "true",
               "SURGE_LIVE_TRADING_ARMED": "I_ACCEPT_LIVE_TRADING_RISK"}
        seen = []
        lines = check(env, client_factory=lambda cfg: seen.append(cfg) or client)
        self.assertEqual(client.calls, ["config", "fee", "balance"])
        self.assertFalse(seen[0].enabled or seen[0].armed)          # live flags are forced off
        text = "\n".join(lines)
        self.assertIn("WARNING: this key can withdraw", text)
        self.assertIn("no IP allowlist", text)
        self.assertIn("maker 0.2000%, taker 0.3500%", text)
        self.assertIn("['BTC']", text)
        for secret in ("KEY-XYZ", "SECRET-XYZ", "PASS-XYZ"):
            self.assertNotIn(secret, text)

    def test_read_env_takes_only_surge_settings(self):
        import tempfile
        from pathlib import Path
        from bot.venue_check import read_env
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".env"
            path.write_text("SURGE_LIVE_EXCHANGE=okxus\nALPACA_SECRET_KEY=x\n# SURGE_OKX_API_KEY=commented\n")
            self.assertEqual(read_env(path, {"SURGE_OKX_API_KEY": "env"}),
                             {"SURGE_LIVE_EXCHANGE": "okxus", "SURGE_OKX_API_KEY": "env"})
