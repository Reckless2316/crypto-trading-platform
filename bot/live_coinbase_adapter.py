"""Legacy opt-in account and an inert, injected managed transport boundary.

The adapter is intentionally separate from the paper account and is not wired into
the dashboard. The legacy market primitive refuses unless the account was built
with explicit ``legacy_opt_in=True``; it is unreachable for managed accounts and for
any client owned by a ManagedBoundedTransport. Managed transport requires an
authority-prepared intent, independently verified capabilities, and bounds every
HTTP response body while streaming.

HANDOFF(claude-opus-5.5 2026-10-18): nothing in the repository constructs a
legacy account with opt-in; deleting the legacy primitive entirely should be
decided at Ship. The streaming bound wraps CCXT's synchronous requests session
only; an async CCXT client is not supported and must not be injected.
"""
from __future__ import annotations

import os
import re
import threading
import weakref
from abc import ABC, abstractmethod
from fractions import Fraction
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
    api_passphrase: str = ""

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "LiveConfig":
        env = os.environ if environ is None else environ
        exchange_id = env.get("SURGE_LIVE_EXCHANGE", "").strip().lower()
        if exchange_id not in {"binance", "coinbaseadvanced", "okx"}:
            raise LiveTradingDisabled("SURGE_LIVE_EXCHANGE must be binance, coinbaseadvanced or okx")
        prefix = {"binance": "SURGE_BINANCE", "okx": "SURGE_OKX"}.get(exchange_id, "SURGE_COINBASE")
        max_order = float(env.get("SURGE_LIVE_MAX_ORDER_QUOTE", "0"))
        return cls(
            exchange_id=exchange_id,
            enabled=env.get("SURGE_LIVE_TRADING_ENABLED", "").lower() == "true",
            armed=env.get("SURGE_LIVE_TRADING_ARMED", "") == "I_ACCEPT_LIVE_TRADING_RISK",
            api_key=env.get(f"{prefix}_API_KEY", ""),
            api_secret=env.get(f"{prefix}_API_SECRET", ""),
            max_order_quote=max_order,
            api_passphrase=env.get(f"{prefix}_API_PASSPHRASE", ""),
        )


# Clients exclusively owned by a ManagedBoundedTransport. A legacy account can
# never reach such a client, whatever flags it was constructed with.
_MANAGED_CLIENTS: "weakref.WeakSet[Any]" = weakref.WeakSet()
_MANAGED_CLIENT_IDS: set[int] = set()  # fallback for objects without weakref support


def _is_managed_client(client: Any) -> bool:
    try:
        return client in _MANAGED_CLIENTS
    except TypeError:
        return id(client) in _MANAGED_CLIENT_IDS


def _register_managed_client(client: Any) -> None:
    try:
        _MANAGED_CLIENTS.add(client)
    except TypeError:
        _MANAGED_CLIENT_IDS.add(id(client))


class ResponseTooLarge(ValueError):
    """A venue response exceeded the bound while streaming; its body was not buffered.

    Deliberately not a requests/CCXT network error, so no layer retries it.
    """


