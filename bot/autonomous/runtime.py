"""End-to-end PAPER runtime: public book -> causal snapshot -> REFLEX -> code gate -> sizing -> hard risk -> paper fill.

This process never holds venue credentials and never sends a real order. "Armed"
here is a local paper flag (``--arm-paper``); it cannot arm a live account. Every
threshold, size and side effect is computed in this module; the REFLEX only
returns a typed judgment. The offline reflex is deterministic rules over the
numeric snapshot and is UNCALIBRATED: it is labeled ``offline-reflex-v1`` in every
row and never presented as Jev.

Until a cohort has ``min_labels`` resolved outcomes the calibrated probability is
the prior 0.5, Kelly is zero, and no entry is sized (``CALIBRATION_INSUFFICIENT``).
An explicit ``--explore-fraction`` permits tiny labeled exploration entries
(hard ceiling 0.1% of NAV) so paper outcomes can accumulate.

# HANDOFF(claude-opus-5.5 2026-10-17, review fixes applied): remaining for this runtime ->
#   1. DONE (inert): `--reflex jev` uses bot/autonomous/jev_reflex.JevReflex -- one batched call per cycle, strict
#      validation, 250 ms budget, gated on JEV_API_KEY + operator-approved <data-dir>/jev_schema.approved.json.
#      Failures HOLD entries (REFLEX_UNAVAILABLE:<reason>); offline fallback only via --reflex-fallback offline.
#      Not yet: authenticated provider evaluation, tokenizer proof, measured latency/calibration (vendor claims).
#   2. Route paper orders through OrderAuthority/Ledger (Task 12/16) instead of this
#      runtime's own append-only fill table, so paper and live share one accounting path.
#   3. DONE: public Coinbase product details (increments, min funds, status) drive rounding and tradability
#      (TTL cache). The fee bound stays an ESTIMATE (0.6% taker); account-specific fee tiers need
#      authenticated evidence (Task 13) before any live use.
#   4. DONE: labels are executable (entry best ask, exit best bid, net round-trip fees) with per-regime
#      cohorts; still top-of-book only — walk depth for the typical notional, and add a slippage stress.
#      positions()/cash() rescan fills each cycle (fine at paper
#      volumes; snapshot them if fills grow large).
#   5. Dashboard read of status.json (Task 17); operator clear-latch / resolve-escalation are local
#      methods and still need the authenticated control path.
#   6. Warmup (fewer than 20 contiguous candles) gates entries only; exits and the crisis latch still act.
#      Warmup decisions are stored with phase='warmup' and never labeled or used for calibration.
#   7. Rules outages/untradable flags block entries only; exits use stale cached rules (or LOT/µUSD) and stop
#      only on trading_disabled/cancel_only. Dust (< base increment or < min funds) is flat for the entry slot
#      and reported as status.dust_usd; sub-increment dust is never sold. Schema runtime-v3 upgrades v2 in place;
#      Brier/nightly read label_version='exec-v1' only.
#   8. A database created before `runtime-v2` meta adopts the current config on first open; it is not
#      re-validated against the config that produced its historical rows.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Callable, Protocol

from .brain import DeterministicBrain
from .calibration import binary_brier, wilson_lower
from .contracts import AdvisoryRisk, Direction, Regime, content_hash
from .policy import entry_gate
from .risk import loss_fractions
from .sizing import kelly_loss_budget
from .state import book_metrics, require_token_budget

REFLEX_ID = "offline-reflex-v1"
SNAPSHOT_VERSION = "paper-snapshot-v1"
SCHEMA_VERSION = "runtime-v3"
UPGRADABLE_SCHEMAS = ("runtime-v2",)  # additive migrations only
LABEL_VERSION = "exec-v1"
ENTRY_RULES_MAX_AGE_US = 5 * 60 * 1_000_000
_DAY_US = 86_400 * 1_000_000
_HOUR_US = 3_600 * 1_000_000
PENDING_WINDOW_US = 7 * _DAY_US
LOT = Decimal("0.00000001")
MICRO_USD = Decimal("0.000001")
# Hard ceilings: configuration may tighten, never loosen.
DRAWDOWN_CEILING = 0.15
DAILY_LOSS_CEILING = 0.02
POSITION_CEILING = 0.05
EXPLORE_CEILING = 0.001
MIN_HISTORY = 20


class Unavailable(ValueError):
    """An observation cannot be used for this cycle."""


@dataclass(frozen=True)
class RuntimeConfig:
    instruments: tuple[str, ...] = ("BTC-USD", "ETH-USD")
    data_dir: Path = Path("data/autonomous/runtime")
    candle_seconds: int = 60
    horizon_candles: int = 60
    lookback_candles: int = 60
    starting_cash_usd: Decimal = Decimal("10000")
    fee_rate: Decimal = Decimal("0.006")  # ESTIMATE: Coinbase Advanced lowest-tier taker bound; not verified per account.
    max_slippage_bps: Decimal = Decimal("10")
    max_drawdown: float = DRAWDOWN_CEILING
    max_daily_loss: float = DAILY_LOSS_CEILING
    max_position_fraction: float = POSITION_CEILING
    explore_fraction: float = 0.0
    min_labels: int = 100
    max_book_age_us: int = 10_000_000
    close_tolerance_us: int = 10_000_000
    market_rules_ttl_us: int = 3_600_000_000
    reflex_fallback: str = "none"  # "offline" only by explicit operator flag; rows are then labeled offline
    brier_window: int = 2_000
    paper_armed: bool = False

    def __post_init__(self):
        if not (0 < self.max_drawdown <= DRAWDOWN_CEILING and 0 < self.max_daily_loss <= DAILY_LOSS_CEILING
                and 0 < self.max_position_fraction <= POSITION_CEILING and 0 <= self.explore_fraction <= EXPLORE_CEILING):
            raise ValueError("risk configuration exceeds a hard ceiling")
        if self.reflex_fallback not in ("none", "offline"):
            raise ValueError("reflex fallback must be none or offline")
        if not self.instruments or any(not i or "/" in i for i in self.instruments):
            raise ValueError("invalid instruments")
        if self.candle_seconds <= 0 or self.horizon_candles <= 0 or self.min_labels <= 0 or self.lookback_candles < MIN_HISTORY:
            raise ValueError("invalid cadence")


@dataclass(frozen=True)
class Snapshot:
    """The only state the REFLEX sees. Every field is computed in code from causal rows."""
    instrument: str
    candle_close_us: int
    event_us: int
    received_us: int
    cutoff_us: int
    mid: float
    spread_bps: float
    imbalance: float
    realized_vol_bps: float
    momentum_bps: float
    trend_t: float
    history: int
    inventory_fraction: float
    drawdown: float
    daily_loss: float

    def wire(self) -> str:
        values = {"v": SNAPSHOT_VERSION, "i": self.instrument, "c": self.candle_close_us,
                  "mid": round(self.mid, 8), "spr": round(self.spread_bps, 3), "imb": round(self.imbalance, 4),
                  "rv": round(self.realized_vol_bps, 3), "mom": round(self.momentum_bps, 3), "t": round(self.trend_t, 3),
                  "n": self.history, "inv": round(self.inventory_fraction, 5), "dd": round(self.drawdown, 5),
                  "dl": round(self.daily_loss, 5)}
        return json.dumps(values, separators=(",", ":"), sort_keys=True)

    def token_estimate(self) -> int:
        """ESTIMATE (not a provider tokenizer count): ceil(bytes/3), conservative for numeric JSON."""
        return math.ceil(len(self.wire().encode()) / 3)


@dataclass(frozen=True)
class Judgment:
    regime: Regime
    direction: Direction
    direction_confidence: float
    toxic_flow: bool
    setup_quality: int
    risk_state: AdvisoryRisk
    reflex_id: str
    calibrated: bool = False
    critical_confidence: float | None = None  # min over regime/direction/quality/risk confidences when the REFLEX reports them

    def __post_init__(self):
        if (type(self.regime) is not Regime or type(self.direction) is not Direction
                or type(self.risk_state) is not AdvisoryRisk or type(self.toxic_flow) is not bool
                or type(self.setup_quality) is not int or not 0 <= self.setup_quality <= 3
                or not math.isfinite(self.direction_confidence) or not 0 <= self.direction_confidence <= 1
                or (self.critical_confidence is not None and not (math.isfinite(self.critical_confidence)
                                                                   and 0 <= self.critical_confidence <= 1))):
            raise ValueError("invalid typed judgment")

    @property
    def escalation_confidence(self) -> float:
        return self.direction_confidence if self.critical_confidence is None else min(self.direction_confidence,
                                                                                      self.critical_confidence)


class ReflexPort(Protocol):
    reflex_id: str
    def judge(self, snapshot: Snapshot) -> Judgment: ...


class OfflineReflex:
    """Deterministic, UNCALIBRATED stand-in for Jev. Rules, not a model."""
    reflex_id = REFLEX_ID

    def judge(self, s: Snapshot) -> Judgment:
        t = abs(s.trend_t)
        if s.drawdown >= 0.10 or s.realized_vol_bps >= 100 or s.spread_bps >= 50:
            regime = Regime.CRISIS
        elif s.realized_vol_bps >= 25:
            regime = Regime.HIGH_VOL
        elif t >= 1.5:
            regime = Regime.TRENDING
        else:
            regime = Regime.MEAN_REVERTING
        if regime in (Regime.TRENDING, Regime.HIGH_VOL) and t >= 1.5:
            direction = Direction.LONG if s.trend_t > 0 else Direction.SHORT
            confidence = 0.5 + 0.45 * math.tanh(t / 2)
        else:
            direction, confidence = Direction.NEUTRAL, 0.95 - 0.45 * math.tanh(t / 1.5)
        toxic = s.spread_bps > 20 or abs(s.imbalance) > 0.9
        quality = (regime is Regime.TRENDING) + (s.spread_bps <= 10) + (t >= 2.5 and regime is not Regime.HIGH_VOL)
        if s.drawdown >= 0.10 or s.daily_loss >= 0.015:
            risk = AdvisoryRisk.REDUCE
        elif s.drawdown >= 0.05 or s.inventory_fraction >= 0.8 * POSITION_CEILING:
            risk = AdvisoryRisk.NEAR_LIMIT
        else:
            risk = AdvisoryRisk.SAFE
        return Judgment(regime, direction, round(confidence, 6), toxic, int(quality), risk, self.reflex_id)


@dataclass(frozen=True)
class MarketRules:
    """Public Coinbase product details. Increments are exchange-published; tradability is code-checked."""
    instrument: str
    base_increment: Decimal
    quote_increment: Decimal
    min_market_funds: Decimal
    status: str
    trading_disabled: bool
    post_only: bool
    limit_only: bool
    cancel_only: bool
    fetched_us: int

    @classmethod
    def parse(cls, raw: dict, fetched_us: int, instrument: str) -> "MarketRules":
        if not isinstance(raw, dict) or raw.get("id") != instrument:
            raise Unavailable("MARKET_RULES_UNAVAILABLE")
        try:
            increments = [Decimal(str(raw[k])) for k in ("base_increment", "quote_increment", "min_market_funds")]
        except Exception as exc:
            raise Unavailable("MARKET_RULES_UNAVAILABLE") from exc
        flags = [raw.get(k) for k in ("trading_disabled", "post_only", "limit_only", "cancel_only")]  # required, never defaulted
        if (any(not x.is_finite() or x < 0 for x in increments) or increments[0] <= 0 or increments[1] <= 0
                or any(type(f) is not bool for f in flags) or not isinstance(raw.get("status"), str)):
            raise Unavailable("MARKET_RULES_UNAVAILABLE")
        return cls(instrument, *increments, raw["status"], *flags, fetched_us)

    @property
    def tradable(self) -> bool:
        # limit_only is compatible with IOC limit orders; post_only cannot take liquidity; cancel_only cannot trade.
        return self.status == "online" and not (self.trading_disabled or self.post_only or self.cancel_only)

    @property
    def halted(self) -> bool:
        """A real venue halt: no order of any kind, exits included."""
        return self.trading_disabled or self.cancel_only

    def is_dust(self, qty: Decimal, bid: Decimal) -> bool:
        """Below one base increment, or worth less than the venue minimum: flat for the entry slot."""
        return qty < self.base_increment or qty * bid < self.min_market_funds

    @staticmethod
    def _step(value: Decimal, step: Decimal, rounding) -> Decimal:
        return (value / step).to_integral_value(rounding=rounding) * step

    def round_limit(self, price: Decimal, side: str) -> Decimal:
        """Adverse to authorization: a buy limit rounds down to the tick, a sell limit rounds up."""
        return self._step(price, self.quote_increment, ROUND_FLOOR if side == "buy" else ROUND_CEILING)

    def floor_qty(self, qty: Decimal) -> Decimal:
        return self._step(qty, self.base_increment, ROUND_FLOOR)

    def floor_quote(self, usd: Decimal) -> Decimal:
        return self._step(usd, self.quote_increment, ROUND_FLOOR)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS snapshots(instrument TEXT, candle_close_us INTEGER, event_us INTEGER, received_us INTEGER,
  cutoff_us INTEGER, mid REAL, best_bid REAL, best_ask REAL, wire TEXT, token_estimate INTEGER, snapshot_hash TEXT,
  PRIMARY KEY(instrument, candle_close_us));
CREATE TABLE IF NOT EXISTS decisions(decision_id TEXT PRIMARY KEY, instrument TEXT, candle_close_us INTEGER, reflex_id TEXT,
  calibrated INTEGER, regime TEXT, direction TEXT, confidence REAL, toxic INTEGER, quality INTEGER, risk_state TEXT,
  gate_open INTEGER, action TEXT, reasons TEXT, mid REAL, created_us INTEGER, phase TEXT DEFAULT 'live');
CREATE TABLE IF NOT EXISTS fills(fill_id TEXT PRIMARY KEY, decision_id TEXT, instrument TEXT, side TEXT, qty TEXT,
  avg_price TEXT, notional_usd TEXT, fee_usd TEXT, exploration INTEGER, created_us INTEGER);
CREATE TABLE IF NOT EXISTS equity_marks(mark_id INTEGER PRIMARY KEY AUTOINCREMENT, at_us INTEGER, equity_usd TEXT,
  cash_usd TEXT, high_water_usd TEXT, day_open_usd TEXT, day TEXT);
CREATE TABLE IF NOT EXISTS labels(decision_id TEXT PRIMARY KEY, instrument TEXT, direction TEXT, confidence REAL,
  entry_mid REAL, exit_mid REAL, net_return REAL, hit INTEGER, labeled_us INTEGER, status TEXT DEFAULT 'resolved',
  mid_return REAL, exec_return REAL, regime TEXT, label_version TEXT);
CREATE TABLE IF NOT EXISTS escalations(escalation_id TEXT PRIMARY KEY, decision_id TEXT, code TEXT, payload TEXT,
  status TEXT, created_us INTEGER);
CREATE TABLE IF NOT EXISTS escalation_resolutions(escalation_id TEXT PRIMARY KEY, note TEXT, resolved_us INTEGER);
CREATE TABLE IF NOT EXISTS reflex_calls(call_id INTEGER PRIMARY KEY AUTOINCREMENT, at_us INTEGER, reflex_id TEXT, outcome TEXT,
  latency_ms REAL, questions INTEGER, status INTEGER, request_hash TEXT, response_hash TEXT, provider_request_id TEXT);
CREATE TABLE IF NOT EXISTS latches(event_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, action TEXT, reason TEXT, at_us INTEGER);
"""
_INDEXES = """
CREATE INDEX IF NOT EXISTS decisions_close ON decisions(candle_close_us);
CREATE INDEX IF NOT EXISTS decisions_instrument ON decisions(instrument, candle_close_us);
CREATE INDEX IF NOT EXISTS decisions_created ON decisions(created_us);
CREATE INDEX IF NOT EXISTS labels_labeled ON labels(labeled_us);
CREATE INDEX IF NOT EXISTS labels_instrument ON labels(instrument, direction);
CREATE INDEX IF NOT EXISTS labels_status ON labels(status);
CREATE INDEX IF NOT EXISTS labels_version ON labels(label_version, status, labeled_us);
CREATE INDEX IF NOT EXISTS labels_cohort ON labels(instrument, direction, regime);
CREATE INDEX IF NOT EXISTS fills_instrument ON fills(instrument, created_us);
CREATE INDEX IF NOT EXISTS fills_created ON fills(created_us);
CREATE INDEX IF NOT EXISTS escalations_created ON escalations(created_us);
"""
_APPEND_ONLY = ("meta", "snapshots", "decisions", "fills", "equity_marks", "labels", "escalations",
                "escalation_resolutions", "latches", "reflex_calls")


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript(_SCHEMA)
    # Additive migrations for databases written by earlier runtime versions. Legacy labels keep NULL executable
    # returns and regimes, so they can never enter cost-aware, per-regime calibration.
    for table, column, decl in (("labels", "status", "TEXT DEFAULT 'resolved'"), ("labels", "mid_return", "REAL"),
                                ("labels", "exec_return", "REAL"), ("labels", "regime", "TEXT"),
                                ("labels", "label_version", "TEXT"),
                                ("decisions", "phase", "TEXT DEFAULT 'live'")):
        if column not in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    db.executescript(_INDEXES)
    for table in _APPEND_ONLY:
        for op in ("UPDATE", "DELETE"):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} "
                       f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END")
    return db


