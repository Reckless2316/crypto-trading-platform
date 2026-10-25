# Fade the forced flush: pre-registration

Written 2026-10-25, before any backtest of this strategy was run. Parameters below are the
starting values from the original design and are **not tuned**. Any later change is a new
experiment and must be reported as one, alongside this one.

## Hypothesis

When price sweeps an obvious level and closes back inside, OI falls sharply during the sweep,
and funding shows the swept side was crowded, the move beyond the level was forced flow
(stops, liquidations). Price then mean-reverts. The funding and OI filters are the only part
of this that could be new; the ablation tests whether they add anything over a plain sweep.

## Data

Binance USD-M perpetuals from data.binance.vision: 5m klines, 5m `sum_open_interest` (coin
units) from the daily metrics files, and funding settlements.
Universe: BTC, ETH, BNB, SOL, XRP, DOGE, ADA, AVAX, LINK, LTC, DOT, BCH (all USDT-margined).
Known biases: the universe is picked with 2026 hindsight (no LUNA, FTT); this is one
exchange's OI, not market-wide; no liquidation history (the "liquidation spike" bonus is
untested).

## Rules (long side; short side mirrors highs/lows and signs)

| # | Step | Rule |
|---|---|---|
| 1 | Bars | 15m UTC-aligned bars built from 5m. ATR = Wilder ATR(14) on 15m, taken at the bar **before** the sweep starts, so the sweep does not inflate it. |
| 2 | Levels | (a) prior UTC day low; (b) 4h fractal swing lows (low below the 2 bars on each side), usable only after the confirming 4h bar closes, active for 7 days. The low must be at least 12h old when it is pierced. A level is consumed the first time a 15m low trades below it, whatever happens next. |
| 3 | Sweep | Starting at the first piercing bar, within 3 bars (piercing bar included) a bar must close back above the level. Penetration = (level − sweep low) / ATR must be in [0.25, 1.5] at the reclaim. Exceeding 1.5 before a reclaim kills the setup. If several levels signal on one bar, the lowest level is used. |
| 4 | Flush | OI change from the snapshot at the piercing bar's open to the snapshot at the reclaim bar's close, as a log change, divided by (σ × √n). σ is the std of 15m OI log changes over the prior 30 days, and n is the number of bars in the window. Flush if z ≤ −2.0. |
| 5 | Crowding | F24 = sum of funding settlements in the last 24h. Rank it against the F24 values at every settlement in the prior 30 days (mid-rank for ties, so weeks pinned at the default rate rank 0.5, not 1.0). Long setup (lows swept): F24 > 0 and rank ≥ 0.70. Short setup (highs swept): F24 < 0 and rank ≤ 0.30. |
| 6 | Entry | Next 15m bar open after the reclaim close. Taker fee plus slippage. |
| 7 | Stop | Sweep low − 0.15 ATR. Skip the trade if the entry is already at or beyond the stop. |
| 8 | Target / 2R gate | Nearest opposite level beyond entry from: prior UTC day high, active 4h swing highs (7d), and active 1h swing highs (fractal 2/2, 3d). Skip if there is none, or if it is less than 2R away. |
| 9 | Cost gate | Skip if stop distance < 5 × round-trip cost (2 × taker + 2 × slippage). |
| 10 | Time stop | Exit at bar close if +1R has not been touched within 12 bars (3h). Hard exit at 96 bars (24h). |
| 11 | Skip | Entry within ±15 min of a funding settlement. Entry on an FOMC decision day between 17:30 and 21:00 UTC. CPI is **not** filtered: the release-date list isn't reliable enough to hardcode. |

Fills: the stop fills at the stop price, or at the open if the bar gaps through it, minus
slippage, plus taker fee. The target is a resting limit that needs high > target, and pays
the maker fee with no slippage. If the stop and the target both fall within one bar, the stop
is assumed to fill first. The entry bar is checked too. Funding is charged for each settlement
in (entry, exit].

Costs: taker 0.05%, maker 0.02%. Slippage per side: 0.02% for BTC and ETH, 0.05% for the
others. Stress run: slippage doubled.

## Ablation

(a) sweep only, (b) + flush, (c) + crowding, (d) + flush + crowding. Every structural gate
(2R, cost, time stop, skips) applies to all four. Only signals with OI and funding history
available count in any variant, so (d) ⊆ (b), (c) ⊆ (a) exactly. Signals are evaluated
independently, with no position blocking, which keeps the subsets nested. A portfolio sim
with per-symbol blocking is reported separately. In it, 0.5% risk per signal moment is split
across all signals opened in the same bar.

## Samples

In-sample 2021-01-01 to 2023-12-31. Out-of-sample 2024-01-01 to 2026-09-30. Nothing is
tuned on either, so both are read; the decision is made on OOS.

## Decision rules (fixed now)

Kill the strategy if, for variant (d) out-of-sample, any of these is true:
1. expectancy < +0.15R per trade net of costs;
2. fewer than 200 trades;
3. more than 50% of total net R comes from one symbol, or from one calendar month.

The filters earn their place only if OOS mean R(d) − mean R(a) > 0, **and** fewer than 5% of
random subsets of (a) with the same size as (d) have a mean ≥ mean R(d) (permutation p <
0.05). If (d) passes the kill rules but the filters fail this test, then the edge, if there is
one, belongs to the plain sweep, and the funding/OI story is decoration.

Uncertainty: 95% CIs by bootstrap clustered on calendar day (signals across coins on one day
are correlated).
