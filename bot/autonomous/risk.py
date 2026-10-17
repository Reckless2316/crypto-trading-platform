"""Pure deterministic admission checks; persistence and dispatch belong to authority."""
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from .contracts import (
    AccountView, ControlState, ContractViolation, IntentState, MarketRules, Mode,
    OrderProposal, Permit, RiskLimits, RiskVerdict, Side, content_hash, decimal_text,
)


def _positive(value: Decimal) -> Fraction:
    decimal_text(value)
    if value <= 0:
        raise ContractViolation('invalid loss baseline')
    return Fraction(value)


def loss_fractions(nav: Decimal, high_water: Decimal, day_open: Decimal) -> tuple[Decimal, Decimal]:
    n,h,d=map(_positive,(nav,high_water,day_open))
    with localcontext(Context(prec=50)):
        return tuple(Decimal(x.numerator)/Decimal(x.denominator) for x in
            (max(Fraction(0),1-n/h),max(Fraction(0),1-n/d)))


def loss_triggers(nav: Fraction, high_water: Fraction, day_open: Fraction) -> tuple[bool,bool]:
    if any(type(x) is not Fraction or x<=0 for x in (nav,high_water,day_open)):
        raise ContractViolation('invalid exact loss baseline')
    # Fraction comparison uses integer cross products, independent of Decimal context.
    return 100*nav<=85*high_water,100*nav<=98*day_open


def loss_limit_reasons(drawdown: Decimal, daily_loss: Decimal) -> tuple[str, ...]:
    """Stable reporting helper; exact NAV evidence remains admission authority."""
    if not all(type(x) is Decimal and x.is_finite() and x >= 0 for x in (drawdown, daily_loss)):
        return ("INVALID_EQUITY",)
    reasons = []
    if drawdown >= Decimal(".15"):
        reasons.append("DRAWDOWN_LATCH")
    if daily_loss >= Decimal(".02"):
        reasons.append("DAILY_HALT")
    return tuple(reasons)