def parse_event_us(stamp) -> int:
    if not isinstance(stamp, str) or not stamp.endswith("Z"):
        raise Unavailable("BOOK_TIME_UNPARSED")
    head, _, frac = stamp[:-1].partition(".")
    try:
        whole = datetime.fromisoformat(head).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise Unavailable("BOOK_TIME_UNPARSED") from exc
    return int(whole.timestamp()) * 1_000_000 + int((frac + "000000")[:6] or 0)


def _levels(rows) -> list[tuple[float, float]]:
    out = []
    for row in rows or ():
        price, qty = float(row[0]), float(row[1])
        if math.isfinite(price) and math.isfinite(qty) and price > 0 and qty > 0:
            out.append((price, qty))
    return out


def paper_fill(levels, side: str, budget_usd: Decimal | None, qty: Decimal | None, limit: Decimal, fee_rate: Decimal,
               rules: MarketRules | None = None):
    """Adverse IOC paper fill. Walk levels to the limit; round qty down to the base increment and the fee up to the
    quote increment (µUSD without rules). Buys below the venue's minimum funds do not happen.

    Buys spend at most ``budget_usd`` including fee. Sells sell at most ``qty``.
    Returns (qty, avg_price, notional, fee) or None when nothing fills.
    """
    lot = rules.base_increment if rules else LOT
    fee_step = rules.quote_increment if rules else MICRO_USD
    filled = notional = Decimal(0)
    ordered = sorted(levels, key=lambda l: l[0], reverse=(side == "sell"))
    for price_f, size_f in ordered:
        price, size = Decimal(str(price_f)), Decimal(str(size_f))
        if (side == "buy" and price > limit) or (side == "sell" and price < limit):
            break
        if side == "buy":
            room = (budget_usd - fee_step - notional * (1 + fee_rate)) / (price * (1 + fee_rate))  # margin for fee ceil
        else:
            room = qty - filled
        take = MarketRules._step(min(size, room), lot, ROUND_FLOOR)
        if take <= 0:
            break
        filled += take
        notional += take * price
    if filled <= 0:
        return None
    fee = MarketRules._step(notional * fee_rate, fee_step, ROUND_CEILING)
    # Minimum funds bind entries only; a reduce-only sell may close a position worth less than the minimum.
    if side == "buy" and (notional + fee > budget_usd or (rules and notional < rules.min_market_funds)):
        return None
    return filled, (notional / filled), notional, fee


