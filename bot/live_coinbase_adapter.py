"""Opt-in CCXT exchange adapter. It never creates orders unless explicitly armed.

The adapter is intentionally separate from the paper account and is not wired into
the dashboard. Callers must provide a risk callback for every order submission.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping


class LiveTradingDisabled(RuntimeError):
    """Raised when live trading is not explicitly enabled and armed."""


class LiveTradingRejected(RuntimeError):
    """Raised when an order fails validation or its risk check."""


@dataclass(frozen=True)
class LiveConfig:
    exchange_id: str
    enabled: bool
    armed: bool
    api_key: str
    api_secret: str
    max_order_quote: float

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "LiveConfig":
        env = os.environ if environ is None else environ
        exchange_id = env.get("SURGE_LIVE_EXCHANGE", "").strip().lower()
        if exchange_id not in {"binance", "coinbaseadvanced"}:
            raise LiveTradingDisabled("SURGE_LIVE_EXCHANGE must be binance or coinbaseadvanced")
        prefix = "SURGE_BINANCE" if exchange_id == "binance" else "SURGE_COINBASE"
        max_order = float(env.get("SURGE_LIVE_MAX_ORDER_QUOTE", "0"))
        return cls(
            exchange_id=exchange_id,
            enabled=env.get("SURGE_LIVE_TRADING_ENABLED", "").lower() == "true",
            armed=env.get("SURGE_LIVE_TRADING_ARMED", "") == "I_ACCEPT_LIVE_TRADING_RISK",
            api_key=env.get(f"{prefix}_API_KEY", ""),
            api_secret=env.get(f"{prefix}_API_SECRET", ""),
            max_order_quote=max_order,
        )


def _ccxt_client(config: LiveConfig) -> Any:
    """Create a CCXT client lazily; the caller must pass all safety checks first."""
    try:
        import ccxt  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("Live exchange support requires the optional 'ccxt' package") from exc
    exchange_type = getattr(ccxt, config.exchange_id, None)
    if exchange_type is None:
        raise RuntimeError(f"Installed ccxt does not support {config.exchange_id}")
    return exchange_type({
        "apiKey": config.api_key,
        "secret": config.api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })


class LiveCoinbaseAccount:
    """Thin account/order interface for Binance and Coinbase Advanced via CCXT.

    Construction is inert unless both explicit config and the user safety latch
    are active. No market data is fetched implicitly. Order sizing takes a
    quote-currency budget and a caller-supplied current reference price.
    """

    def __init__(
        self,
        config: LiveConfig | None = None,
        *,
        user_armed: bool = False,
        client: Any | None = None,
        client_factory: Callable[[LiveConfig], Any] = _ccxt_client,
    ) -> None:
        self.config = config or LiveConfig.from_env()
        if not self.config.enabled or not self.config.armed or not user_armed:
            raise LiveTradingDisabled(
                "Live trading requires SURGE_LIVE_TRADING_ENABLED=true, the exact armed latch, "
                "and user_armed=True"
            )
        if not self.config.api_key or not self.config.api_secret:
            raise LiveTradingDisabled("Exchange API key and secret are required")
        if self.config.max_order_quote <= 0:
            raise LiveTradingDisabled("SURGE_LIVE_MAX_ORDER_QUOTE must be positive")
        if self.config.exchange_id not in {"binance", "coinbaseadvanced"}:
            raise LiveTradingDisabled("Unsupported live exchange")
        self.client = client if client is not None else client_factory(self.config)

    def get_balances(self) -> dict[str, Any]:
        return self.client.fetch_balance()

    def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return self.client.fetch_open_orders(symbol)

    def get_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        return self.client.fetch_order(order_id, symbol)

    def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        return self.client.cancel_order(order_id, symbol)

    def size_order(self, quote_amount: float, reference_price: float, symbol: str) -> float:
        """Convert quote budget into base quantity, applying exchange precision."""
        if quote_amount <= 0 or reference_price <= 0:
            raise ValueError("quote_amount and reference_price must be positive")
        if quote_amount > self.config.max_order_quote:
            raise LiveTradingRejected("Order quote amount exceeds configured maximum")
        raw_amount = quote_amount / reference_price
        precision_amount = float(self.client.amount_to_precision(symbol, raw_amount))
        if precision_amount <= 0:
            raise LiveTradingRejected("Order size rounds to zero at exchange precision")
        return precision_amount

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quote_amount: float,
        reference_price: float,
        *,
        risk_check: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Submit a market order only after caller risk approval.

        ``risk_check`` receives symbol, side, base quantity, reference price, and
        quote notional. It must return True or a tuple beginning with True.
        """
        normalized_side = side.lower()
        if normalized_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if not symbol or "/" not in symbol:
            raise ValueError("symbol must be a CCXT spot symbol such as BTC/USDT")
        if risk_check is None:
            raise LiveTradingRejected("A caller-provided risk_check is required for live orders")
        amount = self.size_order(quote_amount, reference_price, symbol)
        context = {
            "symbol": symbol,
            "side": normalized_side,
            "amount": amount,
            "reference_price": float(reference_price),
            "order_value": amount * float(reference_price),
            "mode": "live",
        }
        decision = risk_check(context)
        allowed = decision is True or (isinstance(decision, tuple) and bool(decision) and decision[0] is True)
        if not allowed:
            raise LiveTradingRejected(f"Risk check rejected order: {decision!r}")
        return self.client.create_order(symbol, "market", normalized_side, amount)
