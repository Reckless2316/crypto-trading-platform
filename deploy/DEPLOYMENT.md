# SURGE Deployment

SURGE runs as one unprivileged Docker service. The API and UI bind to loopback (`127.0.0.1:8000`); SQLite persists in the `surge-data` named volume. Public candle history needs no exchange credentials. Paper trading is the default execution mode.

## Start and operate

```sh
./deploy/deploy.sh start
./deploy/deploy.sh status
./deploy/deploy.sh logs
./deploy/deploy.sh stop
```

Open `http://127.0.0.1:8000` and fetch candles from a supported exchange. Arm paper trading before placing orders or enabling automated execution. The database initializes disarmed with the kill switch enabled. Stop and restart preserve paper state; `stop` does not delete the database volume.

## API

- `GET /health`, `GET /api/status`
- `POST /api/market/refresh?symbol=BTC-USD&timeframe=1h&limit=100`
- `GET /api/candles`, `GET /api/signals`, `GET /api/orders`
- `POST /api/control/arm`, `POST /api/control/kill`, `POST /api/control/flatten`
- `POST /api/strategy/run?symbol=BTC-USD`
- `POST /api/orders` with `{"symbol":"BTC-USD","side":"buy","usd_amount":100}`
- `GET /ws/live`
- `POST /api/ai/analyze`, `GET /api/ai/decisions`, `GET /api/strategies`
- `POST /api/backtest` for a locally stored exchange/symbol/timeframe range
- `GET/POST /api/automation` for opt-in paper-only AI automation
- `GET /api/live/status`, `POST /api/live/arm`, `/api/live/disarm`, `/api/live/orders`

The API has no user authentication; loopback binding is the access boundary. Do not expose it through a public reverse proxy without adding authentication and CSRF protections.

## Exchange history and live trading

Coinbase, Binance, and Kraken public candles can be fetched on demand and are stored in the exchange-aware SQLite `candle_history` table. The dashboard's backtest uses only locally stored candles; fetch enough data first. Backtests use next-bar-open fills and fees, but do not model spread, slippage, or liquidity.

Live orders use optional CCXT support and are disabled by default. To configure, copy `.env.example` to `.env`, choose `binance` or `coinbaseadvanced`, enter API credentials with withdrawals disabled, set an explicit per-order quote cap, then set both `SURGE_LIVE_TRADING_ENABLED=true` and `SURGE_LIVE_TRADING_ARMED=I_ACCEPT_LIVE_TRADING_RISK`. Rebuild/restart the service. The dashboard requires a separate explicit acknowledgement and live arm expires after five minutes. Live orders are manual only; the local AI automation remains paper-only. Use exchange sandbox/testnet credentials before any real funds. Never expose the unauthenticated dashboard outside loopback.

## Current limits

- Live trading is manual only. The AI automation endpoint supports paper mode only.
- Local AI is an explainable weighted ensemble of deterministic strategy signals, not a trained language model or profitability guarantee. Markov parameters are prototype estimates; Elliott Wave output is explicitly subjective.
- Paper risk controls use a fixed $10,000 starting account, 5% per-symbol exposure, a 3% daily realized-loss limit, and a 30-minute maximum paper arm.
- Paper fills use the latest stored Coinbase hourly close. Backtests use next-bar opens and fees but do not model slippage, spread, liquidity, or order rejection.
- Coinbase, Binance, and Kraken public history is fetched when requested; only Coinbase BTC-USD hourly data is refreshed by the startup background collector.
- SQLite is suitable for this single-process local deployment, not concurrent multi-user trading. The API has no user authentication; retain loopback-only exposure.
