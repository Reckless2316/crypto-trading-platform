"""Role entrypoint for the paper decision loop.

The process starts unarmed. It names which provider secrets a role may see, and
it refuses a live venue. A candle can be considered once. Crisis or a hard kill
stops later dispatches. The 00:15 UTC review hook may not approve trading.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field

from .paper_cycle import PaperCycle

_SECRETS = {
    "authority": frozenset({"SURGE_COINBASE_API_KEY", "SURGE_COINBASE_API_SECRET"}),
    "reflex": frozenset({"TYPESAFE_API_KEY"}),
    "brain": frozenset({"SURGE_BRAIN_API_KEY"}),
    "dashboard": frozenset(),
}
_ALL_SECRETS = frozenset().union(*_SECRETS.values())
_DAY_US = 86_400 * 1_000_000
_REVIEW_OFFSET_US = 15 * 60 * 1_000_000




def role_secret_names(role: str) -> frozenset[str]:
    if role not in _SECRETS:
        raise ValueError("unknown role")
    return _SECRETS[role]


def foreign_secret_names(role: str, env: dict) -> frozenset[str]:
    allowed = role_secret_names(role)
    found = frozenset(name for name in env if name in _ALL_SECRETS and name not in allowed)
    return found


def due_review_slot(now_us: int) -> int | None:
    if type(now_us) is not int or now_us < 0:
        raise ValueError("invalid clock")
    midnight = now_us - (now_us % _DAY_US)
    slot = midnight + _REVIEW_OFFSET_US
    if now_us >= slot:
        return slot
    return None


@dataclass
class PaperWorker:
    authority: object
    halted: bool = False
    last_close_us: int | None = None
    reviews_fired: set[int] = field(default_factory=set)
    considered: int = 0

    def __post_init__(self) -> None:
        self.cycle = PaperCycle(self.authority)
        self.activity = None
        self.activity_reports = []
        self.equity = None

    def attach_equity(self, connection, account_id: str) -> None:
        self.equity = (connection, account_id)

    def claim_review_slot(self, connection, now_us: int) -> bool:
        """Claim today's 00:15 UTC slot once. A clock before the slot claims nothing."""
        slot = due_review_slot(now_us)
        if slot is None:
            return False
        connection.execute(
            "CREATE TABLE IF NOT EXISTS review_slots(slot INTEGER PRIMARY KEY, fired_at_us INTEGER NOT NULL)")
        cursor = connection.execute(
            "INSERT OR IGNORE INTO review_slots(slot, fired_at_us) VALUES (?, ?)", (slot, now_us))
        connection.commit()
        claimed = cursor.rowcount == 1
        if not claimed:
            self.reviews_fired.add(slot)
        return claimed

    def attach_activity(self, connection, account_id: str, archive_hash: str) -> None:
        self.activity = (connection, account_id, archive_hash)

    def _fire_review(self, slot: int, now_us: int, review=None) -> None:
        if self.activity is not None:
            from .nightly import review_account_activity
            connection, account_id, archive_hash = self.activity
            report = review_account_activity(connection, account_id, now_us, archive_hash,
                brain=getattr(self, "review_brain", None))
            self.activity_reports.append(report)
            if report.approved:
                raise RuntimeError("nightly review cannot approve trading")
        if review is not None:
            approved = review(slot)
            if approved is True:
                raise RuntimeError("nightly review cannot approve trading")
        self.reviews_fired.add(slot)

    def on_clock(self, now_us: int) -> dict:
        """Run the daily review from the clock. This does not submit an order."""
        if type(now_us) is not int or now_us < 0:
            raise ValueError("invalid clock")
        slot = due_review_slot(now_us)
        if slot is None or slot in self.reviews_fired:
            return {"status": "idle", "reviewed": False, "dispatched": False}
        self._fire_review(slot, now_us)
        return {"status": "reviewed", "reviewed": True, "dispatched": False}

    def quote_book(self, bids, asks, band_bps: float = 10.) -> dict:
        """Compute mid, spread, and imbalance in code. This does not submit."""
        from .state import book_metrics
        mid, spread, imbalance = book_metrics(bids, asks, band_bps)
        return {"status": "quoted", "dispatched": False, "mid": mid, "spread": spread, "imbalance": imbalance}

    def hold_quoted_book(self, bids, asks, encode) -> dict:
        """Encode the quoted book and hold it. The snapshot must carry those three numbers."""
        quoted = self.quote_book(bids, asks)
        encoded = encode(quoted["mid"], quoted["spread"], quoted["imbalance"])
        wire = json.loads(encoded.canonical_state_json)
        row = wire[1][0]
        expected = (quoted["mid"], quoted["spread"], quoted["imbalance"])
        observed = (row[1], row[2], row[3])
        matched = all(abs(float(got) - float(want)) <= 1e-6 for got, want in zip(observed, expected))
        if not matched:
            return {"status": "quote_not_in_snapshot", "dispatched": False, "tokens": None}
        held = self.observe_state(encoded)
        held["mid"] = quoted["mid"]
        held["spread"] = quoted["spread"]
        held["imbalance"] = quoted["imbalance"]
        return held

    def account_features(self, mids, base_qty: float, nav: float, high_water: float, mark: float) -> dict:
        """Realized volatility, inventory, and drawdown stay in code."""
        import math
        import statistics
        if (len(mids) != 61 or any(type(price) not in (int, float) or price <= 0 or not math.isfinite(price) for price in mids)
                or type(base_qty) not in (int, float) or not math.isfinite(base_qty)
                or type(nav) not in (int, float) or type(high_water) not in (int, float)
                or high_water <= 0 or not math.isfinite(nav) or not math.isfinite(high_water)
                or not math.isfinite(mark) or mark <= 0):
            raise ValueError("invalid account inputs")
        returns = [math.log(current / previous) for previous, current in zip(mids, mids[1:])]
        rv = statistics.stdev(returns)
        inventory_usd = float(base_qty) * float(mark)
        drawdown = (float(high_water) - float(nav)) / float(high_water)
        return {"rv": rv, "rv_samples": 60, "base_inventory": float(base_qty),
                "inventory_usd": inventory_usd, "drawdown": drawdown, "dispatched": False}

    def block_from_equity_mark(self, connection, account_id: str) -> dict:
        """Read the latest equity mark and apply the exact loss check. Missing marks halt."""
        row = connection.execute(
            "SELECT nav, high_water_nav, day_open_nav FROM equity_marks WHERE account_id=? "
            "ORDER BY marked_at_us DESC, mark_id DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        if row is None:
            self.halted = True
            self.loss_halted = True
            return {"status": "equity_missing", "dispatched": False, "halted": True}
        from decimal import Decimal
        nav, high_water, day_open = Decimal(row[0]), Decimal(row[1]), Decimal(row[2])
        return self.loss_block(nav, high_water, day_open)

    def loss_block(self, nav, high_water, day_open) -> dict:
        """Exact 15% drawdown and 2% daily-loss checks. A hit never submits."""
        from decimal import Decimal
        from fractions import Fraction
        from .risk import loss_triggers

        def fraction(value):
            if type(value) is Fraction:
                return value
            if type(value) is Decimal:
                return Fraction(value)
            if type(value) is int:
                return Fraction(value)
            if type(value) is float:
                return Fraction(Decimal(str(value)))
            raise ValueError("invalid equity")

        hit_drawdown, hit_daily = loss_triggers(fraction(nav), fraction(high_water), fraction(day_open))
        blocked = hit_drawdown or hit_daily
        if blocked:
            self.halted = True
            self.loss_halted = True
        return {"status": "risk_blocked" if blocked else "risk_clear", "dispatched": False,
                "drawdown_hit": hit_drawdown, "daily_hit": hit_daily, "halted": self.halted}

    def hold_accounted_book(self, bids, asks, mids, base_qty, nav, high_water, encode, day_open=None) -> dict:
        """Hold one snapshot that carries the quote and the account features."""
        if day_open is not None:
            verdict = self.loss_block(nav, high_water, day_open)
            if verdict["status"] == "risk_blocked":
                return verdict
        quoted = self.quote_book(bids, asks)
        account = self.account_features(mids, base_qty, nav, high_water, quoted["mid"])
        encoded = encode(quoted["mid"], quoted["spread"], quoted["imbalance"], account)
        wire = json.loads(encoded.canonical_state_json)
        row = wire[1][0]
        expected = {
            "mid": quoted["mid"], "spread": quoted["spread"], "imbalance": quoted["imbalance"],
            "rv": account["rv"], "base_inventory": account["base_inventory"],
            "inventory_usd": account["inventory_usd"], "drawdown": account["drawdown"],
        }
        observed = {
            "mid": row[1], "spread": row[2], "imbalance": row[3], "rv": row[4],
            "base_inventory": row[7], "inventory_usd": row[8], "drawdown": wire[0][13],
        }
        matched = all(abs(float(observed[name]) - float(expected[name])) <= 1e-6 for name in expected)
        if not matched:
            return {"status": "account_not_in_snapshot", "dispatched": False}
        held = self.observe_state(encoded)
        held.update(expected)
        return held

    def apply_jev_batch(self, batch) -> dict:
        """A rejected or empty Jev batch holds. This method never submits."""
        reason = getattr(batch, "rejection_reason", None)
        decisions = tuple(getattr(batch, "decisions", ()) or ())
        if reason or not decisions:
            return {"status": "jev_hold", "dispatched": False, "decisions": len(decisions), "rejection": reason}
        gated = tuple(self.gate_jev_decision(decision) for decision in decisions)
        opened = [item for item in gated if item["status"] == "gate_open_unsubmitted"]
        status = "gate_open_unsubmitted" if len(opened) == len(gated) else gated[0]["status"]
        return {"status": status, "dispatched": False, "decisions": len(decisions), "gated": gated}

    def gate_jev_decision(self, decision) -> dict:
        """Code owns crisis, low confidence, and the entry gate. None of these submit."""
        from .contracts import CandidateDecision, Regime
        from .policy import entry_gate
        if type(decision) is not CandidateDecision:
            return {"status": "policy_hold", "dispatched": False, "reason": "POLICY_INPUT_INVALID"}
        if decision.regime is Regime.CRISIS:
            self.halted = True
            self.loss_halted = False
            return {"status": "escalated", "dispatched": False, "reason": "BRAIN_CRISIS", "halted": True}
        confidences = (decision.direction_confidence, decision.regime_confidence,
                       decision.quality_confidence, decision.risk_confidence)
        if any(type(value) not in (int, float) or value < 0.60 for value in confidences):
            return {"status": "escalated", "dispatched": False, "reason": "PAUSE_CANDIDATE_AND_REVIEW"}
        opened = entry_gate(decision.setup_quality, decision.direction_confidence, decision.risk_state,
                            decision.toxic_flow, decision.direction)
        reason = "NO_ENTRY_CONTEXT" if opened else "ENTRY_GATE"
        return {"status": "gate_open_unsubmitted" if opened else "policy_hold",
                "dispatched": False, "reason": reason}

    def observe_state(self, encoded) -> dict:
        """Accept a compact snapshot and hold. No judgment means no order."""
        tokens = getattr(getattr(encoded, "token_count", None), "state_tokens", None)
        if type(tokens) is not int or tokens < 0 or tokens > 399:
            return {"status": "state_rejected", "dispatched": False, "tokens": tokens}
        return {"status": "hold_no_judgment", "dispatched": False, "tokens": tokens}

    def serve(self, clock_us, sleep, stop, slot_connection=None, observe=None) -> int:
        """Keep reviewing from clock_us() until stop() is true. sleep is injected and not an order path."""
        if not callable(clock_us) or not callable(sleep) or not callable(stop):
            raise ValueError("serve needs a clock, a sleeper, and a stop flag")
        if observe is not None and not callable(observe):
            raise ValueError("observe must be callable")
        turns = 0
        while not stop():
            now_us = clock_us()
            if observe is not None:
                try:
                    observed = observe(now_us)
                except Exception as exc:
                    observed = {"error": type(exc).__name__}
                if not isinstance(observed, dict):
                    observed = {"error": "observe_not_dict"}
                observed["dispatched"] = False
            claimed = self.claim_review_slot(slot_connection, now_us) if slot_connection is not None else False
            result = self.on_clock(now_us)
            turns += 1
            if result["dispatched"]:
                raise RuntimeError("clock serve submitted an order")
            sleep()
        return turns

    def on_close(self, close_us: int, consider, review=None) -> dict:
        if type(close_us) is not int or close_us < 0:
            raise ValueError("invalid candle close")
        if self.last_close_us is not None and close_us <= self.last_close_us:
            return {"status": "duplicate_or_historical", "dispatched": False, "halted": self.halted}
        slot = due_review_slot(close_us)
        if slot is not None and slot not in self.reviews_fired:
            self._fire_review(slot, close_us, review)
        if self.equity is not None and not self.halted:
            connection, account_id = self.equity
            verdict = self.block_from_equity_mark(connection, account_id)
        if self.halted:
            self.last_close_us = close_us
            if getattr(self, "loss_halted", False):
                pass
            return {"status": "halted", "dispatched": False, "halted": True}
        outcome = consider(self.cycle)
        self.considered += 1
        self.last_close_us = close_us
        codes = outcome.reason_codes
        escalations = outcome.policy.escalation_codes if outcome.policy is not None else ()
        if "HARD_KILL" in codes or "BRAIN_CRISIS" in escalations or "crisis_halt" in getattr(outcome.policy, "halt_transitions", ()):
            self.halted = True
        return {"status": "processed", "dispatched": bool(outcome.dispatched), "halted": self.halted, "outcome": outcome}

    def consider_gated(self, close_us: int, **kwargs) -> dict:
        """Run the paper cycle only after the code gate opens. A closed gate does not submit."""
        gate = self.gate_jev_decision(kwargs.get("decision"))
        if gate["status"] != "gate_open_unsubmitted" or self.halted:
            return {"status": gate["status"], "dispatched": False, "reason": gate.get("reason"), "halted": self.halted}
        if kwargs.get("context") is None:
            return {"status": "policy_hold", "dispatched": False, "reason": "NO_ENTRY_CONTEXT", "halted": self.halted}
        result = self.consider_close(close_us, **kwargs)
        return result

    def consider_close(self, close_us: int, *, review=None, **kwargs) -> dict:
        """Run the real paper cycle for one close. Duplicate closes do not submit again."""
        request_id = kwargs.pop("request_id", None) or f"close-{close_us}"

        def consider(cycle):
            return cycle.consider(**kwargs, request_id=request_id)

        result = self.on_close(close_us, consider, review)
        result.pop("outcome", None)
        return result

    def dispatch_proposal(self, close_us: int, proposal, *, request_id: str) -> dict:
        """Submit one already gated proposal through the paper authority. A repeat close does not submit."""
        from .contracts import IntentReceipt
        from .paper_cycle import CycleOutcome

        def consider(cycle):
            receipt = cycle.authority.submit(proposal, decision=proposal.decision, request_id=request_id)
            dispatched = type(receipt) is IntentReceipt and receipt.status.value in ("pending", "completed")
            reasons = receipt.reason_codes if type(receipt) is IntentReceipt else ("INVALID_RECEIPT",)
            return CycleOutcome(dispatched, reasons, None, None, receipt if type(receipt) is IntentReceipt else None)

        result = self.on_close(close_us, consider)
        result.pop("outcome", None)
        return result


def _record_book_observation(observed: dict) -> None:
    """Append one held book. Eligibility and dispatch are fixed false."""
    import sqlite3
    from pathlib import Path
    path = Path("data/autonomous/book_observations.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS book_observations (
            sequence INTEGER PRIMARY KEY, exchange_time TEXT, bids INTEGER, asks INTEGER,
            reason TEXT, eligible INTEGER NOT NULL, dispatched INTEGER NOT NULL)""")
    connection.execute(
        "INSERT OR IGNORE INTO book_observations VALUES (?,?,?,?,?,0,0)",
        (observed.get("sequence"), observed.get("exchange_time"), observed.get("bids"),
         observed.get("asks"), observed.get("reason")))
    count = connection.execute("SELECT count(*) FROM book_observations").fetchone()[0]
    connection.commit()
    connection.close()
    top = observed.get("top")
    if isinstance(top, dict):
        _record_book_top(observed.get("sequence"), observed.get("exchange_time"), top)


def _record_book_top(sequence, exchange_time, top: dict) -> None:
    """Store one best bid and ask. Eligibility and dispatch stay false."""
    import sqlite3
    from pathlib import Path
    path = Path("data/autonomous/book_observations.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS book_tops (
            sequence INTEGER PRIMARY KEY, exchange_time TEXT, event_us INTEGER NOT NULL,
            bid_price TEXT NOT NULL, bid_size TEXT NOT NULL,
            ask_price TEXT NOT NULL, ask_size TEXT NOT NULL,
            eligible INTEGER NOT NULL, dispatched INTEGER NOT NULL)""")
    connection.execute(
        "INSERT OR IGNORE INTO book_tops VALUES (?,?,?,?,?,?,?,0,0)",
        (sequence, exchange_time, top.get("event_us"), top.get("bid_price"), top.get("bid_size"),
         top.get("ask_price"), top.get("ask_size")))
    count = connection.execute("SELECT count(*) FROM book_tops").fetchone()[0]
    connection.commit()
    connection.close()


