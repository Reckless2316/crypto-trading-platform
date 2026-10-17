"""Fail-closed entry sizing: calibrated quarter-Kelly loss budget under hard capacity caps.

Sizing never trusts a constructed ``EdgeEstimate``: it re-derives the edge from
its ledger-verified evidence and requires equality. All money arithmetic is
exact Decimal; every rounding step moves toward a smaller authorization, and
all caps are rechecked on the final rounded order.
"""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext

from .contracts import (
    AccountView, EdgeEstimate, ExecutableBook, MarketRules, RiskLimits, SizedProposal, SizeRejection,
    content_hash,
)

MICRO = Decimal(1_000_000)
# Proposed initial entry guardrails (spec): unvalidated until measured; never loosened in code.
MAX_SPREAD_BPS = Decimal(10)
MAX_IMPACT_BPS = Decimal(10)
MAX_DEPTH_PARTICIPATION = Decimal("0.01")
# Hard ceilings regardless of configured limits.
KELLY_FRACTION_CEILING = Decimal("0.25")
ENTRY_LOSS_CEILING = Decimal("0.0025")


def kelly_loss_budget(equity: Decimal, p: Decimal, win: Decimal, loss: Decimal, *,
                      kelly_fraction: Decimal = KELLY_FRACTION_CEILING,
                      entry_cap: Decimal = ENTRY_LOSS_CEILING) -> Decimal:
    """Return capped fractional-Kelly loss exposure for a binary net payoff.

    The fraction is ``max(0, p - (1-p)/(win/loss))``, multiplied by
    ``kelly_fraction`` (at most one quarter) and capped at ``entry_cap`` of
    equity (at most 0.25%). Inputs must be finite Decimals.
    """
    values = (equity, p, win, loss, kelly_fraction, entry_cap)
    if any(type(value) is not Decimal or not value.is_finite() for value in values):
        raise ValueError("nonfinite or non-Decimal Kelly input")
    if equity <= 0 or not 0 <= p <= 1 or win <= 0 or loss <= 0:
        raise ValueError("invalid Kelly input")
    if not 0 < kelly_fraction <= KELLY_FRACTION_CEILING or not 0 < entry_cap <= ENTRY_LOSS_CEILING:
        raise ValueError("Kelly fraction or entry cap above hard ceiling")
    with localcontext() as context:
        context.prec = 50
        fraction = max(Decimal(0), p - (1 - p) / (win / loss))
        return equity * min(kelly_fraction * fraction, entry_cap)