class PaperRuntime:
    PENDING_ESCALATIONS_SQL = ("SELECT COUNT(*) FROM escalations e INDEXED BY escalations_created LEFT JOIN escalation_resolutions r "
                               "USING(escalation_id) WHERE e.created_us>=? AND r.escalation_id IS NULL")
    OPEN_LOW_CONFIDENCE_SQL = ("SELECT 1 FROM escalations e INDEXED BY escalations_created JOIN decisions d USING(decision_id) "
                               "LEFT JOIN escalation_resolutions r USING(escalation_id) WHERE e.code='LOW_CONFIDENCE' "
                               "AND d.instrument=? AND e.created_us>? AND r.escalation_id IS NULL LIMIT 1")

    def __init__(self, config: RuntimeConfig, *, book_source: Callable[[str], dict] | None = None,
                 clock_us: Callable[[], int] | None = None, reflex: ReflexPort | None = None,
                 rules_source: Callable[[str], dict] | None = None):
        self.config = config
        self.clock_us = clock_us or (lambda: time.time_ns() // 1000)
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = open_db(config.data_dir / "runtime.sqlite3")
        try:
            self._bind_meta()
        except Exception:
            self.db.close()
            raise
        self.book_source = book_source or _public_book_source(config.data_dir)
        self.rules_source = rules_source or _public_rules_source(config.data_dir)
        self._rules: dict[str, MarketRules] = {}
        self._dust: dict[str, Decimal] = {}
        self.reflex = reflex or OfflineReflex()
        self._offline = OfflineReflex()
        self._batch: dict = {}
        self._batch_wires: dict[str, str] = {}
        self.jev_note = "jev_configured_unwired" if os.environ.get("JEV_API_KEY") else "jev_not_configured"
        if self.db.execute("SELECT COUNT(*) FROM equity_marks").fetchone()[0] == 0:
            now = self.clock_us()
            cash = str(config.starting_cash_usd)
            self.db.execute("INSERT INTO equity_marks(at_us,equity_usd,cash_usd,high_water_usd,day_open_usd,day) VALUES (?,?,?,?,?,?)",
                            (now, cash, cash, cash, cash, _day(now)))
        unlabeled = self.db.execute("SELECT MIN(d.candle_close_us) FROM decisions d LEFT JOIN labels l USING(decision_id) "
                                    "WHERE l.decision_id IS NULL AND d.phase='live'").fetchone()[0]
        self._label_floor_us = unlabeled if unlabeled is not None else 0

    def _bind_meta(self) -> None:
        """Accounting depends on starting cash and candle cadence; a restart may not silently change them."""
        expected = {"schema": SCHEMA_VERSION, "starting_cash_usd": str(self.config.starting_cash_usd),
                    "candle_seconds": str(self.config.candle_seconds)}
        with self._tx():  # first-open inserts are all-or-nothing; a concurrent opener sees all rows or none
            stored = {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM meta")}
            if not stored:
                for key, value in expected.items():
                    self.db.execute("INSERT INTO meta VALUES (?,?)", (key, value))
                return
            schema = self._stored_schema(stored)
            if schema in UPGRADABLE_SCHEMAS:
                self.db.execute("INSERT INTO meta VALUES (?,?)", ("schema@" + SCHEMA_VERSION, schema))  # append-only upgrade record
                schema = SCHEMA_VERSION
            elif schema != SCHEMA_VERSION:
                raise ValueError(f"unknown runtime schema {schema!r}; refusing to open")
        wrong = sorted(k for k, v in expected.items() if k != "schema" and stored.get(k) != v)
        if wrong:
            raise ValueError("config mismatch with stored runtime meta: " + ", ".join(wrong))

    @staticmethod
    def _stored_schema(stored: dict) -> str | None:
        upgrades = [k.partition("@")[2] for k in stored if k.startswith("schema@")]
        return max(upgrades) if upgrades else stored.get("schema")

    def schema_version(self) -> str | None:
        return self._stored_schema({r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM meta")})

    @contextmanager
    def _tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    # ---------- accounting (derived from append-only rows) ----------
    def positions(self) -> dict[str, Decimal]:
        held: dict[str, Decimal] = {}
        for r in self.db.execute("SELECT instrument, side, qty FROM fills"):
            q = Decimal(r["qty"])
            held[r["instrument"]] = held.get(r["instrument"], Decimal(0)) + (q if r["side"] == "buy" else -q)
        return {k: v for k, v in held.items() if v > 0}

    def cash(self) -> Decimal:
        cash = self.config.starting_cash_usd
        for r in self.db.execute("SELECT side, notional_usd, fee_usd FROM fills"):
            n, f = Decimal(r["notional_usd"]), Decimal(r["fee_usd"])
            cash += (-n - f) if r["side"] == "buy" else (n - f)
        return cash

    def _last_mark(self):
        return self.db.execute("SELECT * FROM equity_marks ORDER BY mark_id DESC LIMIT 1").fetchone()

    def latched(self) -> set[str]:
        active: set[str] = set()
        for r in self.db.execute("SELECT kind, action FROM latches ORDER BY event_id"):
            (active.add if r["action"] == "set" else active.discard)(r["kind"])
        return active

    def _latch(self, kind: str, reason: str, now: int) -> None:
        if kind not in self.latched():
            self.db.execute("INSERT INTO latches(kind,action,reason,at_us) VALUES (?,?,?,?)", (kind, "set", reason, now))

    def clear_latch(self, kind: str, reason: str = "operator") -> None:
        """Operator-only local action; the REFLEX has no path to this method."""
        self.db.execute("INSERT INTO latches(kind,action,reason,at_us) VALUES (?,?,?,?)", (kind, "clear", reason, self.clock_us()))

    def resolve_escalation(self, escalation_id: str, note: str) -> None:
        """BRAIN/operator acknowledgment; the escalation row itself stays immutable."""
        self.db.execute("INSERT INTO escalation_resolutions VALUES (?,?,?)", (escalation_id, note, self.clock_us()))

    def mark(self, bids_by: dict[str, float], now: int) -> dict:
        cash, held = self.cash(), self.positions()
        prices = {}
        for instrument in held:
            if instrument in bids_by:
                prices[instrument] = Decimal(str(bids_by[instrument]))
            else:  # last causal liquidation mark; a held asset is never valued at zero by omission
                row = self.db.execute("SELECT best_bid FROM snapshots WHERE instrument=? AND received_us<=? "
                                      "ORDER BY candle_close_us DESC LIMIT 1", (instrument, now)).fetchone()
                prices[instrument] = Decimal(str(row["best_bid"])) if row else Decimal(0)
        equity = cash + sum(q * prices[i] for i, q in held.items())
        last = self._last_mark()
        high = max(Decimal(last["high_water_usd"]), equity)
        day = _day(now)
        day_open = Decimal(last["day_open_usd"]) if last["day"] == day else Decimal(last["equity_usd"])
        self.db.execute("INSERT INTO equity_marks(at_us,equity_usd,cash_usd,high_water_usd,day_open_usd,day) VALUES (?,?,?,?,?,?)",
                        (now, str(equity), str(cash), str(high), str(day_open), day))
        drawdown, daily = (float(x) for x in loss_fractions(max(equity, MICRO_USD), high, day_open))
        if drawdown >= self.config.max_drawdown:
            self._latch("DRAWDOWN_LATCH", f"drawdown {drawdown:.4f}", now)
        if daily >= self.config.max_daily_loss:
            self._latch("DAILY_HALT:" + day, f"daily loss {daily:.4f}", now)
        return {"equity": equity, "cash": cash, "high_water": high, "day_open": day_open,
                "drawdown": drawdown, "daily_loss": daily, "positions": held}

    # ---------- state engine ----------
    def observe(self, instrument: str) -> tuple[dict, int, int]:
        book = self.book_source(instrument)
        received = self.clock_us()
        return book, parse_event_us(book.get("exchange_time")), received

    def market_rules(self, instrument: str, now: int, max_age_us: int | None = None) -> MarketRules:
        """Cached public product details; a failed refresh never extends a cache past its TTL."""
        cached = self._rules.get(instrument)
        age_limit = min(self.config.market_rules_ttl_us, max_age_us or self.config.market_rules_ttl_us)
        if cached is not None and now - cached.fetched_us < age_limit:
            return cached
        try:
            rules = MarketRules.parse(self.rules_source(instrument), now, instrument)
        except Exception as exc:
            raise Unavailable("MARKET_RULES_UNAVAILABLE") from exc
        self._rules[instrument] = rules
        return rules

    def snapshot(self, instrument, book, event_us, received_us, cutoff_us, account) -> Snapshot:
        """``candle_close_us`` is the true close: the candle boundary nearest the event, which must lie within
        ``close_tolerance_us`` of it. Features use only contiguous earlier closes received by the cutoff."""
        if event_us > cutoff_us or received_us > cutoff_us:
            raise Unavailable("FUTURE_OBSERVATION")
        if cutoff_us - event_us > self.config.max_book_age_us:
            raise Unavailable("STALE_BOOK")
        step = self.config.candle_seconds * 1_000_000
        # Nearest boundary: a pre-close book (local clock ahead of the venue) is that close's state; never later data.
        close = (event_us + step // 2) // step * step
        if abs(event_us - close) > self.config.close_tolerance_us:
            raise Unavailable("AWAY_FROM_CLOSE")
        bids, asks = _levels(book.get("bids")), _levels(book.get("asks"))
        try:
            mid, spread, imbalance = book_metrics(bids, asks)
        except ValueError as exc:
            raise Unavailable("INVALID_BOOK") from exc
        prior = []
        for r in self.db.execute("SELECT candle_close_us, mid FROM snapshots WHERE instrument=? AND candle_close_us<? "
                                 "AND candle_close_us>=? AND received_us<=? ORDER BY candle_close_us DESC",
                                 (instrument, close, close - self.config.lookback_candles * step, cutoff_us)):
            if r["candle_close_us"] != close - (len(prior) + 1) * step:
                break  # gap: older rows are not contiguous with this close
            prior.append(r["mid"])
        prior.reverse()
        mids = prior + [mid]
        returns = [1e4 * math.log(b / a) for a, b in zip(mids, mids[1:])]
        vol = statistics.pstdev(returns) if len(returns) >= 2 else 0.0
        momentum = 1e4 * math.log(mids[-1] / mids[0]) if len(mids) > 1 else 0.0
        t = momentum / (vol * math.sqrt(len(returns))) if vol > 0 and returns else 0.0
        held = account["positions"].get(instrument, Decimal(0))
        inventory = float(held * Decimal(str(max(p for p, _ in bids))) / account["equity"]) if account["equity"] > 0 else 0.0
        snap = Snapshot(instrument, close, event_us, received_us, cutoff_us, mid, spread, imbalance, vol, momentum, t,
                        len(prior), inventory, account["drawdown"], account["daily_loss"])
        require_token_budget(snap.token_estimate())
        return snap

    # ---------- calibration ----------
    def calibrated_edge(self, instrument: str, reflex_id: str, now_us: int, regime: str) -> tuple[Decimal, Decimal, Decimal, int]:
        """Wilson-lower hit rate and mean net payoffs over resolved long labels of gate-open decisions.

        Below ``min_labels`` the prior 0.5 with symmetric payoff is returned (Kelly zero). A supported cohort
        with no wins returns p=0; with no losses the loss per dollar is floored at the round-trip fee."""
        rows = self.db.execute("SELECT l.hit, l.exec_return FROM labels l JOIN decisions d USING(decision_id) "
                               "WHERE l.instrument=? AND l.direction='long' AND l.regime=? AND l.status='resolved' "
                               "AND l.exec_return IS NOT NULL AND l.label_version='" + LABEL_VERSION + "' AND d.reflex_id=? AND d.gate_open=1 AND d.phase='live' "
                               "AND l.labeled_us<=?", (instrument, regime, reflex_id, now_us)).fetchall()
        n = len(rows)
        if n < self.config.min_labels:
            return Decimal("0.5"), Decimal(1), Decimal(1), n
        cost = 2 * self.config.fee_rate
        wins = [r["exec_return"] for r in rows if r["hit"]]
        losses = [-r["exec_return"] for r in rows if not r["hit"] and r["exec_return"] < 0]
        if not wins:
            return Decimal(0), cost, cost, n
        loss = Decimal(str(statistics.fmean(losses))) if losses else cost
        return Decimal(str(wilson_lower(len(wins), n))), Decimal(str(statistics.fmean(wins))), max(loss, cost), n

    _LABEL_INSERT = ("INSERT INTO labels(decision_id,instrument,direction,confidence,entry_mid,exit_mid,net_return,hit,"
                     "labeled_us,status,mid_return,exec_return,regime,label_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")

    def label_matured(self, now: int) -> int:
        """Label each decision from the snapshot at exactly close+horizon; a missing close is UNRESOLVABLE.

        Directional outcomes are executable: a long enters at the decision close's best ask and exits at the
        horizon close's best bid, net of the round-trip fee bound (a short mirrors this). The mid return is stored
        alongside for diagnostics only; calibration and sizing read ``exec_return``."""
        step = self.config.candle_seconds * 1_000_000
        horizon = self.config.horizon_candles * step
        cost = 2 * float(self.config.fee_rate)
        count, pending = 0, []
        for d in self.db.execute("SELECT d.* FROM decisions d LEFT JOIN labels l USING(decision_id) WHERE l.decision_id IS NULL "
                                 "AND d.phase='live' AND d.candle_close_us>=? AND d.candle_close_us+?<=?", (self._label_floor_us, horizon, now)).fetchall():
            target = d["candle_close_us"] + horizon
            later = self.db.execute("SELECT mid, best_bid, best_ask FROM snapshots WHERE instrument=? AND candle_close_us>=? "
                                    "AND candle_close_us<? AND received_us<=?", (d["instrument"], target, target + step, now)).fetchone()
            entry = self.db.execute("SELECT best_bid, best_ask FROM snapshots WHERE instrument=? AND candle_close_us=?",
                                    (d["instrument"], d["candle_close_us"])).fetchone()
            if later is None or entry is None:
                if later is None and now < target + step + self.config.close_tolerance_us:
                    pending.append(d["candle_close_us"])
                    continue
                self.db.execute(self._LABEL_INSERT, (d["decision_id"], d["instrument"], d["direction"], d["confidence"], d["mid"],
                                None, None, None, now, "unresolvable", None, None, d["regime"], LABEL_VERSION))
                count += 1
                continue
            mid_return = later["mid"] / d["mid"] - 1
            if d["direction"] == "long":
                exec_return = later["best_bid"] / entry["best_ask"] - 1 - cost
                net, hit = exec_return, exec_return > 0
            elif d["direction"] == "short":
                exec_return = 1 - later["best_ask"] / entry["best_bid"] - cost  # sell at bid0, buy back at ask_h
                net, hit = exec_return, exec_return > 0
            else:  # neutral is not a trade: it is right when the move stays inside the round-trip cost
                exec_return, net, hit = None, -abs(mid_return), abs(mid_return) < cost
            self.db.execute(self._LABEL_INSERT, (d["decision_id"], d["instrument"], d["direction"], d["confidence"], d["mid"],
                            later["mid"], net, int(hit), now, "resolved", mid_return, exec_return, d["regime"], LABEL_VERSION))
            count += 1
        self._label_floor_us = min(pending) if pending else max(self._label_floor_us, now - horizon - step)
        return count

    def brier(self, kind: str = "all") -> tuple[float | None, int]:
        """Binary Brier of the predicted choice over the last ``brier_window`` resolved labels."""
        where = {"all": "", "directional": "AND direction IN ('long','short')", "neutral": "AND direction='neutral'"}[kind]
        rows = self.db.execute(f"SELECT confidence, hit FROM labels WHERE label_version='{LABEL_VERSION}' AND status='resolved' {where} "
                               "ORDER BY labeled_us DESC LIMIT ?", (self.config.brier_window,)).fetchall()
        if not rows:
            return None, 0
        return binary_brier(tuple(float(r["confidence"]) for r in rows), tuple(int(r["hit"]) for r in rows)), len(rows)

    # ---------- hard risk (code only; the model never reaches this) ----------
    def risk_check(self, side: str, instrument: str, notional: Decimal, account: dict) -> tuple[str, ...]:
        reasons = []
        if (self.config.data_dir / "KILL").exists():
            reasons.append("KILL_SWITCH")
        if not self.config.paper_armed:
            reasons.append("PAPER_NOT_ARMED")
        if side == "buy":
            latched = self.latched()
            if "DRAWDOWN_LATCH" in latched or account["drawdown"] >= self.config.max_drawdown:
                reasons.append("DRAWDOWN_LATCH")
            if any(k.startswith("DAILY_HALT:" + _day(self.clock_us())) for k in latched) or account["daily_loss"] >= self.config.max_daily_loss:
                reasons.append("DAILY_HALT")
            if "CRISIS_HALT" in latched:
                reasons.append("CRISIS_HALT")
            held_value = account["positions"].get(instrument, Decimal(0)) * account.get("bid", {}).get(instrument, Decimal(0))
            if held_value + notional > Decimal(str(self.config.max_position_fraction)) * account["equity"]:
                reasons.append("MAX_POSITION")
            if notional * (1 + self.config.fee_rate) > account["cash"]:
                reasons.append("INSUFFICIENT_CASH")
        return tuple(reasons)

    # ---------- one cycle ----------
    def cycle(self) -> dict:
        observed, errors = {}, {}
        for instrument in self.config.instruments:
            try:
                observed[instrument] = self.observe(instrument)
            except Exception as exc:  # public read failures hold, never trade
                errors[instrument] = type(exc).__name__ if not isinstance(exc, Unavailable) else str(exc)
        cutoff = self.clock_us()
        bids = {i: max(_levels(b.get("bids")) or [(0.0, 0.0)])[0] for i, (b, _, _) in observed.items()}
        with self._tx():
            account = self.mark({i: p for i, p in bids.items() if p > 0}, cutoff)
        account["bid"] = {i: Decimal(str(p)) for i, p in bids.items()}
        results = {i: {"status": "hold", "reasons": [e]} for i, e in errors.items()}
        self._judge_cycle(observed, cutoff, account)
        for instrument, (book, event_us, received_us) in observed.items():
            try:
                with self._tx():  # snapshot, decision, escalations and fill commit together or not at all
                    results[instrument] = self._decide(instrument, book, event_us, received_us, cutoff, account)
            except Unavailable as exc:
                results[instrument] = {"status": "hold", "reasons": [str(exc)]}
            except Exception as exc:
                results[instrument] = {"status": "hold", "reasons": ["DECISION_ERROR:" + type(exc).__name__]}
            account["cash"], account["positions"] = self.cash(), self.positions()
        with self._tx():
            labeled = self.label_matured(cutoff)
        return self.write_status(cutoff, results, labeled)

    def _judge_cycle(self, observed: dict, cutoff: int, account: dict) -> None:
        """Batch-capable REFLEX: one call per cycle for every instrument that is past warmup or holds a position."""
        self._batch, self._batch_wires = {}, {}
        if not hasattr(self.reflex, "judge_batch"):
            return
        snaps = {}
        for instrument, (book, event_us, received_us) in observed.items():
            try:
                snap = self.snapshot(instrument, book, event_us, received_us, cutoff, account)
            except Exception:
                continue  # the per-instrument decision records the reason
            if snap.history >= MIN_HISTORY or account["positions"].get(instrument, Decimal(0)) > 0:
                snaps[instrument] = snap
        if not snaps:
            return
        try:
            self._batch = dict(self.reflex.judge_batch(snaps))
        except Exception as exc:  # an adapter bug is still only a REFLEX outage
            self._batch = {i: "adapter_" + type(exc).__name__ for i in snaps}
        self._batch_wires = {i: s.wire() for i, s in snaps.items()}
        call = getattr(self.reflex, "last_call", None) or {}
        with self._tx():
            self.db.execute("INSERT INTO reflex_calls(at_us,reflex_id,outcome,latency_ms,questions,status,request_hash,"
                            "response_hash,provider_request_id) VALUES (?,?,?,?,?,?,?,?,?)",
                            (cutoff, self.reflex.reflex_id, call.get("outcome"), call.get("latency_ms"), call.get("questions"),
                             call.get("status"), call.get("request_hash"), call.get("response_hash"), call.get("provider_request_id")))

    def _judgment(self, instrument: str, snap: Snapshot) -> tuple[Judgment | None, list[str]]:
        if not hasattr(self.reflex, "judge_batch"):
            return self.reflex.judge(snap), []
        result = self._batch.get(instrument, "not_in_batch")
        if isinstance(result, Judgment) and self._batch_wires.get(instrument) == snap.wire():
            return result, []
        reason = result if isinstance(result, str) else "batch_snapshot_mismatch"
        if self.config.reflex_fallback == "offline":
            return self._offline.judge(snap), ["REFLEX_FALLBACK_OFFLINE:" + reason]
        return None, ["REFLEX_UNAVAILABLE:" + reason]  # never a silent fallback

    def _decide(self, instrument, book, event_us, received_us, cutoff, account) -> dict:
        snap = self.snapshot(instrument, book, event_us, received_us, cutoff, account)
        bids, asks = _levels(book.get("bids")), _levels(book.get("asks"))
        best_bid, best_ask = max(p for p, _ in bids), min(p for p, _ in asks)
        try:
            self.db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)", (instrument, snap.candle_close_us, event_us,
                            received_us, cutoff, snap.mid, best_bid, best_ask, snap.wire(), snap.token_estimate(),
                            content_hash(snap.wire())))
        except sqlite3.IntegrityError:
            raise Unavailable("DUPLICATE_CANDLE")
        # Missing or untradable rules block the ENTRY path only. Exits use the last cached rules even when expired
        # (default LOT/µUSD rounding without any) and are refused only by a real venue halt.
        entry_block = None
        try:
            rules = self.market_rules(instrument, cutoff)
            if not rules.tradable:
                entry_block = "MARKET_UNAVAILABLE"
        except Unavailable as exc:
            rules, entry_block = self._rules.get(instrument), str(exc)
        warm = snap.history < MIN_HISTORY
        held = account["positions"].get(instrument, Decimal(0))
        bid = Decimal(str(best_bid))
        dust = held > 0 and (rules.is_dust(held, bid) if rules else held < LOT)
        sellable = held > 0 and (rules.floor_qty(held) if rules else MarketRules._step(held, LOT, ROUND_FLOOR)) > 0
        if dust:
            self._dust[instrument] = held * bid
        else:
            self._dust.pop(instrument, None)
        flat = held <= 0 or dust
        if warm and flat and hasattr(self.reflex, "judge_batch") and instrument not in self._batch:
            return {"status": "warmup", "reasons": ["WARMUP"], "history": snap.history, "snapshot": json.loads(snap.wire())}
        judgment, reflex_notes = self._judgment(instrument, snap)
        if warm and flat and (judgment is None or judgment.regime is not Regime.CRISIS):
            # Warmup gates entries only: with no position and no crisis there is nothing for code to do.
            return {"status": "warmup", "reasons": ["WARMUP"], "history": snap.history, "snapshot": json.loads(snap.wire())}
        reflex_id = judgment.reflex_id if judgment else self.reflex.reflex_id
        decision_id = content_hash((instrument, snap.candle_close_us, reflex_id))
        gate = judgment is not None and entry_gate(judgment.setup_quality, judgment.direction_confidence, judgment.risk_state,
                                                   judgment.toxic_flow, judgment.direction)
        escalate = []
        if judgment is not None and judgment.regime is Regime.CRISIS:
            escalate.append("BRAIN_CRISIS")
            self._latch("CRISIS_HALT", "reflex regime crisis", cutoff)
        if judgment is not None and judgment.escalation_confidence < 0.60 and not warm:  # short history is not judged
            escalate.append("LOW_CONFIDENCE")
        reasons, action = reflex_notes + list(escalate) + (["WARMUP"] if warm else []), "hold"
        fill, exploration = None, False
        if (self.config.data_dir / "KILL").exists():
            reasons.append("KILL_SWITCH")  # armed kill file: no order path is entered at all
        elif sellable and (exit_reason := self._exit_reason(instrument, judgment, snap.candle_close_us, account)):
            # Exits are evaluated independently of escalation, warmup and rules outages; those only block entries.
            if entry_block:
                reasons.append(entry_block)
            if rules is not None and rules.halted:
                reasons += ["EXIT_REFUSED_MARKET_HALT", exit_reason]
            elif blocked := self.risk_check("sell", instrument, Decimal(0), account):
                reasons += blocked
            else:
                raw_limit = bid * (1 - self.config.max_slippage_bps / Decimal(10_000))
                limit = rules.round_limit(raw_limit, "sell") if rules else raw_limit
                qty = rules.floor_qty(held) if rules else MarketRules._step(held, LOT, ROUND_FLOOR)
                fill = paper_fill(bids, "sell", None, qty, limit, self.config.fee_rate, rules)
                action = "sell" if fill else "hold"
                reasons += ["EXIT", exit_reason] if fill else ["EXIT_UNFILLED", exit_reason]
        elif held > 0 and not dust:
            if not escalate:
                reasons.append("ENTRY_SLOT_OCCUPIED")  # one entry per instrument until its exit
        elif escalate or warm or judgment is None:
            pass  # escalation, warmup and a REFLEX outage block entries only
        elif entry_block:
            reasons.append(entry_block)
        elif not gate:
            reasons.append("ENTRY_GATE_REFUSED")
        else:
            try:  # entries never act on product rules older than five minutes
                rules = self.market_rules(instrument, cutoff, max_age_us=ENTRY_RULES_MAX_AGE_US)
            except Unavailable as exc:
                rules, notional, sizing = None, Decimal(0), [str(exc)]
            else:
                notional, exploration, sizing = self._entry_notional(instrument, judgment, account, rules)
                if rules.tradable is False:
                    notional, sizing = Decimal(0), ["MARKET_UNAVAILABLE"]
            reasons += sizing
            if notional > 0 and notional < rules.min_market_funds * (1 + self.config.fee_rate):
                notional = Decimal(0)
                reasons.append("BELOW_MIN_FUNDS")
            if notional > 0:
                if blocked := self.risk_check("buy", instrument, notional, account):
                    reasons += blocked
                else:
                    limit = rules.round_limit(Decimal(str(best_ask)) * (1 + self.config.max_slippage_bps / Decimal(10_000)), "buy")
                    fill = paper_fill(asks, "buy", notional * (1 + self.config.fee_rate), None, limit, self.config.fee_rate, rules)
                    action = "buy" if fill else "hold"
                    if not fill:
                        reasons.append("ENTRY_UNFILLED")
        phase = "warmup" if warm else "live" if judgment is not None else "reflex_unavailable"
        typed = (int(judgment.calibrated), judgment.regime.value, judgment.direction.value, judgment.direction_confidence,
                 int(judgment.toxic_flow), judgment.setup_quality, judgment.risk_state.value) if judgment else (0,) + (None,) * 6
        self.db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (decision_id, instrument, snap.candle_close_us,
                        reflex_id, *typed, int(gate and not warm), action, json.dumps(reasons), snap.mid, cutoff, phase))
        for code in escalate:
            if code == "LOW_CONFIDENCE" and self.db.execute(self.OPEN_LOW_CONFIDENCE_SQL, (instrument, cutoff - _HOUR_US)).fetchone():
                continue  # at most one unresolved low-confidence escalation per instrument per hour
            self.db.execute("INSERT INTO escalations VALUES (?,?,?,?,?,?)", (content_hash((decision_id, code)), decision_id, code,
                            json.dumps({"snapshot": json.loads(snap.wire()), "judgment": _judgment_dict(judgment)}), "pending_brain", cutoff))
        if fill:
            qty, avg, notional, fee = fill
            self.db.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?)", (content_hash((decision_id, action)), decision_id, instrument,
                            action, str(qty), str(avg), str(notional), str(fee), int(action == "buy" and exploration), cutoff))
        return {"status": "warmup" if warm and action == "hold" else action, "reasons": reasons, "decision_id": decision_id,
                "judgment": _judgment_dict(judgment) if judgment else None, "history": snap.history,
                "gate_open": gate and not warm, "snapshot": json.loads(snap.wire()), "token_estimate": snap.token_estimate()}

    def _entry_notional(self, instrument, judgment: Judgment, account, rules: MarketRules) -> tuple[Decimal, bool, list[str]]:
        """Quarter-Kelly notional capped (never refused) by position room and cash; explore only while unsupported."""
        p, win, loss, n = self.calibrated_edge(instrument, judgment.reflex_id, self.clock_us(), judgment.regime.value)
        if n < self.config.min_labels:
            if self.config.explore_fraction <= 0:
                return Decimal(0), False, ["CALIBRATION_INSUFFICIENT"]
            explore = rules.floor_quote(account["equity"] * Decimal(str(self.config.explore_fraction)))
            return explore, True, ["CALIBRATION_INSUFFICIENT", "EXPLORATION"]
        budget = kelly_loss_budget(account["equity"], p, win, loss)
        if budget <= 0:
            return Decimal(0), False, ["NEGATIVE_EDGE"]
        kelly = budget / loss
        held_value = account["positions"].get(instrument, Decimal(0)) * account.get("bid", {}).get(instrument, Decimal(0))
        room = min(Decimal(str(self.config.max_position_fraction)) * account["equity"] - held_value,
                   account["cash"] / (1 + self.config.fee_rate))
        notional = rules.floor_quote(min(kelly, room))
        if notional <= 0:
            return Decimal(0), False, ["MAX_POSITION"]
        return notional, False, ["SIZE_CAPPED"] if notional < kelly else []

    def _exit_reason(self, instrument, judgment: Judgment | None, close_us: int, account) -> str | None:
        last_buy = self.db.execute("SELECT d.candle_close_us FROM fills f JOIN decisions d USING(decision_id) "
                                   "WHERE f.instrument=? AND f.side='buy' ORDER BY f.created_us DESC LIMIT 1", (instrument,)).fetchone()
        horizon = self.config.horizon_candles * self.config.candle_seconds * 1_000_000
        if last_buy is not None and close_us - last_buy["candle_close_us"] >= horizon:
            return "TIME_STOP"
        if "DRAWDOWN_LATCH" in self.latched() or account["drawdown"] >= self.config.max_drawdown:
            return "DRAWDOWN"
        if judgment is None:
            return None  # without a judgment only the code exits above apply
        if judgment.regime is Regime.CRISIS:
            return "CRISIS"
        if judgment.risk_state is AdvisoryRisk.REDUCE:
            return "REDUCE"
        if judgment.direction is Direction.SHORT and judgment.direction_confidence > 0.80:
            return "SIGNAL_SHORT"
        return None

    def write_status(self, now: int, results: dict, labeled: int) -> dict:
        mark = self._last_mark()
        brier, resolved = self.brier()
        status = {"mode": "paper", "live": False, "armed_paper": self.config.paper_armed,
                  "kill_switch": (self.config.data_dir / "KILL").exists(), "reflex": self.reflex.reflex_id,
                  "reflex_last_call": getattr(self.reflex, "last_call", None), "reflex_fallback": self.config.reflex_fallback,
                  "reflex_calibrated": False, "jev": self.jev_note, "at_us": now,
                  "equity_usd": mark["equity_usd"], "cash_usd": mark["cash_usd"], "high_water_usd": mark["high_water_usd"],
                  "day_open_usd": mark["day_open_usd"], "positions": {k: str(v) for k, v in self.positions().items()},
                  "latches": sorted(self.latched()), "brier": brier, "brier_window_labels": resolved,
                  "brier_directional": self.brier("directional")[0], "brier_neutral": self.brier("neutral")[0],
                  "resolved_labels": self.db.execute("SELECT COUNT(*) FROM labels WHERE label_version=? AND status='resolved'", (LABEL_VERSION,)).fetchone()[0],  # index-only
                  "unresolvable_labels": self.db.execute("SELECT COUNT(*) FROM labels WHERE label_version=? AND status='unresolvable'", (LABEL_VERSION,)).fetchone()[0],
                  "labeled_this_cycle": labeled,
                  "pending_escalations": self.db.execute(self.PENDING_ESCALATIONS_SQL, (now - PENDING_WINDOW_US,)).fetchone()[0],
                  "pending_escalations_window_days": PENDING_WINDOW_US // _DAY_US,
                  # Append-only rowid table: MAX(rowid) equals the row count without a scan.
                  "fills": self.db.execute("SELECT COALESCE(MAX(rowid), 0) FROM fills").fetchone()[0],
                  "dust_usd": {k: str(v) for k, v in self._dust.items()}, "instruments": results}
        path = self.config.data_dir / "status.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2, default=str))
        os.replace(tmp, path)
        return status

    def serve(self, stop: Callable[[], bool], sleep: Callable[[float], None] = time.sleep, offset_s: float = 1.0) -> int:
        """Run one cycle shortly after each candle close until ``stop()``. A failed cycle is logged, never fatal."""
        cycles = attempts = 0
        step = self.config.candle_seconds
        while not stop():
            now_s = self.clock_us() / 1e6
            sleep(max(0.0, step - now_s % step + offset_s) if attempts else 0.0)
            if stop():
                break
            attempts += 1
            try:
                self.cycle()
                cycles += 1
            except Exception as exc:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                print(json.dumps({"cycle_error": type(exc).__name__, "detail": str(exc)[:200], "at_us": self.clock_us()}),
                      file=sys.stderr, flush=True)
        return cycles

    def close(self):
        self.db.close()


