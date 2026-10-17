"""Inert typed builders shared by deterministic component tests."""
from decimal import Decimal

from bot.autonomous.contracts import (
    AccountRevision, AccountView, Balance, DecisionReference, Mode, OrderProposal, Side,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


def account_view(account_id: str = "paper-test") -> AccountView:
    """Synthetic $10,000 reconciled paper account; never a live fixture."""
    return AccountView(account_id=account_id, mode=Mode.PAPER,
        revision=AccountRevision(), liquidation_equity_usd_u=10_000_000_000,
        units=Decimal("10000"), nav=Decimal("1"), high_water_nav=Decimal("1"),
        day_open_nav=Decimal("1"), balances=(Balance("USD", Decimal("10000"), 10_000_000_000),),
        reconcile_id="test-reconcile", reconciled_at_us=1_000_000,
        private_heartbeat_us=1_000_000, marked_at_us=1_000_000, ready=True)


def order_proposal(account_id: str = "paper-test") -> OrderProposal:
    """Synthetic bounded entry; a proposal carries no execution permission."""
    return OrderProposal(action_id="action-test", account_id=account_id, candidate_id="aave",
        instrument="AAVE-USD", side=Side.BUY, base_quantity=Decimal("1"),
        limit_price=Decimal("100"), max_fee_usd_u=1_000_000, release_hash=HASH_A,
        decision=DecisionReference("decision-test", "request-test", HASH_B, HASH_A),
        candle_close_us=1_000_000, action_slot="entry", reducing_only=False,
        control_epoch=0, deadline_mono_ns=10_000_000_000, loss_budget_usd_u=2_000_000)