def load_eth_tops(limit: int = 70) -> list:
    """Return stored ETH tops oldest-first. This table is not the BTC volatility series."""
    import sqlite3
    from pathlib import Path
    path = Path("data/autonomous/book_observations.sqlite3")
    if not path.exists():
        return []
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            """SELECT bid_price, bid_size, ask_price, ask_size, event_us, sequence
               FROM eth_book_tops ORDER BY event_us DESC LIMIT ?""", (limit,)).fetchall()
    except sqlite3.Error:
        rows = []
    connection.close()
    return [(((row[0], row[1]),), ((row[2], row[3]),), row[4], row[5]) for row in reversed(rows)]


def _record_eth_top(sequence, exchange_time, top: dict) -> None:
    """Store one ETH best bid and ask away from the BTC table."""
    import sqlite3
    from pathlib import Path
    path = Path("data/autonomous/book_observations.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS eth_book_tops (
            event_us INTEGER PRIMARY KEY, sequence INTEGER, exchange_time TEXT,
            bid_price TEXT NOT NULL, bid_size TEXT NOT NULL,
            ask_price TEXT NOT NULL, ask_size TEXT NOT NULL,
            eligible INTEGER NOT NULL, dispatched INTEGER NOT NULL)""")
    connection.execute(
        "INSERT OR IGNORE INTO eth_book_tops VALUES (?,?,?,?,?,?,?,0,0)",
        (top.get("event_us"), sequence, exchange_time, top.get("bid_price"), top.get("bid_size"),
         top.get("ask_price"), top.get("ask_size")))
    count = connection.execute("SELECT count(*) FROM eth_book_tops").fetchone()[0]
    connection.commit()
    connection.close()


def load_book_tops(limit: int = 70) -> list:
    """Return stored tops oldest-first for a later causal replay. Missing file is an empty series."""
    import sqlite3
    from pathlib import Path
    path = Path("data/autonomous/book_observations.sqlite3")
    if not path.exists():
        return []
    connection = sqlite3.connect(path)
    rows = connection.execute(
        """SELECT bid_price, bid_size, ask_price, ask_size, event_us, sequence
           FROM book_tops ORDER BY event_us DESC LIMIT ?""", (limit,)).fetchall()
    connection.close()
    return [(((row[0], row[1]),), ((row[2], row[3]),), row[4], row[5]) for row in reversed(rows)]


def minute_close_if_near(event_us: int, now_s: float):
    """Return the upcoming minute close only when this book is inside the last 5 seconds."""
    if type(event_us) is not int or event_us < 0 or type(now_s) not in (int, float):
        return None
    close_us = (event_us // 60_000_000 + 1) * 60_000_000
    away = close_us - event_us > 5_000_000
    remaining = close_us / 1_000_000 + 0.2 - now_s
    if away or remaining >= 30:
        return None
    return close_us


def load_causal_inventory(close_us: int, account_id: str = "paper-test", path=None):
    """Read journal inventory visible at one cutoff. A miss leaves the snapshot inventory invalid."""
    import sqlite3
    from pathlib import Path
    from .contracts import CausalCutoffs, ContractViolation
    from .ledger import Ledger
    ledger_path = Path("/tmp/paper-serve-ledger.sqlite3") if path is None else Path(path)
    if type(close_us) is not int or close_us < 0 or not ledger_path.exists():
        return None
    connection = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return Ledger(connection).inventory_as_of(
            account_id, CausalCutoffs(close_us, close_us, 1, "public-top"))
    except (ContractViolation, sqlite3.Error):
        return None
    finally:
        connection.close()


def load_mark_context(account_id: str = "paper-test", path=None):
    """Drawdown and daily loss from the stored mark. This does not reprice the account."""
    import sqlite3
    from decimal import Decimal
    from pathlib import Path
    ledger_path = Path("/tmp/paper-serve-ledger.sqlite3") if path is None else Path(path)
    shared = None
    marked_at_us = None
    if ledger_path.exists():
        connection = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                """SELECT nav, high_water_nav, day_open_nav, marked_at_us FROM equity_marks
                   WHERE account_id=? ORDER BY marked_at_us DESC, mark_id DESC LIMIT 1""",
                (account_id,)).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            connection.close()
        if row is not None:
            nav, high, day = Decimal(row[0]), Decimal(row[1]), Decimal(row[2])
            marked_at_us = row[3]
            if high > 0 and day > 0 and nav >= 0:
                drawdown = (high - nav) / high
                daily_loss = (day - nav) / day
                if 0 <= drawdown <= 1 and 0 <= daily_loss <= 1:
                    shared = {"drawdown": (float(drawdown), True), "daily_loss": (float(daily_loss), True)}
    return shared


def eth_return_context(close_us: int | None = None) -> dict:
    """One-hour ETH return from the ETH table. A short series stays invalid and is not mixed into BTC."""
    from .state import hold_public_top
    tops = load_eth_tops(70)
    context = {"eth_minute_books": 0, "eth_return_1h": None, "eth_return_1h_valid": False}
    if not tops:
        return context
    current = tops[-1]
    event_us = current[2]
    if close_us is None:
        close_us = (event_us // 60_000_000 + 1) * 60_000_000
        if close_us - event_us > 5_000_000:
            close_us = event_us
    if type(close_us) is not int or event_us > close_us:
        return context
    held = hold_public_top(current[0], current[1], event_us, current[3], symbol="ETH-USD",
                           prior=tuple(tops[:-1]), candle_close_us=close_us)
    context["eth_minute_books"] = held.get("minute_books") or 0
    context["eth_return_1h"] = held["eth_return_1h"]
    context["eth_return_1h_valid"] = held["eth_return_1h_valid"]
    return context


def replay_stored_tops() -> dict:
    """Replay stored best bids and asks into one causal snapshot. This does not submit."""
    from .state import hold_public_top
    tops = load_book_tops(70)
    if not tops:
        return {"tops": 0, "minute_books": 0, "required": 61, "mid_valid": False, "rv_valid": False,
                "eligible": False, "dispatched": False, "base_inventory_valid": False,
                "inventory_source": "unreconstructed"}
    current = tops[-1]
    event_us = current[2]
    close_us = (event_us // 60_000_000 + 1) * 60_000_000
    if close_us - event_us > 5_000_000:
        close_us = event_us
    inventory = load_causal_inventory(close_us)
    shared = load_mark_context() or {}
    eth = eth_return_context(close_us)
    if eth["eth_return_1h_valid"]:
        shared["eth_return_1h"] = (eth["eth_return_1h"], True)
    held = hold_public_top(current[0], current[1], event_us, current[3], prior=tuple(tops[:-1]),
                           candle_close_us=close_us, shared=shared, causal_inventory=inventory)
    return {"tops": len(tops), "minute_books": held.get("minute_books"), "required": 61,
            "mid_valid": held["mid_valid"], "rv_valid": held["rv_valid"], "reason": held["reason"],
            "base_inventory": held["base_inventory"], "base_inventory_valid": held["base_inventory_valid"],
            "other_inventory": held["other_inventory"], "inventory_source": held["inventory_source"],
            "spread_bps": held["spread_bps"], "roundtrip_cost_bps": held["roundtrip_cost_bps"],
            "roundtrip_cost_valid": held["roundtrip_cost_valid"], "fees_included": False,
            "position_headroom": held["position_headroom"],
            "position_headroom_valid": held["position_headroom_valid"],
            "btc_return_1h": held["btc_return_1h"],
            "btc_return_1h_valid": held["btc_return_1h_valid"],
            "eth_return_1h": held["eth_return_1h"],
            "eth_return_1h_valid": held["eth_return_1h_valid"],
            "eth_minute_books": eth["eth_minute_books"],
            "drawdown": held["drawdown"], "drawdown_valid": held["drawdown_valid"],
            "daily_loss": held["daily_loss"], "daily_loss_valid": held["daily_loss_valid"],
            "repriced": False, "eligible": False, "dispatched": False}


def _activity_reader(path):
    """Open report input without migrations, recovery or authority writes."""
    import sqlite3
    from pathlib import Path
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autonomous worker parser. Does not load keys or open a venue.")
    parser.add_argument("--role", choices=tuple(_SECRETS), help="Role whose provider-secret allowlist should be printed")
    parser.add_argument("--clock-once", action="store_true", help="Read the wall clock once and review if the slot is due. Does not submit.")
    parser.add_argument("--serve-seconds", type=float, help="Stay on the wall clock for this many seconds. Does not submit.")
    parser.add_argument("--serve", action="store_true", help="Stay on the wall clock until data/autonomous/worker.stop exists. Does not submit.")
    parser.add_argument("--ledger", help="Paper ledger to score when the review slot is claimed")
    parser.add_argument("--account", help="Account inside that ledger")
    parser.add_argument("--slot-db", default="data/autonomous/review_slots.sqlite")
    args = parser.parse_args(argv)
    if args.role:
        print(" ".join(sorted(role_secret_names(args.role))))
    if args.clock_once:
        import sqlite3
        from pathlib import Path
        from types import SimpleNamespace
        if bool(args.ledger) != bool(args.account):
            raise SystemExit("ledger and account are required together")
        now_us = time.time_ns() // 1000
        path = Path(args.slot_db)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        ledger = None
        if args.ledger:
            ledger = _activity_reader(args.ledger)
            worker.attach_activity(ledger, args.account, "e" * 64)
        claimed = worker.claim_review_slot(connection, now_us)
        connection.close()
        result = worker.on_clock(now_us)
        report = worker.activity_reports[-1] if worker.activity_reports else None
        print(json.dumps({"now_us": now_us, "status": result["status"], "reviewed": result["reviewed"],
                          "dispatched": result["dispatched"], "approved": None if report is None else report.approved,
                          "pending": None if report is None else report.pending,
                          "resolved": None if report is None else report.resolved}))
        if ledger is not None:
            ledger.close()
    if args.serve_seconds is not None:
        import sqlite3
        from types import SimpleNamespace
        if args.serve_seconds <= 0:
            raise SystemExit("serve-seconds must be positive")
        if bool(args.ledger) != bool(args.account):
            raise SystemExit("ledger and account are required together")
        deadline = time.time() + args.serve_seconds
        connection = sqlite3.connect(":memory:")
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        ledger = None
        if args.ledger:
            from pathlib import Path
            ledger = _activity_reader(args.ledger)
            worker.attach_activity(ledger, args.account, "e" * 64)
        turns = worker.serve(lambda: time.time_ns() // 1000, lambda: time.sleep(0.25),
                             lambda: time.time() >= deadline, connection)
        connection.close()
        report = worker.activity_reports[-1] if worker.activity_reports else None
        print(json.dumps({"turns": turns, "dispatched": False,
                          "approved": None if report is None else report.approved,
                          "brier": None if report is None else report.binary_brier,
                          "included": [] if report is None else list(report.included_ids)}))
        if ledger is not None:
            ledger.close()
    if args.serve:
        import sqlite3
        from pathlib import Path
        from types import SimpleNamespace
        stop_path = Path("data/autonomous/worker.stop")
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        if stop_path.exists():
            stop_path.unlink()
        slot_path = Path(args.slot_db)
        slot_path.parent.mkdir(parents=True, exist_ok=True)
        if bool(args.ledger) != bool(args.account):
            raise SystemExit("ledger and account are required together")
        connection = sqlite3.connect(slot_path)
        worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
        ledger = None
        if args.ledger:
            ledger = _activity_reader(args.ledger)
            worker.attach_activity(ledger, args.account, "e" * 64)
        history = load_book_tops()
        def observe(_now_us):
            from datetime import datetime
            from data.call_log import DataCallLog
            from data.public_metrics import fetch_coinbase_book
            from .state import hold_public_top
            def event_us_of(stamp):
                if not isinstance(stamp, str) or "." not in stamp or not stamp.endswith("Z"):
                    return None
                head, frac = stamp[:-1].split(".")
                return int(datetime.fromisoformat(head + "." + frac[:6] + "+00:00").timestamp() * 1_000_000)

            call_log = DataCallLog(Path("data/autonomous/public_calls.sqlite3"))
            def fetch_pair():
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=2) as pool:
                    btc_future = pool.submit(fetch_coinbase_book, call_log, "BTC-USD", level=2)
                    eth_future = pool.submit(fetch_coinbase_book, call_log, "ETH-USD", level=1)
                    paired_btc = btc_future.result()
                    try:
                        paired_eth = eth_future.result()
                        paired_error = None
                    except Exception as exc:
                        paired_eth = None
                        paired_error = type(exc).__name__
                return paired_btc, paired_eth, paired_error
            book, eth_book, eth_error = fetch_pair()
            event_us = event_us_of(book.get("exchange_time"))
            if event_us is not None:
                early_us = (60_000_000 - event_us % 60_000_000) % 60_000_000
                if 5_000_000 < early_us < 20_000_000:
                    first_time = book.get("exchange_time")
                    book, eth_book, eth_error = fetch_pair()
                    retried = event_us_of(book.get("exchange_time"))
                    if retried is not None:
                        event_us = retried
            observed = {"sequence": book.get("sequence"), "exchange_time": book.get("exchange_time"),
                        "bids": len(book.get("bids") or []), "asks": len(book.get("asks") or [])}
            if event_us is None:
                observed["snapshot"] = "time_unparsed"
                return observed
            close_us = minute_close_if_near(event_us, time.time())
            if close_us is None:
                observed["snapshot"] = "away_from_close"
                observed["recorded"] = False
                return observed
            try:
                if eth_error is not None or eth_book is None:
                    raise RuntimeError(eth_error or "missing_eth_book")
                eth_event = event_us_of(eth_book.get("exchange_time"))
                eth_close = minute_close_if_near(eth_event, time.time()) if eth_event is not None else None
                if eth_close == close_us and eth_book.get("bids") and eth_book.get("asks"):
                    eth_bid = max(eth_book["bids"], key=lambda row: float(row[0]))
                    eth_ask = min(eth_book["asks"], key=lambda row: float(row[0]))
                    _record_eth_top(eth_book.get("sequence"), eth_book.get("exchange_time"), {
                        "event_us": eth_event, "bid_price": str(eth_bid[0]), "bid_size": str(eth_bid[1]),
                        "ask_price": str(eth_ask[0]), "ask_size": str(eth_ask[1])})
                else:
                    early_us = None if eth_event is None else (60_000_000 - eth_event % 60_000_000) % 60_000_000
            except Exception as exc:
                pass
            remaining = close_us / 1_000_000 + 0.2 - time.time()
            if remaining > 0:
                time.sleep(remaining)
            inventory = load_causal_inventory(close_us, args.account, args.ledger) if args.ledger else None
            shared = load_mark_context(args.account, args.ledger) if args.ledger else {}
            if shared is None:
                shared = {}
            eth = eth_return_context(close_us)
            if eth["eth_return_1h_valid"]:
                shared["eth_return_1h"] = (eth["eth_return_1h"], True)
            observed["eth_minute_books"] = eth["eth_minute_books"]
            held = hold_public_top(book["bids"], book["asks"], event_us, int(book["sequence"]),
                                   prior=tuple(history), candle_close_us=close_us, shared=shared,
                                   causal_inventory=inventory)
            observed["repriced"] = False
            observed.update(held)
            gate = worker.gate_jev_decision(None)
            observed["gate_status"] = gate["status"]
            observed["gate_reason"] = gate["reason"]
            observed["gate_dispatched"] = gate["dispatched"]
            if book.get("bids") and book.get("asks"):
                bid = max(book["bids"], key=lambda row: float(row[0]))
                ask = min(book["asks"], key=lambda row: float(row[0]))
                history.append(((bid,), (ask,), event_us, int(book["sequence"])))
                del history[:-70]
                observed["top"] = {"event_us": event_us, "bid_price": str(bid[0]), "bid_size": str(bid[1]),
                                   "ask_price": str(ask[0]), "ask_size": str(ask[1])}
            _record_book_observation(observed)
            return observed

        def sleep_until_boundary():
            now = time.time()
            target = now - (now % 60) + 59.6
            if target <= now + 0.2:
                target += 60
            time.sleep(target - time.time())

        print(json.dumps({"status": "serving", "stop": str(stop_path), "dispatched": False}), flush=True)
        turns = worker.serve(lambda: time.time_ns() // 1000, sleep_until_boundary,
                             lambda: stop_path.exists(), connection, observe)
        connection.close()
        report = worker.activity_reports[-1] if worker.activity_reports else None
        print(json.dumps({"turns": turns, "dispatched": False,
                          "approved": None if report is None else report.approved,
                          "brier": None if report is None else report.binary_brier,
                          "included": [] if report is None else list(report.included_ids)}))
        if ledger is not None:
            ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
