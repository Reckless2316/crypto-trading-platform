"""Causal replay of the paper runtime and pre-registered promotion criteria (Task 19).

`AutonomousReplay` re-drives the recorded paper runtime through the SAME decision
code (`PaperRuntime` + `OfflineReflex`) in a throwaway data directory. The
recorded `runtime.sqlite3` is opened read-only. Each recorded cycle becomes one
replay cycle with a fake clock pinned to the recorded cutoff and a book rebuilt
from the recorded best bid/ask and imbalance, so a decision can only see what was
recorded at or before its cutoff.

`EvaluationRunner` then:
  * seals chronological train/validation/holdout splits from snapshot times only
    (never outcomes), purging labels that straddle a boundary and embargoing the
    start of each later split;
  * counts nonoverlapping time blocks (pooled across instruments), not rows, and
    shrinks them by lag-1 autocorrelation into an effective count;
  * reports per-cohort (reflex_id, instrument, regime) calibration, Brier and
    Wilson bounds on train and validation;
  * evaluates the holdout once: paired block-bootstrap Brier difference versus
    the training-frozen base rate, and stressed net returns (fee x1.5, spread x2,
    one-candle latency, and all combined), with a fixed seed and Holm correction;
  * records every holdout look in a register and refuses an overlapping second
    look unless it is explicitly labelled a re-look (which can never promote).

`promotion_eligible` is true only when every pre-registered criterion passes.
Local fixtures prove behaviour, not edge; no evidence is invented.

HANDOFF(claude-opus-5.5 2026-10-18): (1) recorded books are top-of-book only, so
the replayed book carries two levels and spread/latency stress is applied to the
recorded quotes, not to walked depth; (2) only the offline REFLEX can be replayed
(recorded Jev judgments are compared, not regenerated); (3) the plan's gap,
delist, private-outage, cancellation and correlated-selloff stress scenarios,
calibration/release/nightly manifest integration and the `data/backtesting.py`
placement are not done; (4) the runtime's horizon/lookback are not stored in
its meta, so the replay assumes the runtime defaults unless told otherwise and
the decision/label fidelity check is what catches a mismatch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Callable, Mapping, Sequence

from .contracts import content_hash
from .runtime import LABEL_VERSION, OfflineReflex, PaperRuntime, RuntimeConfig, Unavailable

REPORT_VERSION = "replay-report-v1"
SPLITS = ("train", "validation", "holdout")
_HOUR_US = 3_600_000_000
_DAY_US = 86_400_000_000
_CODE_FILES = ("replay.py", "runtime.py", "state.py", "policy.py", "sizing.py", "risk.py", "calibration.py")


class HoldoutReused(RuntimeError):
    """A second look at an already-evaluated holdout without an explicit re-look label."""


def minimum_holdout_blocks(delta: float = .05, alpha: float = .05, power: float = .80) -> int:
    """Normal-approximation Bernoulli design check (plan Task 19); not a profit power guarantee."""
    if not all(math.isfinite(x) for x in (delta, alpha, power)):
        raise ValueError("nonfinite power input")
    if not 0 < delta < 1 or not 0 < alpha < .5 or not .5 < power < 1:
        raise ValueError("invalid power input")
    normal = NormalDist()
    z = normal.inv_cdf(1 - alpha / 2) + normal.inv_cdf(power)
    return max(500, math.ceil(z * z * .25 / (delta * delta)))


@dataclass(frozen=True)
class Criteria:
    """Pre-registered before any holdout is unsealed; hashed into every report."""
    min_train_blocks: int = 1_000
    min_validation_blocks: int = 400
    min_holdout_blocks: int = max(800, minimum_holdout_blocks())
    block_floor: int = 200  # absolute floor; the defaults above are stricter
    block_us: int = 4 * _HOUR_US
    embargo_us: int = 4 * _HOUR_US  # the effective embargo is max(this, label horizon)
    fractions: tuple[float, float, float] = (0.6, 0.2, 0.2)
    alpha: float = 0.05
    max_day_concentration: float = 0.30
    fee_multiplier: float = 1.5
    spread_multiplier: float = 2.0
    latency_candles: int = 1
    bootstrap: int = 10_000
    seed: int = 20_261_018

    def __post_init__(self):
        if (min(self.min_train_blocks, self.min_validation_blocks, self.min_holdout_blocks) < self.block_floor
                or self.block_floor < 1 or self.block_us <= 0 or self.embargo_us < 0
                or len(self.fractions) != 3 or any(f <= 0 for f in self.fractions)
                or abs(sum(self.fractions) - 1) > 1e-9 or not 0 < self.alpha < .5
                or not 0 < self.max_day_concentration <= 1 or self.fee_multiplier < 1
                or self.spread_multiplier < 1 or self.latency_candles < 0 or self.bootstrap < 100):
            raise ValueError("criteria may only be at least as strict as the registered floors")

    @property
    def criteria_hash(self) -> str:
        return content_hash(asdict(self))


@dataclass(frozen=True)
class Sample:
    """One resolved, replayed decision label."""
    decision_id: str
    instrument: str
    close_us: int
    reflex_id: str
    regime: str
    direction: str
    confidence: float
    hit: int
    gate_open: bool
    exec_return: float | None


@dataclass(frozen=True)
class ReplayResult:
    source: str
    source_fingerprint: str
    as_of_us: int
    step_us: int
    horizon_us: int
    fee_rate: float
    cycles: int
    decision_errors: int
    samples: tuple[Sample, ...]
    prices: Mapping[tuple[str, int], tuple[float, float]]  # (instrument, close) -> (bid, ask)
    snapshot_closes: tuple[int, ...]
    fidelity: dict


# ---------------------------------------------------------------- replay

def open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _iso_us(us: int) -> str:
    whole, micro = divmod(us, 1_000_000)
    return datetime.fromtimestamp(whole, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{micro:06d}Z"


def recorded_book(best_bid: float, best_ask: float, imbalance: float, event_us: int) -> dict:
    """Two-level book that reproduces the recorded top, mid, spread and imbalance."""
    imbalance = max(-0.999999, min(0.999999, float(imbalance)))
    return {"bids": [[best_bid, 1 + imbalance]], "asks": [[best_ask, 1 - imbalance]],
            "exchange_time": _iso_us(event_us)}


def _replay_rules(instrument: str) -> dict:
    # ASSUMPTION (reported): conservative public increments; recorded rule fetches are not replayed.
    return {"id": instrument, "base_increment": "0.00000001", "quote_increment": "0.01", "min_market_funds": "1",
            "status": "online", "trading_disabled": False, "post_only": False, "limit_only": False,
            "cancel_only": False}


class AutonomousReplay:
    """Replay recorded cycles through PaperRuntime in a throwaway directory."""

    def __init__(self, data_dir: Path, *, as_of_us: int | None = None, config_overrides: Mapping | None = None):
        self.data_dir = Path(data_dir)
        self.source = self.data_dir / "runtime.sqlite3"
        if not self.source.exists():
            raise FileNotFoundError(self.source)
        self.as_of_us = as_of_us
        self.config_overrides = dict(config_overrides or {})

    def _load(self):
        with open_read_only(self.source) as db:
            meta = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM meta")}
            as_of = self.as_of_us
            if as_of is None:
                as_of = db.execute("SELECT MAX(cutoff_us) FROM snapshots").fetchone()[0] or 0
            rows = db.execute("SELECT instrument, candle_close_us, event_us, cutoff_us, mid, best_bid, best_ask, wire, "
                              "snapshot_hash FROM snapshots WHERE cutoff_us<=? ORDER BY cutoff_us, instrument",
                              (as_of,)).fetchall()
            decisions = {r["decision_id"]: dict(r) for r in db.execute(
                "SELECT decision_id, regime, direction, confidence, gate_open, quality, candle_close_us FROM decisions "
                "WHERE created_us<=? AND phase='live'", (as_of,))}
            labels = {r["decision_id"]: dict(r) for r in db.execute(
                "SELECT decision_id, status, hit, exec_return FROM labels WHERE labeled_us<=? AND label_version=?",
                (as_of, LABEL_VERSION))}
        return meta, as_of, [dict(r) for r in rows], decisions, labels

    def run(self) -> ReplayResult:
        meta, as_of, rows, recorded_decisions, recorded_labels = self._load()
        instruments = tuple(sorted({r["instrument"] for r in rows})) or ("BTC-USD",)
        settings = dict(instruments=instruments)
        if "candle_seconds" in meta:
            settings["candle_seconds"] = int(meta["candle_seconds"])
        if "starting_cash_usd" in meta:
            from decimal import Decimal
            settings["starting_cash_usd"] = Decimal(meta["starting_cash_usd"])
        settings.update(self.config_overrides)
        cycles: dict[int, dict[str, dict]] = {}
        for row in rows:
            cycles.setdefault(row["cutoff_us"], {})[row["instrument"]] = row
        prices = {(r["instrument"], r["candle_close_us"]): (r["best_bid"], r["best_ask"]) for r in rows}
        fingerprint = content_hash([as_of, [r["snapshot_hash"] for r in rows]])
        clock = {"now": min(cycles) if cycles else 0}
        current: dict[str, dict] = {}

        def book_source(instrument: str) -> dict:
            row = current.get(instrument)
            if row is None:
                raise Unavailable("NOT_RECORDED")
            imbalance = json.loads(row["wire"]).get("imb", 0.0)
            return recorded_book(row["best_bid"], row["best_ask"], imbalance, row["event_us"])

        errors = 0
        with tempfile.TemporaryDirectory(prefix="surge-replay-") as work:
            config = RuntimeConfig(data_dir=Path(work), **settings)
            runtime = PaperRuntime(config, book_source=book_source, clock_us=lambda: clock["now"],
                                   reflex=OfflineReflex(), rules_source=_replay_rules)
            try:
                for cutoff in sorted(cycles):
                    clock["now"] = cutoff
                    current.clear()
                    current.update(cycles[cutoff])
                    status = runtime.cycle()
                    errors += sum(1 for r in (status.get("instruments") or {}).values()
                                  if any(str(x).startswith("DECISION_ERROR") for x in r.get("reasons", ())))
                db = runtime.db
                replayed = {r["decision_id"]: dict(r) for r in db.execute(
                    "SELECT decision_id, instrument, candle_close_us, reflex_id, regime, direction, confidence, "
                    "gate_open, quality FROM decisions WHERE phase='live'")}
                samples = tuple(Sample(r["decision_id"], r["instrument"], r["candle_close_us"], r["reflex_id"],
                                       r["regime"], r["direction"], float(r["confidence"]), int(r["hit"]),
                                       bool(r["gate_open"]), r["exec_return"])
                                for r in db.execute(
                    "SELECT d.decision_id, d.instrument, d.candle_close_us, d.reflex_id, d.regime, d.direction, "
                    "l.confidence, l.hit, d.gate_open, l.exec_return FROM decisions d JOIN labels l USING(decision_id) "
                    "WHERE d.phase='live' AND l.status='resolved' AND l.label_version=? "
                    "ORDER BY d.candle_close_us, d.instrument", (LABEL_VERSION,)))
                replay_labels = {r["decision_id"]: dict(r) for r in db.execute(
                    "SELECT decision_id, status, hit, exec_return FROM labels")}
            finally:
                runtime.close()
        fidelity = _fidelity(recorded_decisions, replayed, recorded_labels, replay_labels)
        step = config.candle_seconds * 1_000_000
        return ReplayResult(str(self.source), fingerprint, as_of, step, config.horizon_candles * step,
                            float(config.fee_rate), len(cycles), errors, samples, prices,
                            tuple(sorted({r["candle_close_us"] for r in rows})), fidelity)


def _fidelity(recorded: dict, replayed: dict, recorded_labels: dict, replay_labels: dict) -> dict:
    fields = ("regime", "direction", "confidence", "gate_open", "quality")
    common = sorted(set(recorded) & set(replayed))
    mismatched = [d for d in common if any(recorded[d][f] != replayed[d][f] for f in fields)]
    label_common = sorted(set(recorded_labels) & set(replay_labels))
    label_mismatched = [d for d in label_common
                        if (recorded_labels[d]["status"], recorded_labels[d]["hit"]) != (replay_labels[d]["status"], replay_labels[d]["hit"])
                        or not _close(recorded_labels[d]["exec_return"], replay_labels[d]["exec_return"])]
    return {"recorded_decisions": len(recorded), "replayed_decisions": len(replayed), "compared": len(common),
            "mismatched": len(mismatched), "only_recorded": len(set(recorded) - set(replayed)),
            "only_replayed": len(set(replayed) - set(recorded)), "labels_compared": len(label_common),
            "labels_mismatched": len(label_mismatched), "mismatch_examples": mismatched[:5] + label_mismatched[:5],
            "exact": not mismatched and not label_mismatched and set(recorded) <= set(replayed)}


def _close(a, b) -> bool:
    return (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= 1e-12 * max(1.0, abs(a)))


# ---------------------------------------------------------------- splits and blocks

@dataclass(frozen=True)
class SplitPlan:
    start_us: int
    boundaries: tuple[int, int]  # validation start, holdout start
    end_us: int  # exclusive
    horizon_us: int
    embargo_us: int
    seal: str

    def window(self, name: str) -> tuple[int, int]:
        edges = (self.start_us, *self.boundaries, self.end_us)
        index = SPLITS.index(name)
        return edges[index], edges[index + 1]


def seal_splits(snapshot_closes: Sequence[int], *, horizon_us: int, criteria: Criteria, fingerprint: str) -> SplitPlan:
    """Chronological boundaries from snapshot times only; outcomes never move a boundary."""
    if not snapshot_closes:
        raise ValueError("no recorded snapshots")
    start, end = min(snapshot_closes), max(snapshot_closes) + 1
    span = end - start
    first = start + int(span * criteria.fractions[0])
    second = start + int(span * (criteria.fractions[0] + criteria.fractions[1]))
    embargo = max(criteria.embargo_us, horizon_us)
    seal = content_hash(["split-seal-v1", fingerprint, start, first, second, end, horizon_us, embargo,
                         criteria.criteria_hash])
    return SplitPlan(start, (first, second), end, horizon_us, embargo, seal)


def assign_splits(samples: Sequence[Sample], plan: SplitPlan) -> tuple[dict[str, list[Sample]], dict]:
    """Purge labels whose horizon crosses into the next split; embargo each later split's start."""
    out = {name: [] for name in SPLITS}
    dropped = {name: {"purged": 0, "embargoed": 0} for name in SPLITS}
    for sample in samples:
        for index, name in enumerate(SPLITS):
            lo, hi = plan.window(name)
            if not lo <= sample.close_us < hi:
                continue
            if index > 0 and sample.close_us < lo + plan.embargo_us:
                dropped[name]["embargoed"] += 1
            elif index < len(SPLITS) - 1 and sample.close_us + plan.horizon_us >= hi:
                dropped[name]["purged"] += 1
            else:
                out[name].append(sample)
            break
    return out, dropped