def nightly_review(db_path: Path, out_dir: Path, *, now_us: int | None = None, brain=None, fill_cap: int = 10_000,
                   day: str | None = None) -> dict:
    """Review one complete UTC day (default: the day before ``now_us``) and emit a PROPOSAL (never auto-applied).

    The 06:10 UTC timer therefore reviews yesterday in full, not today's first six hours."""
    now_us = now_us if now_us is not None else time.time_ns() // 1000
    day = day if day is not None else _day(now_us - _DAY_US)
    start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) * 1_000_000
    window = (start, start + _DAY_US)
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        fills = [dict(r) for r in db.execute("SELECT * FROM fills WHERE created_us>=? AND created_us<? ORDER BY created_us LIMIT ?",
                                             (*window, fill_cap + 1))]
        decisions = [dict(r) for r in db.execute("SELECT * FROM decisions WHERE created_us>=? AND created_us<? ORDER BY created_us",
                                                 window)]
        # Legacy mid-based labels (no label_version) are never mixed into executable metrics.
        labels = [dict(r) for r in db.execute("SELECT * FROM labels WHERE label_version=? AND labeled_us>=? AND labeled_us<?",
                                              (LABEL_VERSION, *window))]
        escalations = db.execute("SELECT code, COUNT(*) n FROM escalations WHERE created_us>=? AND created_us<? GROUP BY code",
                                 window).fetchall()
    finally:
        db.close()
    truncated = len(fills) > fill_cap
    fills = fills[:fill_cap]
    resolved = [l for l in labels if l["status"] == "resolved"]
    misses = [d for d in decisions if d["gate_open"] and d["action"] == "hold"]
    reasons: dict[str, int] = {}
    for d in decisions:
        for code in json.loads(d["reasons"]):
            reasons[code] = reasons.get(code, 0) + 1
    buckets = []
    for lo in (0.5, 0.6, 0.7, 0.8, 0.9):
        rows = [l for l in resolved if lo <= l["confidence"] < lo + 0.1 or (lo == 0.9 and l["confidence"] == 1.0)]
        if rows:
            buckets.append({"bucket": f"{lo:.1f}-{lo + 0.1:.1f}", "n": len(rows),
                            "mean_confidence": statistics.fmean(l["confidence"] for l in rows),
                            "hit_rate": statistics.fmean(l["hit"] for l in rows)})

    def score(rows):
        return binary_brier(tuple(float(l["confidence"]) for l in rows), tuple(int(l["hit"]) for l in rows)) if rows else None

    directional = [l for l in resolved if l["direction"] in ("long", "short")]
    metrics = {"day": day, "decisions": len(decisions), "decision_ids_sample": [d["decision_id"] for d in decisions[:20]],
               "fills": len(fills), "fills_truncated": truncated, "misses": len(misses), "reason_counts": reasons,
               "resolved_labels": len(resolved), "unresolvable_labels": len(labels) - len(resolved),
               "brier": score(resolved), "brier_directional": score(directional),
               "brier_neutral": score([l for l in resolved if l["direction"] == "neutral"]), "buckets": buckets,
               "escalations": {r["code"]: r["n"] for r in escalations}}
    archive_hash = content_hash(json.dumps(metrics, sort_keys=True, default=str))
    proposal = (brain or DeterministicBrain()).review(job_id="nightly-" + day, archive_hash=archive_hash, metrics=metrics)
    report = {"generated_us": now_us, "archive_hash": archive_hash, "metrics": metrics,
              "fills": fills, "misses": [{k: m[k] for k in ("decision_id", "instrument", "reasons", "confidence")} for m in misses[:fill_cap]],
              "proposal": asdict(proposal), "applied": False,
              "governance": "PROPOSAL ONLY: a schema change ships only through the AgenKit review gate with operator approval."}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"nightly-{day}.json").write_text(json.dumps(report, indent=2, default=str))
    overconfident = [b for b in buckets if b["n"] >= 20 and b["mean_confidence"] - b["hit_rate"] > 0.1]
    lines = [f"# Schema proposal {day} (PROPOSAL ONLY, not applied)", "", f"BRAIN: {proposal.model_id} -> {proposal.action}",
             f"Finding: {proposal.finding}", f"Brier: {metrics['brier']} over {len(resolved)} resolved labels "
             f"(directional {metrics['brier_directional']}, neutral {metrics['brier_neutral']})", ""]
    lines += [f"- Bucket {b['bucket']} overconfident: mean {b['mean_confidence']:.3f} vs hit {b['hit_rate']:.3f}; "
              "consider tightening the direction rubric or the 0.80 entry gate." for b in overconfident] or ["- No bucket shows supported overconfidence."]
    (out_dir / f"schema-proposal-{day}.md").write_text("\n".join(lines) + "\n")
    return report


