"""Run the pre-registered four-way ablation and write results.json plus a summary table.

    python3 -m research.fade_flush.ablation              # base costs and 2x-slippage stress
    python3 -m research.fade_flush.ablation --symbols BTCUSDT,ETHUSDT --out /tmp/x

Decision rules are in docs/research/2026-10-25-fade-forced-flush/PREREG.md and are applied
mechanically in `decide`; nothing here is tuned.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from data.binance_archive import DEFAULT_CACHE, SYMBOLS, load
from research.trade_stats import clustered_ci, concentration as _concentration, mean_r as _mean_r, random_subset_test
from .strategy import Params, Trade, run_symbol

OOS_START = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
OUT = Path(__file__).resolve().parents[2] / "docs" / "research" / "2026-10-25-fade-forced-flush"
VARIANTS = {
    "a_sweep": lambda t: True,
    "b_flush": lambda t: t.flush,
    "c_crowd": lambda t: t.crowd,
    "d_both": lambda t: t.flush and t.crowd,
}
RISK_PER_MOMENT = 0.005
BOOT = 2000
PERM = 20000


def _run(job):
    symbol, cache, slip_mult = job
    trades, rejected, n_signals = run_symbol(load(symbol, Path(cache)), replace(Params(), slip_mult=slip_mult))
    return symbol, [t.to_dict() for t in trades], rejected, n_signals


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")


def _month(ts: int) -> str:
    return _day(ts)[:7]


def summarize(trades: list[Trade], rng: random.Random) -> dict:
    if not trades:
        return {"n": 0}
    r = [t.net_r for t in trades]
    wins = [x for x in r if x > 0]
    losses = [-x for x in r if x <= 0]
    exits = defaultdict(int)
    for t in trades:
        exits[t.exit_reason] += 1
    return {
        "n": len(r), "mean_r": statistics.fmean(r), "median_r": statistics.median(r),
        "mean_r_ci95": clustered_ci(trades, _mean_r, rng, reps=BOOT), "total_r": sum(r),
        "win_rate": len(wins) / len(r),
        "profit_factor": sum(wins) / sum(losses) if losses and sum(losses) else None,
        "mean_gross_r": statistics.fmean(t.gross_r for t in trades),
        "mean_fee_r": statistics.fmean(t.fee_r for t in trades),
        "mean_funding_r": statistics.fmean(t.funding_r for t in trades),
        "mean_target_r": statistics.fmean(t.target_r for t in trades),
        "exits": dict(exits),
        "longs": sum(t.side == "long" for t in trades),
    }


def breakdown(trades: list[Trade], key) -> dict:
    groups = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t.net_r)
    return {k: {"n": len(v), "mean_r": statistics.fmean(v), "total_r": sum(v)} for k, v in sorted(groups.items())}


def concentration(trades: list[Trade]) -> dict:
    return _concentration(trades, {"symbol": lambda t: t.symbol, "month": lambda t: _month(t.entry_ts)})


def filter_test(base: list[Trade], subset: list[Trade], rng: random.Random) -> dict:
    """Is the filtered subset better than random subsets of the base of the same size?"""
    return random_subset_test(base, subset, rng, reps=PERM, ci_reps=BOOT)


def portfolio(trades: list[Trade]) -> dict:
    """Per-symbol blocking; 0.5% risk per entry moment split across the trades opened in it."""
    trades = sorted(trades, key=lambda t: (t.entry_ts, t.symbol))
    busy_until: dict[str, int] = {}
    taken = []
    i = 0
    while i < len(trades):
        moment = [t for t in trades[i:] if t.entry_ts == trades[i].entry_ts]
        i += len(moment)
        accepted = [t for t in moment if busy_until.get(t.symbol, 0) <= t.entry_ts]
        for t in accepted:
            busy_until[t.symbol] = t.exit_ts
            taken.append((t, RISK_PER_MOMENT / len(accepted)))
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for t, risk in sorted(taken, key=lambda x: x[0].exit_ts):
        equity *= 1 + risk * t.net_r
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
    years = (max(t.exit_ts for t, _ in taken) - min(t.entry_ts for t, _ in taken)) / 3.156e10 if taken else 0
    return {"trades": len(taken), "final_equity": equity, "max_drawdown": max_dd,
            "cagr": equity ** (1 / years) - 1 if years > 0.25 else None}


def decide(oos: dict) -> dict:
    d = oos["variants"]["d_both"]
    conc = oos["concentration_d"]
    reasons = []
    if d["n"] == 0 or d["mean_r"] < 0.15:
        reasons.append(f"expectancy {d.get('mean_r', 0):+.3f}R < +0.15R")
    if d["n"] < 200:
        reasons.append(f"only {d['n']} trades (< 200)")
    for name in ("symbol", "month"):
        share = conc.get(f"top_{name}_share")
        if share is not None and share > 0.5:
            reasons.append(f"{share:.0%} of net R from one {name} ({conc[f'top_{name}']})")
    ft = oos["filter_tests"]["d_both"]
    filters_earn = ft.get("diff_mean_r", 0) > 0 and ft.get("perm_p", 1) < 0.05
    return {"kill": bool(reasons), "kill_reasons": reasons, "filters_earn_their_place": filters_earn}


def analyze(trades: list[Trade], seed: int = 0) -> dict:
    rng = random.Random(seed)
    eligible = [t for t in trades if t.flush is not None and t.crowd is not None]
    out = {"trades_total": len(trades), "trades_eligible": len(eligible)}
    for sample, keep in (("in_sample", lambda t: t.entry_ts < OOS_START), ("out_of_sample", lambda t: t.entry_ts >= OOS_START)):
        part = [t for t in eligible if keep(t)]
        sets = {name: [t for t in part if f(t)] for name, f in VARIANTS.items()}
        out[sample] = {
            "variants": {name: summarize(s, rng) for name, s in sets.items()},
            "filter_tests": {name: filter_test(sets["a_sweep"], s, rng) for name, s in sets.items() if name != "a_sweep"},
            "concentration_d": concentration(sets["d_both"]),
            "portfolio": {name: portfolio(s) for name, s in sets.items()},
            "by_side": {name: breakdown(s, lambda t: t.side) for name, s in sets.items()},
            "by_symbol": {name: breakdown(s, lambda t: t.symbol) for name, s in sets.items()},
            "by_year": {name: breakdown(s, lambda t: _day(t.entry_ts)[:4]) for name, s in sets.items()},
        }
    out["decision"] = decide(out["out_of_sample"])
    return out


def run(symbols, cache: Path, slip_mult: float, workers: int):
    with ProcessPoolExecutor(workers) as pool:
        results = list(pool.map(_run, [(s, str(cache), slip_mult) for s in symbols]))
    trades, rejected, signals = [], defaultdict(int), 0
    for _, rows, rej, n in results:
        trades += [Trade(**row) for row in rows]
        signals += n
        for k, v in rej.items():
            rejected[k] += v
    return trades, dict(rejected), signals


def _fmt(x, spec="+.3f"):
    return "—" if x is None else format(x, spec)


def table(res: dict) -> str:
    lines = []
    for sample in ("in_sample", "out_of_sample"):
        s = res[sample]
        lines += [f"\n**{sample.replace('_', '-')}**\n",
                  "| variant | n | win | mean R | 95% CI | total R | PF | vs (a) | perm p | port. maxDD | port. CAGR |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for name, v in s["variants"].items():
            if not v["n"]:
                lines.append(f"| {name} | 0 | | | | | | | | | |")
                continue
            ft = s["filter_tests"].get(name, {})
            ci = v["mean_r_ci95"]
            p = s["portfolio"][name]
            lines.append(f"| {name} | {v['n']} | {v['win_rate']:.0%} | {v['mean_r']:+.3f} | "
                         f"{'—' if not ci else f'{ci[0]:+.3f} … {ci[1]:+.3f}'} | {v['total_r']:+.1f} | "
                         f"{_fmt(v['profit_factor'], '.2f')} | {_fmt(ft.get('diff_mean_r'))} | {_fmt(ft.get('perm_p'), '.3f')} | "
                         f"{p['max_drawdown']:.1%} | {_fmt(p['cagr'], '+.1%')} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbols": args.symbols.split(",")}
    md = ["# Fade the forced flush: ablation results", "", "Generated by `python3 -m research.fade_flush.ablation`. "
          "Rules: PREREG.md. R is net of fees, slippage and funding."]
    for label, slip in (("base", 1.0), ("stress_2x_slippage", 2.0)):
        trades, rejected, signals = run(report["symbols"], args.cache, slip, args.workers)
        res = analyze(trades)
        res.update({"signals": signals, "rejected": rejected, "slip_mult": slip})
        report[label] = res
        md += [f"\n## {label} (slippage ×{slip:g})", "",
               f"Signals {signals}; rejected by gates {json.dumps(rejected, sort_keys=True)}; "
               f"trades {res['trades_total']}, with OI+funding history {res['trades_eligible']}.",
               table(res), "", f"Decision (OOS, variant d): `{json.dumps(res['decision'])}`"]
        (args.out / f"trades_{label}.jsonl").write_text("".join(json.dumps(t.to_dict()) + "\n" for t in trades))
    (args.out / "results.json").write_text(json.dumps(report, indent=1, default=str))
    (args.out / "RESULTS-TABLES.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