def block_of(close_us: int, block_us: int) -> int:
    return close_us // block_us


def block_stats(samples: Sequence[Sample], block_us: int) -> dict:
    """Independent units are nonoverlapping time blocks pooled across instruments."""
    blocks: dict[int, list[Sample]] = {}
    for sample in samples:
        blocks.setdefault(block_of(sample.close_us, block_us), []).append(sample)
    ordered = [blocks[k] for k in sorted(blocks)]
    means = [sum(s.hit for s in b) / len(b) for b in ordered]
    rho = _lag1(means)
    n = len(ordered)
    effective = n if rho is None or rho <= 0 else n * (1 - rho) / (1 + rho)
    return {"rows": len(samples), "blocks": n, "lag1_autocorrelation": rho,
            "effective_blocks": math.floor(effective)}


def _lag1(values: Sequence[float]) -> float | None:
    if len(values) < 3:
        return None
    mean = sum(values) / len(values)
    denominator = sum((v - mean) ** 2 for v in values)
    if denominator == 0:
        return None
    return sum((a - mean) * (b - mean) for a, b in zip(values, values[1:])) / denominator


# ---------------------------------------------------------------- calibration

def wilson_interval(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def brier(samples: Sequence[Sample]) -> float | None:
    return sum((s.confidence - s.hit) ** 2 for s in samples) / len(samples) if samples else None


def cohort_calibration(samples: Sequence[Sample]) -> list[dict]:
    cohorts: dict[tuple[str, str, str], list[Sample]] = {}
    for sample in samples:
        cohorts.setdefault((sample.reflex_id, sample.instrument, sample.regime), []).append(sample)
    out = []
    for (reflex_id, instrument, regime), rows in sorted(cohorts.items()):
        hits = sum(s.hit for s in rows)
        interval = wilson_interval(hits, len(rows))
        mean_conf = sum(s.confidence for s in rows) / len(rows)
        out.append({"reflex_id": reflex_id, "instrument": instrument, "regime": regime, "n": len(rows),
                    "hit_rate": hits / len(rows), "mean_confidence": mean_conf,
                    "wilson_95": list(interval), "brier": brier(rows),
                    "calibrated_within_wilson": interval[0] <= mean_conf <= interval[1]})
    return out


# ---------------------------------------------------------------- stress and bootstrap

@dataclass(frozen=True)
class Stress:
    name: str
    fee_multiplier: float = 1.0
    spread_multiplier: float = 1.0
    latency_candles: int = 0


def stress_scenarios(criteria: Criteria) -> tuple[Stress, ...]:
    return (Stress("baseline"), Stress("fee_x%g" % criteria.fee_multiplier, fee_multiplier=criteria.fee_multiplier),
            Stress("spread_x%g" % criteria.spread_multiplier, spread_multiplier=criteria.spread_multiplier),
            Stress("latency_%d_candle" % criteria.latency_candles, latency_candles=criteria.latency_candles),
            Stress("combined", criteria.fee_multiplier, criteria.spread_multiplier, criteria.latency_candles))


def stressed_return(sample: Sample, prices: Mapping[tuple[str, int], tuple[float, float]], *, stress: Stress,
                    fee_rate: float, step_us: int, horizon_us: int) -> float | None:
    """Long entry at the (delayed, widened) ask and exit at the horizon bid, net of stressed round-trip fees.

    The baseline equals the runtime's executable label. None when a needed quote was not recorded (a nonfill).
    """
    entry_close = sample.close_us + stress.latency_candles * step_us
    entry, exit_ = prices.get((sample.instrument, entry_close)), prices.get((sample.instrument, entry_close + horizon_us))
    if entry is None or exit_ is None:
        return None

    def widen(quote):
        bid, ask = quote
        mid = (bid + ask) / 2
        return mid - stress.spread_multiplier * (mid - bid), mid + stress.spread_multiplier * (ask - mid)

    ask = widen(entry)[1]
    bid = widen(exit_)[0]
    return bid / ask - 1 - 2 * fee_rate * stress.fee_multiplier


def block_bootstrap(values: Sequence[tuple[int, float]], *, replications: int, seed: int) -> list[float]:
    """Resample whole blocks with replacement; returns the mean of each replication (deterministic)."""
    blocks: dict[int, list[float]] = {}
    for block, value in values:
        blocks.setdefault(block, []).append(value)
    keys = sorted(blocks)
    if len(keys) < 2:
        return []
    rng = random.Random(seed)
    means = []
    for _ in range(replications):
        total = count = 0
        for _ in keys:
            chosen = blocks[keys[rng.randrange(len(keys))]]
            total += sum(chosen)
            count += len(chosen)
        means.append(total / count)
    return means


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[position]


def holm(pvalues: Mapping[str, float | None], alpha: float) -> dict[str, bool]:
    """Holm step-down; a missing p-value is never rejected."""
    decided = {name: False for name in pvalues}
    tested = sorted((p, name) for name, p in pvalues.items() if p is not None)
    m = len(pvalues)
    for rank, (p, name) in enumerate(tested):
        if p > alpha / (m - rank):
            break
        decided[name] = True
    return decided


def day_concentration(values: Sequence[tuple[int, float]]) -> float | None:
    days: dict[int, float] = {}
    for close_us, value in values:
        days[close_us // _DAY_US] = days.get(close_us // _DAY_US, 0.0) + value
    total = sum(abs(v) for v in days.values())
    return max(abs(v) for v in days.values()) / total if total > 0 else None


# ---------------------------------------------------------------- holdout register

class HoldoutRegister:
    """Append-only JSON record of every holdout look (identity, window, evaluation hash)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict) or not isinstance(value.get("looks"), list):
            raise ValueError("holdout register unreadable")
        return value["looks"]

    def record(self, entry: dict, *, relook: str | None) -> dict:
        looks = self.entries()
        lo, hi = entry["window_us"]
        prior = [e for e in looks if e["window_us"][0] < hi and lo < e["window_us"][1]
                 and set(e["instruments"]) & set(entry["instruments"])]
        if prior and not relook:
            raise HoldoutReused("holdout overlaps evaluation(s) " + ", ".join(e["evaluation_hash"][:12] for e in prior))
        entry = dict(entry, relook=relook or None, prior_looks=[e["evaluation_hash"] for e in prior])
        looks.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps({"version": 1, "looks": looks}, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.path)
        return entry


# ---------------------------------------------------------------- evaluation

def code_hash() -> str:
    here = Path(__file__).resolve().parent
    return content_hash([[name, hashlib.sha256((here / name).read_bytes()).hexdigest()] for name in _CODE_FILES])


@dataclass
class EvaluationRunner:
    criteria: Criteria = field(default_factory=Criteria)

    def _prepare(self, replay: ReplayResult):
        c = self.criteria
        plan = seal_splits(replay.snapshot_closes, horizon_us=replay.horizon_us, criteria=c,
                           fingerprint=replay.source_fingerprint)
        splits, dropped = assign_splits(replay.samples, plan)
        split_report = {}
        for name in SPLITS:
            lo, hi = plan.window(name)
            split_report[name] = {"window_us": [lo, hi], **dropped[name], **block_stats(splits[name], c.block_us)}
        return plan, splits, split_report

    def diagnose(self, replay: ReplayResult) -> dict:
        """Nightly diagnostics: splits and train/validation calibration only. The holdout is never computed or
        registered, so a diagnostic run cannot spend it; it can never be promotion eligible."""
        plan, splits, split_report = self._prepare(replay)
        return {"version": REPORT_VERSION, "mode": "diagnostic",
                "manifest": {"source": replay.source, "source_fingerprint": replay.source_fingerprint,
                             "as_of_us": replay.as_of_us, "code_hash": code_hash(),
                             "criteria_hash": self.criteria.criteria_hash, "seal": plan.seal},
                "replay": {"cycles": replay.cycles, "resolved_samples": len(replay.samples),
                           "decision_errors": replay.decision_errors, "fidelity": replay.fidelity},
                "splits": {name: {k: v for k, v in split.items()} for name, split in split_report.items()
                           if name != "holdout"},
                "calibration": {name: cohort_calibration(splits[name]) for name in ("train", "validation")},
                "holdout": None, "criteria_results": {}, "promotion_eligible": False,
                "ineligible_reasons": ["holdout_not_evaluated"]}

    def evaluate(self, replay: ReplayResult, *, register: HoldoutRegister, relook: str | None = None) -> dict:
        c = self.criteria
        plan, splits, split_report = self._prepare(replay)
        manifest = {"source": replay.source, "source_fingerprint": replay.source_fingerprint,
                    "as_of_us": replay.as_of_us, "code_hash": code_hash(), "criteria": asdict(c),
                    "criteria_hash": c.criteria_hash, "seal": plan.seal,
                    "bootstrap": {"replications": c.bootstrap, "seed": c.seed, "block_us": c.block_us,
                                  "method": "nonoverlapping time-block bootstrap, blocks resampled with replacement"},
                    "assumptions": ["replayed book = recorded best bid/ask with recorded imbalance (two levels)",
                                    "product rules = fixed conservative public increments",
                                    "OfflineReflex regenerated; stress applied to recorded top-of-book quotes",
                                    "trades = gate-open long decisions (the only entries the runtime can make)"]}
        holdout_lo, holdout_hi = plan.window("holdout")
        instruments = sorted({i for i, _ in replay.prices})
        holdout_identity = content_hash(["holdout-v1", replay.source_fingerprint, plan.seal, holdout_lo, holdout_hi,
                                         instruments])
        evaluation_hash = content_hash(["holdout-evaluation-v1", holdout_identity, c.criteria_hash,
                                        manifest["code_hash"], c.seed, c.bootstrap])
        # Exposure is recorded BEFORE any holdout outcome is computed.
        look = register.record({"evaluation_hash": evaluation_hash, "holdout_identity": holdout_identity,
                                "window_us": [holdout_lo, holdout_hi], "instruments": instruments,
                                "seal": plan.seal, "as_of_us": replay.as_of_us}, relook=relook)
        holdout = self._holdout(splits["train"], splits["holdout"], replay)
        holdout.update(evaluation_hash=evaluation_hash, holdout_identity=holdout_identity,
                       relook=look["relook"], prior_looks=look["prior_looks"])
        results = self._criteria(split_report, holdout, replay, unseen=not look["prior_looks"])
        eligible = all(r["pass"] for r in results.values())
        return {"version": REPORT_VERSION, "manifest": manifest,
                "replay": {"cycles": replay.cycles, "resolved_samples": len(replay.samples),
                           "decision_errors": replay.decision_errors, "fidelity": replay.fidelity},
                "splits": split_report,
                "calibration": {name: cohort_calibration(splits[name]) for name in ("train", "validation")},
                "holdout": holdout, "criteria_results": results, "promotion_eligible": eligible,
                "ineligible_reasons": sorted(name for name, r in results.items() if not r["pass"])}

    def _holdout(self, train: Sequence[Sample], holdout: Sequence[Sample], replay: ReplayResult) -> dict:
        c = self.criteria
        base_rate = sum(s.hit for s in train) / len(train) if train else None
        out: dict = {"samples": len(holdout), "base_rate_from_train": base_rate, "brier": brier(holdout),
                     "base_rate_brier": None, "brier_difference_upper_95": None, "p_brier": None,
                     "cohorts": cohort_calibration(holdout), "stress": {}}
        if base_rate is not None and holdout:
            out["base_rate_brier"] = sum((base_rate - s.hit) ** 2 for s in holdout) / len(holdout)
            diffs = [(block_of(s.close_us, c.block_us), (s.confidence - s.hit) ** 2 - (base_rate - s.hit) ** 2)
                     for s in holdout]
            boot = block_bootstrap(diffs, replications=c.bootstrap, seed=c.seed)
            if boot:
                out["brier_difference_upper_95"] = _quantile(boot, 0.95)
                out["p_brier"] = sum(1 for x in boot if x >= 0) / len(boot)
        trades = [s for s in holdout if s.gate_open and s.direction == "long"]
        for stress in stress_scenarios(c):
            values = []
            unresolved = 0
            for s in trades:
                r = stressed_return(s, replay.prices, stress=stress, fee_rate=replay.fee_rate,
                                    step_us=replay.step_us, horizon_us=replay.horizon_us)
                if r is None:
                    unresolved += 1
                else:
                    values.append((s.close_us, r))
            boot = block_bootstrap([(block_of(t, c.block_us), v) for t, v in values],
                                   replications=c.bootstrap, seed=c.seed)
            out["stress"][stress.name] = {
                "trades": len(trades), "unresolved_nonfills": unresolved,
                "mean_net_return": sum(v for _, v in values) / len(values) if values else None,
                "lower_95": _quantile(boot, 0.05), "upper_95": _quantile(boot, 0.95),
                "p_not_positive": (sum(1 for x in boot if x <= 0) / len(boot)) if boot else None,
                "day_concentration": day_concentration(values)}
        return out

    def _criteria(self, splits: dict, holdout: dict, replay: ReplayResult, *, unseen: bool) -> dict:
        c = self.criteria
        combined = holdout["stress"]["combined"]
        decisions = holm({"brier_beats_base_rate": holdout["p_brier"], "stressed_net_positive": combined["p_not_positive"]},
                         c.alpha)

        def result(ok, value, threshold):
            return {"pass": bool(ok), "value": value, "threshold": threshold}

        brier_upper = holdout["brier_difference_upper_95"]
        lower = combined["lower_95"]
        concentration = combined["day_concentration"]
        return {
            "train_effective_blocks": result(splits["train"]["effective_blocks"] >= c.min_train_blocks,
                                             splits["train"]["effective_blocks"], c.min_train_blocks),
            "validation_effective_blocks": result(splits["validation"]["effective_blocks"] >= c.min_validation_blocks,
                                                  splits["validation"]["effective_blocks"], c.min_validation_blocks),
            "holdout_effective_blocks": result(splits["holdout"]["effective_blocks"] >= c.min_holdout_blocks,
                                               splits["holdout"]["effective_blocks"], c.min_holdout_blocks),
            "brier_beats_base_rate": result(brier_upper is not None and brier_upper < 0
                                            and decisions["brier_beats_base_rate"], brier_upper, "<0 (95% upper, Holm)"),
            "stressed_net_lower_bound_positive": result(lower is not None and lower > 0
                                                        and decisions["stressed_net_positive"], lower, ">0 (95% lower, Holm)"),
            "day_concentration": result(concentration is not None and concentration <= c.max_day_concentration,
                                        concentration, c.max_day_concentration),
            "holdout_unseen": result(unseen, unseen, True),
            "replay_fidelity_exact": result(replay.fidelity.get("exact") is True, replay.fidelity.get("mismatched"), 0),
            "no_decision_errors": result(replay.decision_errors == 0, replay.decision_errors, 0),
        }


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.autonomous.replay",
                                     description="Read-only causal replay of the paper runtime with promotion criteria.")
    parser.add_argument("--data-dir", type=Path, default=Path("data/autonomous/runtime"))
    parser.add_argument("--out", type=Path, default=None, help="report path (default: stdout)")
    parser.add_argument("--register", type=Path, default=Path("docs/reviews/holdout-register.json"))
    parser.add_argument("--relook", default=None, help="explicit label for a second look at a seen holdout")
    parser.add_argument("--as-of-us", type=int, default=None)
    parser.add_argument("--bootstrap", type=int, default=Criteria.bootstrap)
    parser.add_argument("--seed", type=int, default=Criteria.seed)
    args = parser.parse_args(argv)
    replay = AutonomousReplay(args.data_dir, as_of_us=args.as_of_us).run()
    runner = EvaluationRunner(Criteria(bootstrap=args.bootstrap, seed=args.seed))
    try:
        report = runner.evaluate(replay, register=HoldoutRegister(args.register), relook=args.relook)
    except HoldoutReused as exc:
        print(f"refused: {exc}; pass --relook LABEL for an explicit, non-promotable re-look", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