def _judgment_dict(j: Judgment) -> dict:
    return {"regime": j.regime.value, "direction": j.direction.value, "direction_confidence": j.direction_confidence,
            "toxic_flow": j.toxic_flow, "setup_quality": j.setup_quality, "risk_state": j.risk_state.value,
            "reflex_id": j.reflex_id, "calibrated": j.calibrated, "critical_confidence": j.critical_confidence}


def _day(now_us: int) -> str:
    return datetime.fromtimestamp(now_us / 1e6, tz=timezone.utc).strftime("%Y-%m-%d")


def _public_rules_source(data_dir: Path) -> Callable[[str], dict]:
    from data.call_log import DataCallLog
    from data.public_metrics import fetch_coinbase_product
    log = DataCallLog(data_dir / "public_calls.sqlite3")
    return lambda product: fetch_coinbase_product(log, product)


def _public_book_source(data_dir: Path) -> Callable[[str], dict]:
    from data.call_log import DataCallLog
    from data.public_metrics import fetch_coinbase_book
    log = DataCallLog(data_dir / "public_calls.sqlite3")
    return lambda product: fetch_coinbase_book(log, product, level=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autonomous PAPER runtime. Never sends a real order.")
    parser.add_argument("--paper", action="store_true", required=True, help="required: paper mode is the only mode")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--instruments", default="BTC-USD,ETH-USD")
    parser.add_argument("--data-dir", default="data/autonomous/runtime")
    parser.add_argument("--candle-seconds", type=int, default=60)
    parser.add_argument("--horizon-candles", type=int, default=60)
    parser.add_argument("--explore-fraction", type=float, default=0.0)
    parser.add_argument("--arm-paper", action="store_true", help="local paper flag; cannot arm a live account")
    parser.add_argument("--report", action="store_true", help="run nightly review and exit")
    parser.add_argument("--review-day", help="UTC day YYYY-MM-DD for --report (default: previous complete day)")
    parser.add_argument("--clear-latch", help="operator: clear one latch kind")
    parser.add_argument("--reflex", choices=("offline", "jev"), default="offline")
    parser.add_argument("--reflex-fallback", choices=("none", "offline"), default="none",
                        help="explicit opt-in: judge with the labeled offline reflex when Jev is unavailable")
    parser.add_argument("--jev-model", default=None, help="exact Jev model version (default jev-1.14.0)")
    parser.add_argument("--jev-budget-ms", type=int, default=250)
    parser.add_argument("--write-jev-schema", action="store_true", help="write the proposed Jev schema for operator approval")
    args = parser.parse_args(argv)
    config = RuntimeConfig(tuple(i.strip() for i in args.instruments.split(",") if i.strip()), Path(args.data_dir),
                           args.candle_seconds, args.horizon_candles, explore_fraction=args.explore_fraction,
                           paper_armed=args.arm_paper, reflex_fallback=args.reflex_fallback)
    if args.report:
        print(json.dumps(nightly_review(config.data_dir / "runtime.sqlite3", config.data_dir / "reviews", day=args.review_day)["metrics"], default=str))
        return 0
    reflex = None
    if args.reflex == "jev" or args.write_jev_schema:
        from .jev_reflex import DEFAULT_MODEL, JevReflex, write_proposed_schema
        # The key is read here only; the adapter refuses to send anything without the operator-approved schema file.
        reflex = JevReflex(data_dir=config.data_dir, api_key=os.environ.get("JEV_API_KEY") or None,
                           model=args.jev_model or DEFAULT_MODEL, budget_ms=args.jev_budget_ms,
                           horizon_candles=config.horizon_candles, candle_seconds=config.candle_seconds)
        if args.write_jev_schema:
            path = write_proposed_schema(config.data_dir, reflex.schema)
            print(json.dumps({"proposed": str(path), "schema_hash": reflex.schema_hash,
                              "approve_by": "operator copies it to jev_schema.approved.json"}))
            return 0
    runtime = PaperRuntime(config, reflex=reflex)
    try:
        if args.clear_latch:
            runtime.clear_latch(args.clear_latch)
            print(json.dumps({"cleared": args.clear_latch}))
        elif args.once:
            print(json.dumps(runtime.cycle(), default=str))
        else:
            stop_file = config.data_dir / "STOP"
            runtime.serve(lambda: stop_file.exists())
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
