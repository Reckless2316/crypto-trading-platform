# SURGE Sentinel

Sentinel is a real-time crypto indicator engine in Go, with a Claude analyst agent on top. It reads
OKX market data (REST history plus a live websocket) and analyses each pair across three
timeframes. Every closed bar produces:

- a **Sentinel score** from -100 to 100 and a regime;
- a **seven-layer confluence** tally, scored independently for long and short;
- a **signal gate** with a trade plan;
- a **volatility forecast** with calibrated price ranges;
- a **skill-gated direction model**;
- a **self-auditing track record**.

Signals are hash-chained into a tamper-evident ledger. The same tools are served three ways: to a
Claude agent, to any MCP client, and over HTTP with a live dashboard. A TradingView Pine Script v6
port charts the indicator.

It reads public data only. It loads no exchange keys and never places orders.

## Why it is built this way

Sentinel was designed against an honest question: what in technical analysis actually predicts
crypto out of sample, after fees? The walk-forward study is in
[`docs/research/2026-10-28-sentinel-predictiveness`](../docs/research/2026-10-28-sentinel-predictiveness/RESULTS.md).

| Component | Out-of-sample result | What Sentinel does with it |
|---|---|---|
| Volatility forecast (HAR-RV) | R² 0.11-0.44 vs naive -0.28-0.20; 68%/90% bands cover 67-69% / 87-90% | Headline output: expected move, calibrated ranges, expansion/contraction |
| Directional classifier | No Brier skill on any series | Skill gate: abstains (reports the base rate) until its recent window proves skill |
| TA confluence signals | About 0 to +0.15R better than random entry; still negative after OKX US fees | Signals carry an edge status; confidence is capped without proven edge; `-strict` mode |
| Indicator math | Matches TA-Lib (C) to 1e-7..1e-9 relative on 2,000 real bars | Golden test in CI |

The short version: Sentinel tells you **how far** price is likely to move with real, measured
skill. For **which way**, it tells you whether there is any measured edge, and there usually is not
after costs at short timeframes.

## Architecture

```
OKX REST history ──► warm-up replay (time-ordered, pooled model) ─┐
OKX websocket (443, REST-poll fallback) ──► closed bars ──────────┼─► Desk ──► Engine per symbol
OKX perp funding/OI/basis, Deribit PCR/DVOL/skew (every 5/15 min) ─┘         │
                                                                              ├─ Frames: base / mid / high (aggregated, never look ahead)
                                                                              │    TA-Lib-verified streaming indicators, swings, divergence,
                                                                              │    33 candlestick patterns, support/resistance
                                                                              ├─ Regime: trend_up / trend_down / range / volatile
                                                                              ├─ Confluence: 7 capped layers, independent long/short votes
                                                                              ├─ Volatility: HAR-RV (RLS) + empirical quantile bands
                                                                              ├─ Direction model: FTRL-Proximal + Platt, prequential, skill-gated
                                                                              ├─ Gate: score, layers, trend/reversal route, disagreement cap, BTC shock, model edge
                                                                              └─ Triple-barrier labels → model training + signal track record
Snapshots ──► SSE stream + dashboard ─ HTTP tools ─ MCP stdio ─ Claude analyst agent ─ hash-chained ledger
```

| Package | Role |
|---|---|
| `internal/ta` | Streaming indicators: SMA/EMA/RMA/WMA/HMA/KAMA, RSI, MACD, ATR, ADX/DI, Bollinger, Keltner, StochRSI, MFI, OBV, CMF, session VWAP, Supertrend, Donchian, efficiency ratio, regression slope, Parkinson volatility, swings/divergence/levels |
| `internal/candles` | The 33 MotiveWave study patterns, with ATR-normalised thresholds and a trend filter |
| `internal/engine` | Multi-timeframe frames, regime, confluence, features, triple-barrier labels, gate, plan, confidence |
| `internal/model` | FTRL-Proximal + Platt classifier with skill gate, prequential metrics, ridge logistic, HAR-RV forecaster |
| `internal/desk` | Universe coordinator: time-merged warm-up, BTC context, context refresh, ledger, subscribers |
| `internal/feed` | OKX REST and websocket, OKX perp positioning, Deribit options |
| `internal/backtest` | Walk-forward evaluation with bootstrap CIs and a verdict |
| `internal/tools` | The tool set shared by the agent, MCP and HTTP |
| `internal/agent` | Claude analyst (Anthropic Go SDK, `claude-opus-5-5`) |
| `internal/mcp` | Model Context Protocol server (JSON-RPC over stdio) |
| `internal/server` | HTTP API, SSE stream, embedded dashboard |
| `internal/ledger` | Append-only, SHA-256 hash-chained JSONL journal |
| `pine/surge_sentinel.pine` | TradingView port (confluence, HAR forecast, self-audit) |

