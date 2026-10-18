"""Paper runtime -> OrderAuthority/Ledger adapter: one accounting path for paper and (later) live.

Every runtime entry and exit becomes a typed ``OrderProposal`` admitted by ``OrderAuthority.submit``: the ledger
reserves, re-runs ``risk.assess`` and its own hard checks, the ``PaperVenue`` fills against the runtime's fetched
executable book, fills and fees land in the formal Ledger, and ``OrderAuthority.reconcile`` releases reservations.
Runtime cash and positions are read back from ``ledger.account_view``.

This adapter never forges evidence. What the formal path needs and the runtime cannot yet produce is reported as a
refusal reason, not filled in:
  * entries need a persisted formal decision (DecisionReference into ``decisions``/``snapshots`` with an eligible
    candidate) and an entry verifier that reproduces attested sizing -> ``DECISION_PROVENANCE_UNAVAILABLE``;
  * every order needs an activated release and an operator permit/lease (release.py / control.py)
    -> ``NO_ACTIVE_RELEASE`` / ``PERMIT_NONE`` / ``LEASE_EXPIRED``;
  * only research candidates have wire identities -> ``NOT_A_CANDIDATE`` for e.g. BTC-USD.

The paper marks recorded here are the paper venue's own reconciliation source (local-paper provenance), exactly
the evidence class the ledger accepts for paper accounts. They are not venue attestations.

# HANDOFF(claude-opus-5.5 2026-10-17): remaining for the ledger accounting path ->
#   1. Entries: a formal decision record per runtime judgment (Task 7/10 batch persistence) plus a recorded
#      calibration attestation (Task 8) and an entry verifier bound to it; until then entries are refused.
#   2. Activation: DONE for releases (operator `activate_release` -> ledger schedule -> boundary apply). Still open:
#      control.py arm/lease renewal; nothing here writes account_control permits (tests use explicit fixtures).
#   3. DONE: PaperRuntime.serve wakes 1.2 s before UTC midnight, reads books and records `boundary_mark` exactly at
#      the boundary. A missed boundary (process down at midnight) leaves the account unready until the next one.
#   4. Sub-minimum reduce-only exits are refused by the formal minimum-notional check (runtime mode allows them).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Callable

from . import risk
from .contracts import (
    CANDIDATE_WIRE_IDS, BookPayload, ContractViolation, DecisionReference, ExecutableBook, MarketRules, OrderProposal,
    ReceiptStatus, ReconcileReason, RiskLimits, Side, content_hash, decimal_text,
)
from .execution import OrderAuthority, PaperVenue

CANDIDATE_INSTRUMENTS = {cid.upper() + "-USD": cid for cid, _ in CANDIDATE_WIRE_IDS}
_MICRO = Decimal(1_000_000)
_DEADLINE_NS = 5_000_000_000
_RULES_VALID_US = 60_000_000


@dataclass(frozen=True)
class AuthorityOutcome:
    ok: bool
    reasons: tuple[str, ...]
    intent_id: str | None = None
    state: str | None = None
    qty: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    reconciled: bool = False

    @property
    def avg_price(self) -> Decimal | None:
        return self.notional / self.qty if self.qty else None


def _step(value: Decimal, step: Decimal, rounding) -> Decimal:
    return (value / step).to_integral_value(rounding=rounding) * step


def _refused(*reasons: str) -> AuthorityOutcome:
    return AuthorityOutcome(False, tuple(reasons))


def open_paper_account(ledger, account_id: str, cash: Decimal) -> bool:
    """Create and fund a paper account once (local-paper initial evidence), then record its opening mark.

    Returns whether the opening mark left the account ready (it must sit exactly on a UTC day boundary)."""
    ledger.create_paper_account(account_id)
    if ledger.connection.execute("SELECT 1 FROM accounting_state WHERE account_id=?", (account_id,)).fetchone() is None:
        now = ledger._clock_us()
        payload = {"balances": [{"asset": "USD", "quantity": decimal_text(cash), "basis_usd_u": int(cash * _MICRO)}],
                   "units": decimal_text(cash)}
        evidence = ledger.import_local_evidence(account_id, f"runtime-initial-{now}", "initial", payload,
                                                event_at_us=now, received_at_us=now, known_at_us=now)
        ledger.initialize_account(evidence)
        RuntimeAuthority._record_paper_mark(ledger, account_id, {}, now)
    return ledger.account_view(account_id).ready


class RuntimeAuthority:
    """Owns the OrderAuthority + PaperVenue wiring for one paper ledger account."""

    def __init__(self, ledger, *, account_id: str, clock_us: Callable[[], int],
                 monotonic_ns: Callable[[], int] = time.monotonic_ns, fee_rate: Decimal = Decimal("0.006"),
                 slippage_bps: Decimal = Decimal(0), limit_slippage_bps: Decimal = Decimal(10), boot_id: str = "runtime-boot", limits: RiskLimits | None = None,
                 entry_verifier: Callable | None = None, configure_risk: bool = True):
        self.ledger, self.account_id, self.clock_us, self.monotonic_ns = ledger, account_id, clock_us, monotonic_ns
        # ``slippage_bps`` is the PaperVenue's adverse execution model; ``limit_slippage_bps`` bounds the IOC limit.
        self.fee_rate, self.slippage_bps, self.limit_bps, self.boot_id = fee_rate, slippage_bps, limit_slippage_bps, boot_id
        self._books: dict[str, ExecutableBook] = {}
        self._rules: dict[str, MarketRules] = {}
        if configure_risk:
            # Default limits are unarmed: the formal path refuses every entry until promotion configuration exists.
            ledger.configure_risk_checks(limits=limits or RiskLimits(), market=self.market_rules, assess=risk.assess,
                                         boot_id=boot_id, clock_us=clock_us, monotonic_ns=monotonic_ns)
        self.venue = PaperVenue(fee_rate=fee_rate, slippage_bps=slippage_bps, clock_us=clock_us)
        self.authority = OrderAuthority(ledger, self.venue, book_source=self._books.get, market_source=self.market_rules,
                                        entry_verifier=entry_verifier or (lambda *a: False), clock_us=clock_us,
                                        reconciliation_source=self._reconciliation_mark)

    # ---------- observations from the runtime ----------
    def observe(self, instrument: str, bids, asks, event_us: int, rules) -> None:
        """Convert the runtime's fetched book and public product rules into the authority's contract types."""
        def levels(rows, reverse):
            seen = {}
            for price, qty in rows:
                p, q = Decimal(str(price)), Decimal(str(qty))
                if p > 0 and q > 0:
                    seen[p] = seen.get(p, Decimal(0)) + q
            return tuple(sorted(seen.items(), reverse=reverse))
        payload = BookPayload(levels(bids, True), levels(asks, False))
        self._books[instrument] = ExecutableBook("paper", instrument, event_us, payload, content_hash(payload))
        tick = rules.quote_increment
        # The ledger settles quote at microdollar precision; coarsen the lot so lot x tick is a whole microdollar
        # multiple (e.g. 1e-8 BTC with a 0.01 tick becomes a 1e-4 lot). Never finer than the venue increment.
        needed = Decimal("0.000001") / tick
        lot = rules.base_increment if rules.base_increment >= needed else _step(needed, rules.base_increment, ROUND_CEILING)
        self._rules[instrument] = MarketRules(
            "paper", instrument, instrument.split("-", 1)[0], "USD", tick, lot, lot,
            max(rules.min_market_funds, Decimal("0.000001")), self.fee_rate, max(0, -tick.normalize().as_tuple().exponent),
            content_hash({k: decimal_text(v) if isinstance(v, Decimal) else v for k, v in vars(rules).items()}),
            event_us + _RULES_VALID_US,
            # The executing venue here is the PaperVenue, whose IOC-limit semantics are the code under test.
            spot_allowed=rules.tradable, limit_ioc_verified=True, book_at_us=event_us,
            available_buy_notional_usd_u=int(sum(p * q for p, q in payload.asks) * _MICRO))

    def market_rules(self, instrument: str) -> MarketRules | None:
        return self._rules.get(instrument)

    # ---------- read model ----------
    def view(self) -> dict:
        account = self.ledger.account_view(self.account_id)
        cash = sum((b.quantity for b in account.balances if b.asset == "USD"), Decimal(0))
        positions = {b.asset + "-USD": b.quantity for b in account.balances if b.asset != "USD" and b.quantity > 0}
        state = self.ledger.connection.execute("SELECT day_start_us FROM accounting_state WHERE account_id=?",
                                               (self.account_id,)).fetchone()
        return {"cash": cash, "positions": positions, "ready": account.ready,
                "reservations": len(account.reservations), "equity_usd_u": account.liquidation_equity_usd_u,
                "day_start_us": state[0] if state else None}

    def readiness(self, now_us: int) -> dict:
        """Operator-facing ledger state. A day without its exact-midnight mark waits for the next boundary."""
        view = self.view()
        day = now_us // 86_400_000_000 * 86_400_000_000
        out = {"ready": view["ready"], "reservations": view["reservations"], "equity_usd_u": view["equity_usd_u"]}
        if view["ready"]:
            out["state"] = "READY"
        elif view["day_start_us"] != day:
            out.update(state="LEDGER_AWAITING_MIDNIGHT_MARK", next_eligible_us=day + 86_400_000_000)
        else:
            out["state"] = "UNREADY"
        return out

    def last_entry_close(self, instrument: str) -> int | None:
        row = self.ledger.connection.execute(
            "SELECT i.candle_close_us FROM order_intents i WHERE i.account_id=? AND i.instrument=? AND i.side='buy' "
            "AND EXISTS (SELECT 1 FROM fills f WHERE f.intent_id=i.intent_id) ORDER BY i.recorded_at_us DESC LIMIT 1",
            (self.account_id, instrument)).fetchone()
        return row[0] if row else None

    def kill(self) -> None:
        """Durable operator kill in the ledger. Nothing here ever clears it."""
        self.ledger.latch_operator_kill(self.account_id)

    # ---------- paper reconciliation source ----------
    @staticmethod
    def _record_paper_mark(ledger, account_id: str, books: dict, now: int, *, record: bool = True) -> str:
        c = ledger.connection
        balances = {r["asset"]: r["native_qty"] for r in c.execute(
            "SELECT asset, native_qty FROM balance_positions WHERE account_id=? ORDER BY asset", (account_id,))}
        held = {asset for asset, qty in balances.items() if asset != "USD" and Decimal(qty) > 0}
        payload = {"balances": balances, "heartbeat_at_us": now, "liabilities_usd_u": 0, "exit_cost_usd_u": 0,
                   "open_intent_ids": [r[0] for r in c.execute(
                       "SELECT intent_id FROM order_intents WHERE account_id=? AND state NOT IN ('filled','canceled','rejected') "
                       "ORDER BY intent_id", (account_id,))],
                   "books": {asset: books[asset] for asset in held if asset in books}}
        sequence = c.execute("SELECT COUNT(*) FROM accounting_evidence WHERE account_id=?", (account_id,)).fetchone()[0]
        evidence = ledger.import_local_evidence(account_id, f"runtime-mark-{now}-{sequence}", "mark", payload,
                                                event_at_us=now, received_at_us=now, known_at_us=now)
        if record:
            ledger.record_mark(evidence)
        return evidence

    def _mark_books(self) -> dict:
        return {book.instrument.split("-", 1)[0]: {"at_us": book.observed_at_us, "quote_asset": "USD",
                "bids": [[decimal_text(p), decimal_text(q)] for p, q in book.book.bids]} for book in self._books.values()}

    def mark(self) -> bool:
        self._record_paper_mark(self.ledger, self.account_id, self._mark_books(), self.clock_us())
        return self.ledger.account_view(self.account_id).ready

    def boundary_mark(self, day_start_us: int) -> bool:
        """The exact-UTC-midnight mark: event time is the boundary, depth was observed <= 2 s before it.

        Must be called within 2 s after the boundary with books read just before it."""
        now = self.clock_us()
        if day_start_us % 86_400_000_000 or not 0 <= now - day_start_us <= 2_000_000:
            return False
        books = {asset: book for asset, book in self._mark_books().items() if 0 <= day_start_us - book["at_us"] <= 2_000_000}
        c = self.ledger.connection
        balances = {r["asset"]: r["native_qty"] for r in c.execute(
            "SELECT asset, native_qty FROM balance_positions WHERE account_id=? ORDER BY asset", (self.account_id,))}
        held = {asset for asset, qty in balances.items() if asset != "USD" and Decimal(qty) > 0}
        payload = {"balances": balances, "heartbeat_at_us": day_start_us, "liabilities_usd_u": 0, "exit_cost_usd_u": 0,
                   "open_intent_ids": [r[0] for r in c.execute(
                       "SELECT intent_id FROM order_intents WHERE account_id=? AND state NOT IN ('filled','canceled','rejected') "
                       "ORDER BY intent_id", (self.account_id,))],
                   "books": {asset: books[asset] for asset in held if asset in books}}
        evidence = self.ledger.import_local_evidence(self.account_id, f"runtime-boundary-{day_start_us}", "mark", payload,
                                                     event_at_us=day_start_us, received_at_us=now, known_at_us=now)
        self.ledger.record_mark(evidence)
        return self.ledger.account_view(self.account_id).ready

    def _reconciliation_mark(self, account_id: str) -> str:
        return self._record_paper_mark(self.ledger, account_id, self._mark_books(), self.clock_us(), record=False)

    def reconcile(self):
        return self.authority.reconcile(self.account_id, ReconcileReason.PERIODIC)

    # ---------- admission ----------
    def diagnose(self, instrument: str, *, reducing: bool) -> tuple[str, ...]:
        """Named refusals for what the formal path requires; ``submit`` remains the authority."""
        if instrument not in CANDIDATE_INSTRUMENTS:
            return ("NOT_A_CANDIDATE",)
        reasons = []
        row = self.ledger.connection.execute("SELECT * FROM account_control WHERE account_id=?", (self.account_id,)).fetchone()
        if row["hard_killed"]:
            reasons.append("HARD_KILL")
        if row["permit"] == "none":
            reasons.append("PERMIT_NONE")
        elif not reducing and row["permit"] != "entry":
            reasons.append("ENTRY_PERMIT_NONE")
        if row["active_release_hash"] is None:
            reasons.append("NO_ACTIVE_RELEASE")
        else:
            release = self.ledger.connection.execute("SELECT expires_at_us FROM release_bundles WHERE release_hash=?",
                                                     (row["active_release_hash"],)).fetchone()
            if release is None or release[0] <= self.clock_us():
                reasons.append("RELEASE_EXPIRED")
        if row["lease_deadline_mono_ns"] is None or self.monotonic_ns() >= row["lease_deadline_mono_ns"]:
            reasons.append("LEASE_EXPIRED")
        elif row["lease_boot_id"] != self.boot_id:
            reasons.append("LEASE_BOOT_MISMATCH")
        if instrument not in self._books or instrument not in self._rules:
            reasons.append("BOOK_OR_RULES_UNAVAILABLE")
        return tuple(reasons)

    def _control(self):
        row = self.ledger.connection.execute("SELECT permit_epoch, lease_deadline_mono_ns, active_release_hash FROM account_control "
                                             "WHERE account_id=?", (self.account_id,)).fetchone()
        return row[0], min(self.monotonic_ns() + _DEADLINE_NS, row[1]), row[2]

    def _reduction_slot(self, instrument: str) -> tuple[str, int] | None:
        c = self.ledger.connection
        parent = c.execute("SELECT i.intent_id FROM order_intents i WHERE i.account_id=? AND i.instrument=? AND i.side='buy' "
                           "AND i.state IN ('filled','partial','canceled') AND EXISTS (SELECT 1 FROM fills f WHERE f.intent_id=i.intent_id) "
                           "ORDER BY i.recorded_at_us DESC LIMIT 1", (self.account_id, instrument)).fetchone()
        if parent is None:
            return None
        prior = c.execute("SELECT COUNT(*) FROM order_intents WHERE account_id=? AND action_slot LIKE ?",
                          (self.account_id, f"reduce:{parent[0]}:%")).fetchone()[0]
        return parent[0], prior + 1

    def _submit(self, proposal: OrderProposal, decision) -> AuthorityOutcome:
        receipt = self.authority.submit(proposal, decision=decision, request_id="runtime-" + proposal.action_id)
        report = self.reconcile()
        fills = self.ledger.connection.execute("SELECT base_qty, quote_qty, fee_qty FROM fills WHERE intent_id=?",
                                               (receipt.intent_id,)).fetchall() if receipt.intent_id else []
        qty = sum((Decimal(r[0]) for r in fills), Decimal(0))
        notional = sum((Decimal(r[1]) for r in fills), Decimal(0))
        fee = sum((Decimal(r[2]) for r in fills), Decimal(0))
        reasons = tuple(receipt.reason_codes) + (() if report.ready else ("RECONCILE_INCOMPLETE",))
        ok = receipt.status is ReceiptStatus.COMPLETED and qty > 0
        return AuthorityOutcome(ok, reasons or (("NO_FILL",) if not ok else ()), receipt.intent_id,
                                receipt.state.value if receipt.state else None, qty, notional, fee, report.ready)

    def _fee_bound_u(self, qty: Decimal, price: Decimal, levels: int) -> int:
        # PaperVenue rounds each level's fee up to a microdollar; reserve that rounding per level.
        return int((qty * price * self.fee_rate * _MICRO).to_integral_value(rounding=ROUND_CEILING)) + levels

    def exit(self, instrument: str, close_us: int) -> AuthorityOutcome:
        """Reduce-only IOC sell of the full accounted holding through the authority."""
        self.ledger.enforce_lease(self.account_id)  # a lapsed deadman lease leaves exits on a bounded reduce lease
        if blocked := self.diagnose(instrument, reducing=True):
            return _refused(*blocked)
        slot = self._reduction_slot(instrument)
        if slot is None:
            return _refused("NO_LEDGER_ENTRY")
        try:
            if not self.mark():
                return _refused("ACCOUNT_UNREADY")
            book, rules = self._books[instrument], self._rules[instrument]
            owned = sum((b.quantity for b in self.ledger.account_view(self.account_id).balances if b.asset == rules.base_asset), Decimal(0))
            qty = _step(owned, rules.lot_size, ROUND_FLOOR)
            best_bid = book.book.bids[0][0]
            limit = _step(best_bid * (1 - self.limit_bps / 10_000), rules.tick_size, ROUND_CEILING)
            if qty < rules.minimum_quantity:
                return _refused("DUST")
            if qty * limit < rules.minimum_notional:
                return _refused("BELOW_MINIMUM_NOTIONAL")
            epoch, deadline, release = self._control()
            parent, ordinal = slot
            proposal = OrderProposal(content_hash(("runtime-exit", parent, ordinal)), self.account_id,
                                     CANDIDATE_INSTRUMENTS[instrument], instrument, Side.SELL, qty, limit,
                                     self._fee_bound_u(qty, best_bid, len(book.book.bids)), release, None, close_us,
                                     f"reduce:{parent}:{ordinal}", True, epoch, deadline, 0)
        except (ContractViolation, ValueError) as exc:
            return _refused("PROPOSAL_INVALID:" + type(exc).__name__)
        return self._submit(proposal, None)

    def entry(self, instrument: str, notional: Decimal, close_us: int, *, decision: DecisionReference | None,
              loss_budget_usd_u: int | None = None) -> AuthorityOutcome:
        """IOC limit buy through the authority; refused without a persisted formal decision (never forged)."""
        if decision is None:
            return _refused("DECISION_PROVENANCE_UNAVAILABLE")
        self.ledger.enforce_lease(self.account_id)  # deadman: a lapsed lease disarms entries before admission
        if blocked := self.diagnose(instrument, reducing=False):
            return _refused(*blocked)
        try:
            if not self.mark():
                return _refused("ACCOUNT_UNREADY")
            book, rules = self._books[instrument], self._rules[instrument]
            limit = _step(book.book.asks[0][0] * (1 + self.limit_bps / 10_000), rules.tick_size, ROUND_FLOOR)
            qty = _step(notional / limit, rules.lot_size, ROUND_FLOOR)
            if qty < rules.minimum_quantity or qty * limit < rules.minimum_notional:
                return _refused("BELOW_MINIMUM_NOTIONAL")
            equity_u = self.ledger.account_view(self.account_id).liquidation_equity_usd_u
            cap = equity_u // 400  # the formal 0.25% entry loss ceiling
            loss = min(loss_budget_usd_u or cap, cap)
            epoch, deadline, release = self._control()
            proposal = OrderProposal(content_hash(("runtime-entry", decision.decision_id, instrument)), self.account_id,
                                     CANDIDATE_INSTRUMENTS[instrument], instrument, Side.BUY, qty, limit,
                                     self._fee_bound_u(qty, limit, len(book.book.asks)), release, decision, close_us,
                                     "entry", False, epoch, deadline, loss)
        except (ContractViolation, ValueError) as exc:
            return _refused("PROPOSAL_INVALID:" + type(exc).__name__)
        return self._submit(proposal, decision)
