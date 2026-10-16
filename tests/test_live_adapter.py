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

    def test_order_requires_risk_approval_and_uses_sized_amount(self):
        exchange = MockExchange()
        account = LiveCoinbaseAccount(enabled_config(), user_armed=True, client=exchange)
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


if __name__ == "__main__":
    unittest.main()