### The seven layers

| Layer | Frame | Evidence |
|---|---|---|
| Trend | high | Price vs EMA200/EMA50, MACD momentum, swing structure (HH/HL), Supertrend, Hull slope |
| Reversal | mid | RSI extremes, regular RSI divergence, Bollinger bounce, candlestick patterns, support/resistance proximity, squeeze breakout |
| Momentum | mid | Volume surge, MACD building, StochRSI cross, VWAP position, Chaikin money flow, Donchian breakout, ADX direction |
| Timing | base | Momentum burst, fast RSI cross, strong close, Supertrend flip |
| Microstructure | live | Open interest vs price, crowded funding (adds disagreement only: the fade-forced-flush study killed crowding reversals), basis |
| Sentiment | live | Deribit put/call ratio (contrarian), call-put skew, DVOL spike at stretched prices |
| Context | cross-asset | Relative strength vs BTC, BTC trend, regime alignment |

Each layer is capped, so correlated indicators cannot fake confluence. A signal must pass every
gate check:

- minimum winning score;
- at least three agreeing layers;
- a trend route (trend and momentum agree) or a reversal route (exhaustion cluster plus timing or
  momentum);
- opposing score at most 50% of the winning score;
- no adverse BTC shock;
- positive net edge, when the model is skilled.

All constants are published in `engine.DefaultConfig` and exposed as flags.

### Timeframe ladder

The base frame is the chart you trade. The mid and high frames are built from base bars, so they
never look ahead.

| Base | Mid | High |
|---|---|---|
| 5m | 15m | 1h |
| 15m | 1h | 4h |
| 30m | 2h | 12h |
| 1h | 4h | 1d |
| 4h | 1d | 1w |

## Quick start

Requires Go 1.25 or newer (the toolchain downloads automatically with `GOTOOLCHAIN=auto`).

```sh
cd sentinel
go build -o sentinel ./cmd/sentinel

./sentinel analyze -symbol BTC-USDT -tf 1h -text        # one-shot analysis
./sentinel scan -tf 4h                                  # rank the default universe
./sentinel backtest -symbol ETH-USDT -tf 1h -bars 12000 # walk-forward evaluation with fees
./sentinel serve -tf 1h                                 # live engine + dashboard at http://127.0.0.1:8090
```

`serve` warms each symbol up from history, then streams closed bars from the OKX websocket. Port
443 is used because it passes through more proxies; `-ws` overrides it. After three failed
connections it falls back to REST polling. Signals are evaluated only on confirmed bars, so nothing
repaints. Useful `serve` flags:

| Flag | Default | Meaning |
|---|---|---|
| `-symbols` | 8 majors | live universe (BTC-USDT is added as the reference) |
| `-tf` | `1h` | base timeframe; `4h` is the most cost-viable on OKX US fees |
| `-ledger` | `data/sentinel-ledger.jsonl` | hash-chained signal ledger (`""` disables it) |
| `-strict` | off | signals are actionable only with proven edge or a skilled model |
| `-token` / `SENTINEL_TOKEN` | none | bearer token for POST endpoints; required to bind a non-loopback address |
| `-okx` | `https://www.okx.com` | REST host (`https://us.okx.com` also serves public spot data) |
| `-stop-atr`, `-target-r`, `-horizon` | 1.5, 2, 48 | barrier geometry |
| `-fee-entry`, `-fee-exit`, `-slippage` | 0.20, 0.35, 0.05 | costs, percent |

Verify the ledger at any time with `./sentinel verify -ledger data/sentinel-ledger.jsonl`.

## The analyst agent

Set `ANTHROPIC_API_KEY` (or run `ant auth login`). Then:

```sh
./sentinel agent -symbol SOL-USDT -tf 4h "Is this breakout worth taking given funding and the volatility outlook?"
```

The dashboard's **Ask the analyst** box calls the same agent when `serve` has credentials.

