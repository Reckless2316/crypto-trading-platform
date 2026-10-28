# How predictive is SURGE Sentinel? Walk-forward results (2026-10-28)

## Question

Which parts of a technical-indicator engine actually predict crypto prices out of sample, after
OKX US fees? Sentinel was built to answer that, and its design follows from the answer.

## Method

- Data: OKX spot candles for BTC-USDT, ETH-USDT and SOL-USDT. That is 30,000 bars at 15m, 12,000 at
  1h and 6,000 at 4h, ending 2026-10-27/28. Downloaded with `sentinel fetch`.
- Everything is **prequential** (test, then train). At each bar Sentinel predicts first. A label
  trains the model only once its outcome is resolved, so every number is out-of-sample from the
  first bar to the last. Multi-symbol runs merge bars in global time order so a pooled model never
  learns from another symbol's future.
- Labels are triple-barrier (Lopez de Prado): stop at 1R = 1.5 x ATR(14), target at 2R, vertical
  barrier at 48 bars. If one bar touches both the stop and the target, it counts as a stop.
- Costs: 0.20% maker entry + 0.35% taker exit (OKX US Lv1) + 0.05% slippage, charged in R per trade.
- Reproduce one row: `sentinel backtest -symbol ETH-USDT -tf 1h -bars 12000`. Full JSON reports
  are in [`reports/`](reports/).

## Results

| data | bars | span | vol R² (naive) | 68% / 90% band coverage | long AUC / Brier skill | short AUC / Brier skill | long signals: n, net R (every-bar baseline) | short signals: n, net R (baseline) |
|---|---|---|---|---|---|---|---|---|
| BTC 15m | 30000 | 2025-12-19..2026-10-28 | 0.438 (0.200) | 66.7% / 88.9% | 0.595 / -0.007 | 0.484 / -0.015 | 766, -1.660 (-1.841) | 856, -1.674 (-1.838) |
| ETH 15m | 30000 | 2025-12-19..2026-10-28 | 0.414 (0.163) | 66.9% / 88.7% | 0.565 / -0.007 | 0.467 / -0.017 | 720, -1.144 (-1.308) | 949, -1.125 (-1.307) |
| BTC 1h | 12000 | 2025-06-15..2026-10-28 | 0.291 (-0.026) | 67.3% / 88.9% | 0.501 / -0.028 | 0.425 / -0.046 | 257, -0.659 (-0.772) | 488, -0.727 (-0.771) |
| ETH 1h | 12000 | 2025-06-15..2026-10-28 | 0.308 (0.058) | 67.6% / 89.9% | 0.496 / -0.030 | 0.428 / -0.051 | 237, -0.567 (-0.510) | 451, -0.437 (-0.541) |
| SOL 1h | 12000 | 2025-06-15..2026-10-28 | 0.375 (0.178) | 68.9% / 89.1% | 0.474 / -0.043 | 0.384 / -0.045 | 253, -0.348 (-0.483) | 427, -0.380 (-0.422) |
| BTC 4h | 6000 | 2024-02-01..2026-10-27 | 0.208 (0.012) | 67.5% / 87.2% | 0.449 / -0.069 | 0.427 / -0.077 | 118, -0.417 (-0.315) | 105, -0.098 (-0.357) |
| ETH 4h | 6000 | 2024-02-01..2026-10-27 | 0.114 (-0.279) | 67.6% / 89.0% | 0.493 / -0.057 | 0.388 / -0.086 | 101, -0.199 (-0.177) | 150, -0.133 (-0.251) |
| SOL 4h | 6000 | 2024-02-01..2026-10-27 | 0.211 (-0.017) | 66.7% / 87.1% | 0.392 / -0.101 | 0.458 / -0.064 | 59, -0.485 (-0.239) | 150, -0.260 (-0.241) |

The model AUC and Brier skill columns score the raw directional classifier on every bar, including
bars where the skill gate kept it from influencing signals.

## Findings

1. **Volatility is predictable. Direction is not.** The HAR-RV forecaster beats naive persistence on
   every series, with out-of-sample R² of 0.11 to 0.44 in log variance. After Sentinel switched its
   bands from normal quantiles to empirical quantiles, the 68% and 90% price bands cover 66.7-68.9%
   and 87.1-89.9% of realised closes. This is the most reliably predictive output Sentinel has.
2. **The directional classifier has no out-of-sample skill.** Its Brier skill is negative on every
   series. Short-side AUC sits below 0.5 across all assets and timeframes: what predicted the past
   regime inverts in the next one, which is consistent with short-horizon reversal. These checks
   were run during development and found the same result:
   - A purged, expanding-window ridge logistic refit.
   - Rolling windows of 1,500 and 4,000 labels. These were worse.
   - A compact set of 17 trend and volatility features.
   - A "decisive move" target. This turned out to be degenerate: 95% of labels hit a barrier.

   Sentinel therefore skill-gates the classifier. It reports the base rate, and does not affect
   signals, unless its recent 1,500-label window shows AUC >= 0.53 and positive Brier skill.
3. **Classic TA confluence has about zero edge after costs.** Confluence-gated signals (the
   Aminoid-style 5+ layer gate) improve on entering every bar by roughly 0 to +0.15R per trade. That
   is far less than the fees they must overcome. Every row has negative net expectancy.
4. **Costs decide the timeframe.** On a 1.5 x ATR stop, OKX US fees cost about 1.8R per trade at 15m,
   0.5-0.8R at 1h and 0.2-0.4R at 4h. A low-turnover 4h process is the only one with a realistic
   chance. This matches the venue research in this repository.

## What Sentinel does about it

- Signals carry an **edge status**: `proven`, `unproven` or `negative`. It comes from the realised,
  fee-adjusted record of fired signals for that symbol and side. Confidence is capped at tier C
  without proven edge, and at tier D with a negative record. `sentinel serve -strict` makes only
  proven-edge signals actionable.
- The direction model only gates signals while it demonstrates skill. Otherwise it abstains and
  says why.
- Live signals are written to a hash-chained ledger, so a future track record can be verified
  instead of self-reported.

## Caveats

- One market period. 2025-26 included a strong rally and a 25% decline. Results may differ in other
  regimes.
- Fixed barrier geometry. A sweep of stop and target settings (1.5-4 ATR, 1.5-3R) changed none of
  the conclusions.
- Microstructure (funding, OI) and options sentiment have no point-in-time history here, so they
  are excluded from the model and from these backtests. Their contribution is unmeasured.
- The data is spent for this hypothesis set. New ideas need fresh data or a pre-registered holdout,
  per [`docs/research/2026-10-25-fade-forced-flush/`](../2026-10-25-fade-forced-flush/RESULTS.md).
