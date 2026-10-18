# Paper runtime checkpoint (Claude Opus 5.5) — 2026-10-17

`HANDOFF(claude-opus-5.5 2026-10-17)`. This is a working PAPER system. It is not live-trading readiness.

## Commits
| Commit | What |
|---|---|
| `32a72bc` | Inherited Codex authority-owned paper reconciliation and venue/mode guard |
| `81910dd` | `bot/autonomous/runtime.py`: end-to-end paper loop (18 tests) |
| `bc13161` | `GET /api/autonomous/runtime` read model, `deploy/autonomous/` systemd units, `HANDOFF.md` |
| `9492575` | Fixes for 6 Important + minor findings from the independent review |
| `f735c2d` | Fixes for 2 Important regressions from the re-review (exits during warmup, nightly reviews the previous day) |
| `16a4d6e` | Live-run bug: local clock ~4 s ahead of Coinbase starved every snapshot; now snapshots the nearest close |
| `2c294cb` | Preserved untracked Codex live_venue/release/dashboard-authority WIP (NOT ACCEPTED) |
| `a0001ca` | Public Coinbase product rules (adverse rounding), executable-price labels net of fees, per-regime cohorts |
| `1f2b02d` | Review fixes: rules outages never block exits, dust frees the slot, short label math, schema runtime-v3 |

Verification: the full suite passes (`.venv/bin/python -m unittest discover -s tests`, 652 tests on Python 3.14). Two independent contrarian reviews: the first found 6 Important issues; the re-review passed all 6 and found 2 regressions, both now fixed. The live paper process is running against public Coinbase books and accumulating contiguous minute snapshots.

## Loop (one cycle per minute close)
1. **State (code):** public L2 book → mid, spread, imbalance, realized vol, momentum, inventory, drawdown. Observations after the cutoff are rejected, features require contiguous candles, and the snapshot is about 60–70 *estimated* tokens (bytes/3, not a provider tokenizer), well under the 400 limit.
2. **REFLEX:** `OfflineReflex` (`offline-reflex-v1`, UNCALIBRATED, deterministic rules). Jev is not wired yet: `JevClient` needs an approved release bundle and schema resolver (Task 15).
3. **Gate (code):** `policy.entry_gate`: quality ≥ 2, confidence > 0.80 (exactly 0.80 refuses), risk_state safe, not toxic, long only.
4. **Escalation:** confidence < 0.60 or crisis writes a durable row for BRAIN review. Crisis sets an operator-only latch. Escalation blocks entries only, and code exits always run.
5. **Sizing:** quarter-Kelly `kelly_loss_budget` from Wilson-lower calibrated p on gate-open resolved labels. Below 100 labels p is 0.5 and size is 0. Notional is capped to position room and cash. Negative edge holds.
6. **Hard risk (code, tighten-only):** 15% drawdown latch, 2% daily halt, 5% per-asset, KILL file (also blocks exits, per global constraints), and a local paper-arm flag.
7. **Fill:** adverse IOC against the fetched book. Quantity rounds down to the lot, the fee rounds up to µUSD.
8. **Learn:** exact-horizon labels net of fees, split Brier, nightly review of the previous UTC day, and a schema proposal that is never auto-applied.

## Operate
- Run: `python -m bot.autonomous.runtime --paper --data-dir data/autonomous/runtime` (add `--arm-paper` to permit paper fills, or `--explore-fraction 0.0005` for tiny labeled exploration entries).
- Stop: `touch data/autonomous/runtime/STOP`. Kill orders: `touch data/autonomous/runtime/KILL`. Review: `--report [--review-day YYYY-MM-DD]`.
- The service files are in `deploy/autonomous/` (not installed). Dashboard: `/api/autonomous/runtime` (read-only; the frontend panel is in the uncommitted operator `index.html` edits).
- The currently running process is NOT `--arm-paper`. It observes, judges, labels and escalates, but does not fill. That is deliberate until the operator arms paper.

## Research status (unchanged; see `docs/research/2026-10-17-autonomous-agent/`)
- **Regime:** unclassified, so HOLD (`INTRADAY-UPDATE-2026-10-17.md`; Kraken/Deribit/Circle/Fed sources dated there).
- **Candidates:** AAVE, HYPE, PENDLE, AERO, LDO and ONDO remain unqualified hypotheses. Float, comparable net revenue, holder concentration and executable depth are not proven. No metric is filled with an estimate. An empty eligible universe is the correct output.

## Remaining (ordered)
1. Wire `JevClient` behind an activated release bundle (Task 15). Keep the offline reflex as the fallback.
2. Route runtime paper orders through `OrderAuthority`/`Ledger` so paper and live share one accounting path (Tasks 12/16).
3. Use verified per-instrument `MarketRules`. Labels should account for spread and slippage, with per-regime cohorts.
4. Operator latch clearing and kill via the authenticated `control.py` socket, not files (Tasks 14/17).
5. Tasks 13 and 18–22 per `master-context.md`. Live progression needs explicit operator approval at every gate.

## WHAT COULD I BE WRONG ABOUT?
- **Edge may not exist.** The offline reflex is hand-written rules over a minute book. Nothing shows it beats fees. Its Brier score is the first honest test, and a likely outcome is "no supported cohort, never trades". That is survival working, not failure.
- **Organic vs incentive:** no candidate has a proven organic-demand decomposition. Token-level theses stay research-only.
- **Priced in:** no catalyst has synchronized announcement/price/positioning evidence. Assume anything known is priced in.
- **Value accrual:** no candidate clears the entitlement and cash-flow gates. Protocol activity is not token value.
- **Costs and slippage:** labels now use the best ask in and the best bid out, net of an *estimated* 0.6% taker fee per side. They still ignore depth beyond the top of book, latency and market impact, and paper fills execute against the same book that produced the signal. The edge is still somewhat overstated.
- **Calibration sample:** 100 labels per cohort is a floor, not proof. Minute-candle labels overlap and are autocorrelated, so the effective sample size is much smaller than the row count.
- **Clock:** the local clock was ~4 s off the venue. The runtime now tolerates ±10 s, but a larger skew silently holds forever. Monitor `AWAY_FROM_CLOSE` and `FUTURE_OBSERVATION` rates.
- **Hard limits delegated to the model?** None: the drawdown, daily, position, kill and arm checks are all in code and tighten-only. The model can trigger a crisis halt but cannot clear one. The KILL file blocks exits too, so a killed account with an open position stays exposed until the operator acts. That trade-off is set by the project's global constraints.
- **Token estimate:** the 400-token budget uses a bytes/3 estimate, not Jev's tokenizer. Prove it with the provider tokenizer before relying on it.