class BoundedSession:
    """Wraps an HTTP session so every response body is read with a hard byte cap.

    The inner request is made with ``stream=True``. A declared Content-Length above
    the cap is refused before reading; otherwise the body is read in chunks and
    the read stops (and the connection closes) as soon as the cap is exceeded.
    """

    def __init__(self, inner: Any, *, max_bytes: int, chunk_size: int = 65_536):
        if type(max_bytes) is not int or max_bytes <= 0 or type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError('positive byte bounds required')
        if isinstance(inner, BoundedSession):
            inner = inner.inner
        self.inner, self.max_bytes, self.chunk_size = inner, max_bytes, chunk_size

    @property
    def cookies(self):
        return self.inner.cookies

    def request(self, method, url, **kwargs):
        kwargs['stream'] = True
        response = self.inner.request(method, url, **kwargs)
        try:
            declared = response.headers.get('Content-Length')
            if declared is not None:
                try:
                    if int(declared) > self.max_bytes:
                        raise ResponseTooLarge('declared response length exceeds bound')
                except ValueError as exc:
                    if isinstance(exc, ResponseTooLarge):
                        raise
                    raise ResponseTooLarge('invalid declared response length') from exc
            body = bytearray()
            for chunk in response.iter_content(chunk_size=self.chunk_size):
                body.extend(chunk)
                if len(body) > self.max_bytes:
                    raise ResponseTooLarge('response body exceeds bound')
        except BaseException:
            response.close()
            raise
        response._content = bytes(body)
        response._content_consumed = True
        response.close()
        return response

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _ccxt_client(config: LiveConfig) -> Any:
    """Create a CCXT client lazily; the caller must pass all safety checks first."""
    try:
        import ccxt  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("Live exchange support requires the optional 'ccxt' package") from exc
    exchange_type = getattr(ccxt, config.exchange_id, None)
    if exchange_type is None:
        raise RuntimeError(f"Installed ccxt does not support {config.exchange_id}")
    params: dict[str, Any] = {
        "apiKey": config.api_key,
        "secret": config.api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    }
    if config.api_passphrase:
        params["password"] = config.api_passphrase
    return exchange_type(params)


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
        managed: bool = False,
        legacy_opt_in: bool = False,
    ) -> None:
        if type(managed) is not bool or type(legacy_opt_in) is not bool or managed and legacy_opt_in:
            raise LiveTradingDisabled("A managed account cannot opt into the legacy market primitive")
        self._managed, self._legacy_opt_in = managed, legacy_opt_in
        self.config = config or LiveConfig.from_env()
        if not self.config.enabled or not self.config.armed or not user_armed:
            raise LiveTradingDisabled(
                "Live trading requires SURGE_LIVE_TRADING_ENABLED=true, the exact armed latch, "
                "and user_armed=True"
            )
        if not self.config.api_key or not self.config.api_secret:
            raise LiveTradingDisabled("Exchange API key and secret are required")
        if self.config.exchange_id == "okx" and not self.config.api_passphrase:
            raise LiveTradingDisabled("OKX API passphrase is required")
        if self.config.max_order_quote <= 0:
            raise LiveTradingDisabled("SURGE_LIVE_MAX_ORDER_QUOTE must be positive")
        if self.config.exchange_id not in {"binance", "coinbaseadvanced", "okx"}:
            raise LiveTradingDisabled("Unsupported live exchange")
        self.client = client if client is not None else client_factory(self.config)

    @property
    def managed(self) -> bool:
        return self._managed

    @property
    def legacy_opt_in(self) -> bool:
        return self._legacy_opt_in

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
        # Unreachable for managed accounts; legacy callers must opt in explicitly.
        if self._managed or _is_managed_client(self.client):
            raise LiveTradingRejected("Managed accounts require prepared bounded transport")
        if self._legacy_opt_in is not True:
            raise LiveTradingRejected("Legacy market orders require explicit legacy_opt_in=True")
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


# These claims describe the exact CCXT call below, including its parameter names
# and Decimal-text serialization. A vendor's generic "supports limit" flag does
# not establish them.
MANAGED_CAPABILITIES = frozenset({
    'spot_usd', 'tick_lot_minimum', 'limit_ioc', 'ccxt_decimal_text',
    'ccxt_timeInForce_IOC', 'ccxt_clientOrderId', 'client_id_action_binding',
    'client_id_idempotency', 'client_id_lookup', 'usd_fee_bound', 'fill_history',
    'submit_no_retries',
})


class VenueCapabilityVerifier(ABC):
    """Trusted authority dependency, never a wire value or a caller approval bool.

    A production implementation must verify immutable source records against the
    exact configured client/version/account, all MarketRules, IOC and parameter
    semantics, collision-safe action-derived client IDs, idempotency/lookup,
    complete fill history and USD-only bounded fees. There is intentionally no
    production implementation or permissive default. Hashes alone are not proof.
    """

    @abstractmethod
    def verify(self, intent, market, now_us: int, *, client: Any) -> VerifiedVenueCapabilities | None:
        raise NotImplementedError


@dataclass(frozen=True)
class VerifiedVenueCapabilities:
    issuer: VenueCapabilityVerifier
    client: Any
    intent_hash: str
    market_hash: str
    verified_at_us: int
    expires_at_us: int
    source_record_hashes: tuple[str, ...]
    capabilities: frozenset[str]
    client_id_max_length: int


MAX_RESPONSE_BYTES = 1_048_576