def _ceil_fraction(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def _usd_u(quantity: Decimal, price: Decimal) -> int:
    if type(quantity) is not Decimal or type(price) is not Decimal or not quantity.is_finite() or not price.is_finite():
        raise ContractViolation("invalid order economics")
    return _ceil_fraction(Fraction(quantity) * Fraction(price) * 1_000_000)


def _append(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def assess(proposal: OrderProposal, account: AccountView, control: ControlState,
           limits: RiskLimits, market: MarketRules, now_us: int, *,
           now_mono_ns: int | None = None, current_boot_id: str | None = None) -> RiskVerdict:
    """Assess one proposal against the supplied current snapshot without side effects.

    All monetary capacity checks use integer microdollars and cross-products. Exact NAV
    rational evidence, rather than its Decimal display projection, owns loss admission.
    """
    if (type(proposal), type(account), type(control), type(limits), type(market)) != (
            OrderProposal, AccountView, ControlState, RiskLimits, MarketRules):
        raise ContractViolation("typed risk inputs required")
    reasons: list[str] = []
    if type(now_us) is not int or now_us < 0:
        return RiskVerdict(False, ("INVALID_TIME",), account.revision, content_hash(proposal))
    buy = proposal.side is Side.BUY
    reduce = proposal.reducing_only

    if proposal.account_id != account.account_id:
        _append(reasons, "ACCOUNT_MISMATCH")
    if control.hard_killed:
        _append(reasons, "HARD_KILL")
    if not control.armed:
        _append(reasons, "DISARMED")
    if control.permit is Permit.NONE or (buy and control.permit is not Permit.ENTRY):
        _append(reasons, "PERMIT_DENIED")
    if control.boot_id is None or current_boot_id is None:
        _append(reasons, "LEASE_BOOT_UNKNOWN")
    elif control.boot_id != current_boot_id:
        _append(reasons, "LEASE_BOOT_MISMATCH")
    if control.deadman_deadline_mono_ns is None:
        _append(reasons, "LEASE_MISSING")
    elif type(now_mono_ns) is not int or now_mono_ns < 0:
        _append(reasons, "LEASE_TIME_UNAVAILABLE")
    elif now_mono_ns >= control.deadman_deadline_mono_ns:
        _append(reasons, "LEASE_EXPIRED")
    if type(now_mono_ns) is not int or now_mono_ns < 0:
        _append(reasons, "PROPOSAL_TIME_UNAVAILABLE")
    elif now_mono_ns >= proposal.deadline_mono_ns:
        _append(reasons, "PROPOSAL_EXPIRED")
    if (control.deadman_deadline_mono_ns is not None and
            proposal.deadline_mono_ns > control.deadman_deadline_mono_ns):
        _append(reasons, "PROPOSAL_EXCEEDS_LEASE")
    if proposal.control_epoch != control.control_epoch:
        _append(reasons, "STALE_CONTROL_EPOCH")
    if control.active_release_hash != proposal.release_hash:
        _append(reasons, "INACTIVE_RELEASE")
    if not control.reconciliation_ready:
        _append(reasons, "RECONCILIATION_REQUIRED")
    if account.mode is not limits.mode or account.mode is not control.mode or account.mode is Mode.SHADOW:
        _append(reasons, "MODE_MISMATCH")
    if not account.ready:
        _append(reasons, "ACCOUNT_UNREADY")

    if account.unknown_intent_ids:
        _append(reasons, "UNKNOWN_ORDERS")
    reservation_ids = {r.intent_id for r in account.reservations if r.state not in
        (IntentState.CANCELED, IntentState.REJECTED, IntentState.FILLED)}
    if set(account.open_intent_ids) - reservation_ids:
        _append(reasons, "UNKNOWN_ORDERS")
    if any(r.state is IntentState.UNKNOWN for r in account.reservations):
        _append(reasons, "UNKNOWN_ORDERS")

    if any(t is None or t > now_us or now_us - t > limits.reconciliation_age_ms * 1000
           for t in (account.reconciled_at_us,)):
        _append(reasons, "STALE_RECONCILIATION")
    if account.private_heartbeat_us is None or account.private_heartbeat_us > now_us or now_us-account.private_heartbeat_us > limits.private_heartbeat_ms*1000:
        _append(reasons, "STALE_PRIVATE_HEARTBEAT")
    if account.marked_at_us is None or account.marked_at_us > now_us or now_us-account.marked_at_us > limits.max_snapshot_age_ms*1000:
        _append(reasons, "STALE_ACCOUNT_MARK")
    if any(h.valuation_at_us > now_us or now_us-h.valuation_at_us > limits.max_snapshot_age_ms*1000
           for h in account.holdings):
        _append(reasons, "STALE_HOLDING_VALUATION")
    valued_assets = {h.base_asset for h in account.holdings}
    if any(b.asset != "USD" and b.quantity > 0 and b.asset not in valued_assets for b in account.balances):
        _append(reasons, "UNVALUED_HOLDING")

    evidence = account.exact_nav
    if evidence is None or evidence.account_id != account.account_id or evidence.revision != account.revision or evidence.equity_usd_u != account.liquidation_equity_usd_u:
        _append(reasons, "EXACT_NAV_UNAVAILABLE")
    elif account.liquidation_equity_usd_u <= 0 or evidence.nav.fraction() <= 0 or evidence.high_water_nav.fraction() <= 0 or evidence.day_open_nav.fraction() <= 0:
        _append(reasons, "INVALID_EQUITY")
    else:
        n, h, d = evidence.nav.fraction(), evidence.high_water_nav.fraction(), evidence.day_open_nav.fraction()
        # Decimal loss_fractions/loss_limit_reasons are reporting helpers only.
        # Admission is decided exclusively by the authoritative integer cross-products.
        hit_drawdown, hit_daily = loss_triggers(n, h, d)
        if hit_drawdown:
            _append(reasons, "DRAWDOWN_LATCH")
        if hit_daily:
            _append(reasons, "DAILY_HALT")
    if control.drawdown_latched:
        _append(reasons, "DRAWDOWN_LATCH")
    if control.daily_halted:
        _append(reasons, "DAILY_HALT")
    if control.crisis_halted and buy:
        _append(reasons, "CRISIS_HALT")

    if buy and (not limits.exposure_enabled or not limits.armed):
        _append(reasons, "EXPOSURE_DISABLED")
    if reduce and (not proposal.reducing_only or proposal.side is not Side.SELL):
        _append(reasons, "INVALID_REDUCE")
    if not buy and not reduce:
        _append(reasons, "INVALID_REDUCE")

    if market.instrument != proposal.instrument or market.quote_asset != "USD" or not market.spot_allowed:
        _append(reasons, "MARKET_NOT_ALLOWED")
    if not market.limit_ioc_verified:
        _append(reasons, "UNVERIFIED_ORDER_CAPABILITY")
    if market.expires_at_us <= now_us:
        _append(reasons, "MARKET_RULES_EXPIRED")
    if market.book_at_us is None or market.book_at_us > now_us or now_us-market.book_at_us > limits.max_book_age_ms*1000:
        _append(reasons, "STALE_BOOK")
    if (Fraction(proposal.limit_price) % Fraction(market.tick_size) or
            Fraction(proposal.base_quantity) % Fraction(market.lot_size) or
            proposal.base_quantity < market.minimum_quantity):
        _append(reasons, "INVALID_LOT_OR_TICK")

    pending = tuple(r for r in account.reservations if r.state not in
        (IntentState.CANCELED, IntentState.REJECTED, IntentState.FILLED))
    try:
        notional_u = _usd_u(proposal.base_quantity, proposal.limit_price)
        min_notional = Fraction(market.minimum_notional)
        fee_floor_u = _ceil_fraction(Fraction(notional_u) * Fraction(market.fee_bound))
        cash_u = notional_u + proposal.max_fee_usd_u if buy else 0
        if Fraction(proposal.base_quantity) * Fraction(proposal.limit_price) < min_notional:
            _append(reasons, "BELOW_MINIMUM_NOTIONAL")
        if proposal.max_fee_usd_u < fee_floor_u:
            _append(reasons, "INSUFFICIENT_FEE_BOUND")

        cash_exact = sum((Fraction(b.quantity) * 1_000_000 for b in account.balances if b.asset == "USD"), Fraction(0))
        usd_cash_u = cash_exact.numerator // cash_exact.denominator
        if cash_u + sum(r.cash_usd_u for r in pending) > usd_cash_u:
            _append(reasons, "INSUFFICIENT_CASH")

        if reduce:
            owned = sum((b.quantity for b in account.balances if b.asset == market.base_asset), Decimal(0))
            reserved = sum((r.remaining_quantity for r in pending), Decimal(0))
            if proposal.base_quantity > owned - reserved:
                _append(reasons, "INSUFFICIENT_UNRESERVED_BASE")
        if buy and evidence is not None and account.liquidation_equity_usd_u > 0:
            equity_u = account.liquidation_equity_usd_u
            held_asset_u = sum(h.liquidation_value_usd_u for h in account.holdings if h.instrument == proposal.instrument)
            pending_gross_u = sum(r.gross_usd_u for r in pending)
            if (held_asset_u + pending_gross_u + notional_u) * 100 > equity_u * 5:
                _append(reasons, "ASSET_CAP")
            held_gross_u = sum(h.liquidation_value_usd_u for h in account.holdings)
            if (held_gross_u + pending_gross_u + notional_u) * 100 > equity_u * 20:
                _append(reasons, "GROSS_CAP")
            if proposal.loss_budget_usd_u <= 0 or proposal.loss_budget_usd_u * 400 > equity_u:
                _append(reasons, "ENTRY_LOSS_CAP")
            if (proposal.loss_budget_usd_u + sum(r.loss_budget_usd_u for r in pending)) * 100 > equity_u:
                _append(reasons, "PENDING_LOSS_CAP")
            if notional_u > market.available_buy_notional_usd_u:
                _append(reasons, "INSUFFICIENT_BOUNDED_DEPTH")
    except (ArithmeticError, ValueError, TypeError, ContractViolation):
        _append(reasons, "INVALID_ORDER_ECONOMICS")

    return RiskVerdict(not reasons, tuple(reasons), account.revision, content_hash(proposal),
        halt_transitions=tuple(r for r in reasons if r in ("DRAWDOWN_LATCH", "DAILY_HALT")))
