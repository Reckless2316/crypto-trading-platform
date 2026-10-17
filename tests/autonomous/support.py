"""Inert typed builders shared by deterministic component tests."""
from decimal import Decimal

from bot.autonomous.contracts import (
    AccountRevision, AccountView, Balance, CausalCutoffs, DecisionReference, EncodedState,
    Mode, NumericSnapshot, OrderProposal, Side, TokenCount, canonical_bytes, content_hash,
    ExactRatio, ExactNavEvidence,
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
        private_heartbeat_us=1_000_000, marked_at_us=1_000_000, ready=True,
        exact_nav=ExactNavEvidence(account_id,'test-mark',AccountRevision(),1,10_000_000_000,
            ExactRatio('10000','1'),ExactRatio('1','1'),ExactRatio('1','1'),ExactRatio('1','1'),HASH_A))


def order_proposal(account_id: str = "paper-test") -> OrderProposal:
    """Synthetic bounded entry; a proposal carries no execution permission."""
    return OrderProposal(action_id="action-test", account_id=account_id, candidate_id="aave",
        instrument="AAVE-USD", side=Side.BUY, base_quantity=Decimal("1"),
        limit_price=Decimal("100"), max_fee_usd_u=1_000_000, release_hash=HASH_A,
        decision=DecisionReference("decision-test", "request-test", HASH_B, HASH_A),
        candle_close_us=1_000_000, action_slot="entry", reducing_only=False,
        control_epoch=0, deadline_mono_ns=10_000_000_000, loss_budget_usd_u=2_000_000)


def encoded_state(*, candidate_id="aave", wire_id=1, shared=None, row=None, snapshot_mask=0):
    """All-unknown inert numeric state, with optional malformed wire fixtures."""
    from bot.autonomous.contracts import Feature, default_field_dictionary
    row = [wire_id] + [0] * 15 if row is None else row
    shared = [1] + [0] * 16 if shared is None else shared
    definitions = default_field_dictionary().candidate_fields
    features = tuple(Feature(field.name, 0.0, bool(snapshot_mask & (1 << i))) for i, field in enumerate(definitions))
    snapshot = NumericSnapshot(candidate_id, "TEST-USD", HASH_A, CausalCutoffs(1, 2, 3, "boot"),
        features, snapshot_mask, HASH_B, AccountRevision())
    state = [shared, [row]]
    return EncodedState(canonical_bytes(state).decode("ascii"), content_hash(state), "d" * 64,
        (candidate_id,), TokenCount(399, "tok", "provider", "pinned_provider_tokenizer", "c" * 64), (snapshot,))