class ManagedBoundedTransport:
    """Inert, injected-client transport boundary; no authority or live wiring.

    This is deliberately not VenuePort: submit_prepared returns an untrusted raw
    acknowledgment, not reconciled execution/fill evidence. A future live port
    must verify it and reconcile before capacity can be released. The authority
    must commit DISPATCHING under its dispatch mutex before invoking this method.
    Any create_order exception propagates unchanged: acceptance is unknown and
    the authority must retain the reservation. This layer never retries submits.
    """

    venue = 'coinbaseadvanced'

    def __init__(self, *, client: Any, account_id: str,
            capability_verifier: VenueCapabilityVerifier | None,
            clock_us: Callable[[], int], monotonic_ns: Callable[[], int]):
        if type(account_id) is not str or not account_id:
            raise LiveTradingRejected('Managed account identity required')
        self.client, self.account_id = client, account_id
        # The injected client is owned exclusively by this transport/LiveVenue.
        _register_managed_client(client)
        self.client_lock = threading.RLock()
        # Every HTTP response body (submit and reads) is bounded while streaming.
        self.max_response_bytes = MAX_RESPONSE_BYTES
        if hasattr(client, 'session'):
            client.session = BoundedSession(client.session, max_bytes=MAX_RESPONSE_BYTES)
        self.capability_verifier = capability_verifier
        self.clock_us, self.monotonic_ns = clock_us, monotonic_ns

    def submit_prepared(self, intent, market) -> dict[str, Any]:
        from .autonomous.contracts import (
            IntentState, MarketRules, PreparedIntent, Side, content_hash, decimal_text,
        )
        reject = LiveTradingRejected
        if type(intent) is not PreparedIntent or type(market) is not MarketRules:
            raise reject('Typed prepared intent and market rules required')
        p, reservation = intent.proposal, intent.reservation
        now, mono = self.clock_us(), self.monotonic_ns()
        if (type(now) is not int or now < 0 or type(mono) is not int or mono < 0
                or p.deadline_mono_ns <= mono or market.expires_at_us <= now):
            raise reject('Expired intent or market rules')
        if (p.account_id != self.account_id or intent.state is not IntentState.DISPATCHING
                or p.side not in (Side.BUY, Side.SELL)
                or p.side is Side.SELL and not p.reducing_only):
            raise reject('Invalid managed dispatch identity/state/side')
        if (market.venue != self.venue or not market.spot_allowed or not market.limit_ioc_verified
                or market.quote_asset != 'USD' or p.instrument != market.instrument
                or market.instrument != f'{market.base_asset}-USD'
                or not re.fullmatch(r'[A-Z0-9]+', market.base_asset)):
            raise reject('Verified Coinbase USD spot market required')
        if (reservation.intent_id != intent.intent_id
                or reservation.instrument != p.instrument or reservation.side != p.side.value
                or reservation.state is not IntentState.RESERVED
                or reservation.base_quantity != p.base_quantity
                or reservation.remaining_quantity != p.base_quantity):
            raise reject('Prepared reservation does not match dispatch')
        # Fraction operations preserve exact products and increments even at 38
        # significant digits and independently of ambient Decimal precision.
        quantity, price = decimal_text(p.base_quantity), decimal_text(p.limit_price)
        for value in (market.tick_size, market.lot_size, market.minimum_quantity,
                market.minimum_notional, market.fee_bound):
            decimal_text(value)
        q, price_value = Fraction(p.base_quantity), Fraction(p.limit_price)
        if (q <= 0 or price_value <= 0 or market.tick_size <= 0 or market.lot_size <= 0
                or market.minimum_quantity < 0 or market.minimum_notional < 0
                or q < market.minimum_quantity or q * price_value < market.minimum_notional
                or (q / Fraction(market.lot_size)).denominator != 1
                or (price_value / Fraction(market.tick_size)).denominator != 1
                or not 0 < market.fee_bound <= 1
                or not 0 <= market.max_price_precision <= 38
                or len(price.partition('.')[2]) > market.max_price_precision):
            raise reject('Invalid bounded quantity/price/increments/minimum/fee rules')
        fee_u = q * price_value * Fraction(market.fee_bound) * 1_000_000
        ceiling_fee_u = -(-fee_u.numerator // fee_u.denominator)
        if p.max_fee_usd_u < ceiling_fee_u:
            raise reject('Prepared fee buffer below verified bound')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', intent.client_order_id):
            raise reject('Unsupported client ID format')
        verifier = self.capability_verifier
        if not isinstance(verifier, VenueCapabilityVerifier):
            raise reject('Source capability verifier required')
        evidence = verifier.verify(intent, market, now, client=self.client)
        if (type(evidence) is not VerifiedVenueCapabilities or evidence.issuer is not verifier
                or evidence.client is not self.client
                or evidence.intent_hash != content_hash(intent) or evidence.market_hash != content_hash(market)
                or type(evidence.verified_at_us) is not int or type(evidence.expires_at_us) is not int
                or not 0 <= evidence.verified_at_us <= now < evidence.expires_at_us
                or type(evidence.capabilities) is not frozenset
                or not MANAGED_CAPABILITIES <= evidence.capabilities
                or type(evidence.source_record_hashes) is not tuple or not evidence.source_record_hashes
                or any(type(h) is not str or not re.fullmatch(r'[0-9a-f]{64}', h)
                    for h in evidence.source_record_hashes)
                or type(evidence.client_id_max_length) is not int
                or not len(intent.client_order_id) <= evidence.client_id_max_length <= 128):
            raise reject('Missing, unbound or expired venue capability evidence')
        # Verification may consume time; expiry is checked again immediately
        # before the sole effectful operation. No fallback, catch, or retry here.
        if (self.monotonic_ns() >= p.deadline_mono_ns
                or self.clock_us() >= min(market.expires_at_us, evidence.expires_at_us)):
            raise reject('Dispatch expired during capability verification')
        with self.client_lock:
            if self.monotonic_ns() >= p.deadline_mono_ns:
                raise reject('Dispatch expired waiting for client')
            return self.client.create_order(f'{market.base_asset}/USD', 'limit', p.side.value, quantity, price,
                {'timeInForce': 'IOC', 'clientOrderId': intent.client_order_id})
