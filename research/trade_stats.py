"""Statistics for judging a list of R-multiple trades without fooling yourself.

- `clustered_ci`: bootstrap CI that resamples whole days, because signals across coins on
  one day are correlated and a per-trade bootstrap would be too narrow.
- `random_subset_test`: does a filter pick better trades than chance? It compares the
  filtered subset's mean with random subsets of the unfiltered base of the same size.
- `concentration`: how much of the total comes from the best symbol or month.

Trades are any objects with `net_r` plus the attributes the key functions read.
"""
from __future__ import annotations

import random
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Sequence


def utc_day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def mean_r(trades) -> float | None:
    return statistics.fmean(t.net_r for t in trades) if trades else None


def clustered_ci(trades: Sequence, stat: Callable, rng: random.Random, *, day: Callable = lambda t: utc_day(t.entry_ts),
                 reps: int = 2000, level: float = 0.95) -> list[float] | None:
    by_day = defaultdict(list)
    for t in trades:
        by_day[day(t)].append(t)
    days = list(by_day.values())
    if len(days) < 2:
        return None
    draws = []
    for _ in range(reps):
        value = stat([t for _ in days for t in days[rng.randrange(len(days))]])
        if value is not None:
            draws.append(value)
    if not draws:
        return None
    draws.sort()
    tail = (1 - level) / 2
    return [draws[int(tail * len(draws))], draws[int((1 - tail) * len(draws)) - 1]]


def random_subset_test(base: Sequence, subset: Sequence, rng: random.Random, *, reps: int = 20000,
                       ci_reps: int = 2000) -> dict:
    """p = share of random same-size subsets of `base` whose mean R is at least the subset's."""
    if not subset or len(subset) >= len(base):
        return {"n_subset": len(subset)}
    base_r = [t.net_r for t in base]
    target = mean_r(subset)
    hits = sum(statistics.fmean(rng.sample(base_r, len(subset))) >= target for _ in range(reps))
    ids = {id(t) for t in subset}

    def diff(sample):
        sub = [t for t in sample if id(t) in ids]
        return mean_r(sub) - mean_r(sample) if sub else None
    return {"n_subset": len(subset), "diff_mean_r": target - mean_r(base),
            "diff_ci95": clustered_ci(base, diff, rng, reps=ci_reps), "perm_p": (hits + 1) / (reps + 1)}


def concentration(trades: Sequence, keys: dict[str, Callable]) -> dict:
    total = sum(t.net_r for t in trades)
    out = {"total_r": total}
    for name, key in keys.items():
        sums = defaultdict(float)
        for t in trades:
            sums[key(t)] += t.net_r
        top = max(sums.items(), key=lambda kv: kv[1]) if sums else (None, 0.0)
        out[f"top_{name}"] = top[0]
        out[f"top_{name}_share"] = top[1] / total if total > 0 else None
    return out