- Model: Claude Opus 5.5 (`claude-opus-5-5`), effort `high`.
- Server-side refusal fallbacks are on. `-no-fallback` disables them.
- The agent investigates with Sentinel's tools, then submits a strictly typed assessment: a stance
  (`concur`, `caution`, `disagree` or `no_trade`), evidence, risks, invalidation and watch levels.

Determinism comes first. The agent cannot change any number. A grounding check flags any watch level
that does not appear in a tool result.

## MCP server

`sentinel mcp` serves 11 read-only tools over stdio:

- `analyze_symbol`, `scan_market`, `get_readings`, `volatility_forecast`, `model_report`
- `run_backtest`, `compute_indicator`, `candlestick_patterns`
- `derivatives_positioning`, `options_sentiment`, `signal_ledger`

For Claude Desktop, Claude Code or Hermes, add this to the MCP config (see
`examples/mcp-config.json`):

```json
{ "mcpServers": { "surge-sentinel": { "command": "/path/to/sentinel", "args": ["mcp", "-tf", "1h"] } } }
```

## HTTP API (`serve`)

| Method | Path | |
|---|---|---|
| GET | `/` | live dashboard |
| GET | `/healthz` | universe, timeframe, agent availability |
| GET | `/api/briefs` | latest brief per symbol |
| GET | `/api/snapshot/{symbol}` | full snapshot (every reading, vote and gate check) |
| GET | `/api/stream` | server-sent events, one `brief` per closed bar or context refresh |
| GET | `/api/tools` | tool list with JSON schemas |
| POST | `/api/tools/{name}` | run a tool (JSON body) |
| POST | `/api/agent` | `{"symbol","timeframe","question"}` → analyst assessment |
| GET | `/api/ledger` | ledger verification, live record, recent entries |

POST endpoints refuse cross-origin requests, and require the bearer token when one is set.

The SURGE dashboard reads Sentinel through `strategies/sentinel_client.py` and
`GET /api/sentinel/briefs`. These are advisory only; execution stays with the authority process.

## TradingView

Paste `pine/surge_sentinel.pine` into the Pine editor. It ports:

- the confluence layers (trend, reversal, momentum, timing, BTC context) and the gate;
- the HAR volatility forecast, fitted by recursive least squares in Pine, with empirically
  calibrated bands projected `horizon` bars forward;
- the self-audit: triple-barrier outcomes of fired signals, net of fees, giving the edge status.

Higher-timeframe values use the previous completed bar (`lookahead_on` with `[1]`), and signals fire
only on confirmed bars. The script passes a Pine v6 compile check. The direction model,
perp-positioning layer and options layer exist only in the Go engine.

## Testing

```sh
go vet ./... && go test -race ./...
```

All tests are offline: OKX, Deribit and the Anthropic API are faked with `httptest`.

- `internal/ta` checks 22 indicator outputs against `testdata/talib_golden.csv`. That file was
  generated by the TA-Lib C library from 2,000 real OKX BTC-USDT hours; rebuild it with
  `tools/talibgold/gen.sh`.
- The engine tests prove determinism, that snapshots never repaint, triple-barrier labelling,
  de-duplication and side-effect-free context refresh.
- The model tests prove that the skill gate opens on real signal and stays shut on noise, and that
  the HAR bands are calibrated on GARCH data.

## Lineage

Sentinel absorbs the repositories it was asked to build from:

- **cinar/indicator** (Go) and **TA-Lib** (C): streaming indicator design, verified against TA-Lib.
- **crypto-indicators-mcp** (JS): the MCP tool server, rebuilt on the Go engine.
- **Aminoid-Alpha** (docs): independent long/short scoring, capped layers, the dual-route gate, the
  disagreement cap, price-aware cooldown and "determinism first, AI second". Sentinel adds measured
  edge where Aminoid reports self-claimed win rates.
- **motivewave-candlestick-pattern-study** (Java): the 33 patterns and their priority order.
- **Options-Trading-Strategies-in-Python**: the PCR and VIX strategies, adapted as Deribit put/call
  and DVOL sentiment.
- **crypto_coins_technical_analysis** (TS): RSI/MACD/EMA scanning, generalised into `scan`.
- **CoinQuant HMA strategy**: the Hull MA trend filter.
- **Cryptocurrency-Parallel-Tree-Optimization**: Merkle-style hashing for the signal ledger, and
  concurrent fetching.
- This repository's own research: OKX US fee model, prequential evaluation, and the killed
  fade-forced-flush result.

Not financial advice. Sentinel is a research and decision-support tool, and every signal is advisory.