def _floor_to(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _micro_ceil(value: Decimal) -> int:
    return int((value * MICRO).to_integral_value(rounding=ROUND_CEILING))


def size_entry(edge: EdgeEstimate, account: AccountView, market: MarketRules, limits: RiskLimits,
               book: ExecutableBook | None = None, *, now_us: int | None = None,
               edge_inputs: tuple | None = None, ledger=None) -> SizedProposal | SizeRejection:
    """Size one spot buy entry, or refuse with a typed reason.

    ``edge_inputs`` is ``(batch, decision, snapshot, artifact)``; with ``ledger``
    it must reproduce ``edge`` exactly through ``estimate_edge``.
    """
    from .calibration import estimate_edge
    from .contracts import Direction
    if (type(edge) is not EdgeEstimate or type(account) is not AccountView
            or type(market) is not MarketRules or type(limits) is not RiskLimits):
        return SizeRejection("typed_sizing_inputs_required")
    if not account.ready or account.exact_nav is None:
        return SizeRejection("reconciled_account_evidence_required")
    if not market.spot_allowed or not market.limit_ioc_verified:
        return SizeRejection("verified_spot_limit_capability_required")
    if type(book) is not ExecutableBook or type(now_us) is not int or edge_inputs is None or ledger is None:
        return SizeRejection("verified_edge_and_executable_book_evidence_unavailable")
    if type(edge_inputs) is not tuple or len(edge_inputs) != 4:
        return SizeRejection("verified_edge_and_executable_book_evidence_unavailable")
    # Spot entries are buys: only a LONG judgment may open exposure.
    if getattr(edge_inputs[1], "direction", None) is not Direction.LONG:
        return SizeRejection("spot_entry_requires_long")
    if now_us >= edge.valid_until_us:
        return SizeRejection("edge_expired")
    if estimate_edge(*edge_inputs, ledger=ledger, now_us=now_us) != edge:
        return SizeRejection("edge_not_reproducible")
    snapshot = edge_inputs[2]
    if (book.venue != market.venue or book.instrument != market.instrument
            or snapshot.instrument != market.instrument or snapshot.account_id != account.account_id):
        return SizeRejection("book_identity_mismatch")
    if market.expires_at_us <= now_us:
        return SizeRejection("market_capability_expired")
    if not 0 <= now_us - book.observed_at_us <= limits.max_book_age_ms * 1000:
        return SizeRejection("book_stale_or_future")
    if market.available_buy_notional_usd_u <= 0:
        return SizeRejection("venue_buy_capacity_unknown")
    if market.fee_bound <= 0:
        return SizeRejection("fee_bound_unknown")

    with localcontext() as context:
        context.prec = 50
        bid, ask = book.book.bids[0][0], book.book.asks[0][0]
        mid = (bid + ask) / 2
        if (ask - bid) / mid * 10_000 > MAX_SPREAD_BPS:
            return SizeRejection("spread_exceeds_guardrail")
        limit_price = _floor_to(ask * (1 + MAX_IMPACT_BPS / 10_000), market.tick_size)
        if limit_price < ask:
            return SizeRejection("tick_exceeds_impact_bound")
        depth_qty = sum((qty for price, qty in book.book.asks if price <= limit_price), Decimal(0))

        equity_u = account.exact_nav.equity_usd_u
        try:
            budget = kelly_loss_budget(Decimal(equity_u) / MICRO, Decimal(repr(edge.calibrated_probability)),
                edge.win_per_dollar, edge.loss_per_dollar,
                kelly_fraction=min(limits.kelly_fraction, KELLY_FRACTION_CEILING),
                entry_cap=min(limits.max_entry_loss_fraction, ENTRY_LOSS_CEILING))
        except ValueError:
            return SizeRejection("invalid_kelly_inputs")
        if budget <= 0:
            return SizeRejection("no_positive_kelly_edge")
        # Every cap is an integer microdollar floor, so a ceiling-rounded notional of a
        # floored quantity can never exceed it.
        floor_u = lambda value: int(value.to_integral_value(rounding=ROUND_FLOOR))
        reservations = account.reservations
        reserved_loss_u = sum(r.loss_budget_usd_u for r in reservations)
        reserved_gross_u = sum(r.gross_usd_u for r in reservations)
        reserved_cash_u = sum(r.cash_usd_u for r in reservations)
        loss_cap_u = min(floor_u(budget * MICRO),
            floor_u(Decimal(equity_u) * limits.max_pending_loss_fraction) - reserved_loss_u)
        if loss_cap_u <= 0:
            return SizeRejection("pending_loss_capacity_exhausted")
        held_u = sum(h.liquidation_value_usd_u for h in account.holdings)
        held_asset_u = sum(h.liquidation_value_usd_u for h in account.holdings if h.base_asset == market.base_asset)
        cash_u = floor_u(sum((b.quantity for b in account.balances if b.asset == "USD"), Decimal(0)) * MICRO) - reserved_cash_u
        caps_u = {
            "loss": floor_u(Decimal(loss_cap_u) / edge.loss_per_dollar),
            # Reservations are not instrument-tagged, so they count against both caps.
            "asset": floor_u(Decimal(equity_u) * limits.max_position_fraction) - held_asset_u - reserved_gross_u,
            "gross": floor_u(Decimal(equity_u) * limits.max_gross_fraction) - held_u - reserved_gross_u,
            "cash": floor_u(Decimal(cash_u) / (1 + market.fee_bound)),
            "venue": market.available_buy_notional_usd_u,
        }
        cap_u = min(caps_u.values())
        if cap_u <= 0:
            return SizeRejection("notional_capacity_exhausted")
        quantity = _floor_to(min(Decimal(cap_u) / MICRO / limit_price, depth_qty * MAX_DEPTH_PARTICIPATION), market.lot_size)
        if quantity <= 0 or quantity < market.minimum_quantity:
            return SizeRejection("below_venue_minimum")
        notional_u = _micro_ceil(quantity * limit_price)
        fee_u = _micro_ceil(Decimal(notional_u) / MICRO * market.fee_bound)
        if Decimal(notional_u) / MICRO < market.minimum_notional:
            return SizeRejection("below_venue_minimum")
        # Defensive recheck of the proofs above on the final rounded order.
        if (notional_u > cap_u or _micro_ceil(Decimal(notional_u) / MICRO * edge.loss_per_dollar) > loss_cap_u
                or notional_u + fee_u > cash_u):
            return SizeRejection("rounding_exceeds_caps")
        budget_u = loss_cap_u
    return SizedProposal(quantity, notional_u, budget_u, account.revision, edge.artifact_hash,
        market.instrument, limit_price, fee_u, book.lineage_hash, edge.supported_input_hash)
