# Fade the forced flush: results

Run 2026-10-25 against the rules in [PREREG.md](PREREG.md), committed before any result was
seen (`cab9f0d`). Full tables: [RESULTS-TABLES.md](RESULTS-TABLES.md). Raw numbers:
`results.json`.

## Verdict: kill

The pre-registered decision rule, applied mechanically to variant (d) out-of-sample, kills the
strategy on two counts:

- expectancy is **−0.34R per trade** (95% CI −0.64 to +0.02), against a +0.15R bar;
- there are only **50 trades** in 2024-01 to 2026-09 across 12 perps, against a 200-trade bar.

The filters do not earn their place either. Out-of-sample, (d) is 0.06R *worse* than the plain
sweep, and 60% of random subsets of (a) with the same size do at least as well (perm p = 0.60).

## Out-of-sample, base costs

| variant | trades | win | mean R (net) | 95% CI | perm p vs (a) |
|---|---|---|---|---|---|
| (a) sweep only | 1177 | 25% | −0.28 | −0.39 … −0.15 | — |
| (b) + OI flush | 166 | 33% | −0.23 | −0.43 … −0.02 | 0.33 |
| (c) + crowded funding | 301 | 27% | −0.28 | −0.44 … −0.11 | 0.52 |
| (d) + both | 50 | 30% | −0.34 | −0.64 … +0.02 | 0.60 |

In-sample (2021–2023) is no better: (a) −0.24R, (d) −0.48R. Doubling slippage changes
nothing material.

## Why it fails

**Costs are not the problem.** Mean gross R, before fees and funding, is already −0.17
(in-sample) and −0.20 (out-of-sample) for the plain sweep. Fees cost about 0.08R and funding
about 0. A cheaper venue would not rescue this.

**The losses are broad, not one bad pocket.** The plain sweep loses on 11 of 12 coins out-of-sample
(DOT is flat at +0.01R), on longs and shorts, and in every calendar year from 2021 to 2026.
That is a consistent sign, not noise around zero.

**What the trades do.** About 60% stop out and about 10% reach target. The 2R gate keeps setups
whose nearest opposing level averages about 4R away. That means price is in open space with no
nearby structure, which is where a "sweep" is often the first leg of a trend rather than a
trap. The tight stop (sweep extreme − 0.15 ATR) gets retested often.

**The OI flush is not a reversal signal.** In-sample, sweeps with a ≥2σ OI drop did markedly
*worse* than sweeps without one (−0.44R vs −0.24R; only 0.5% of random subsets were that bad).
Out-of-sample that flipped to a small, insignificant improvement. Either way there is no stable
information pointing toward reversal.

## Caveats (they cut both ways, and none would reverse a −0.2R gross loss)

- OI is Binance-only, not market-wide. No liquidation history was available, so the
  "liquidation spike bonus" was never tested.
- The universe was chosen with 2026 hindsight (no LUNA, FTT).
- 15m bars with stop-first fills are conservative when a stop and a target fall in one bar.
  This affects a small number of trades.
- CPI releases were not filtered.

## What this does and doesn't rule out

It rules out this rule set, with these starting values, on these 12 perps. It does not
prove that no sweep/flush idea works. But both samples have now been seen, so any variation
designed from these results (other timeframes, wider stops, fading only in ranges, trading the
sweep as a breakout instead) is a **new hypothesis**. It needs its own pre-registration and
data it was not designed on, such as forward paper trading from 2026-10 or a different venue's
history.

Reproduce: `python3 -m data.binance_archive download` (one-time download, about 550 MB), then
`python3 -m research.fade_flush.ablation`.
