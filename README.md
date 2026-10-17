# SURGE

SURGE is a local-first crypto market research and trading dashboard. It collects and stores public candle history from Coinbase, Binance, and Kraken; charts prices; runs technical and statistical strategies; backtests on stored data; and provides a persistent paper account with risk controls. An explainable local strategy ensemble can automate paper trading when separately enabled and armed.

Optional manual live orders are supported through CCXT for Binance and Coinbase Advanced. They are disabled by default, require API credentials plus two explicit arming steps, and are subject to a configured per-order cap. Read [live setup and deployment limits](deploy/DEPLOYMENT.md) before configuring exchange access. SURGE is a prototype and makes no profitability claims.

## Run with Docker

Requirements: Docker Engine with the Compose plugin.

```sh
./deploy/deploy.sh start
```

Open <http://127.0.0.1:8000>. The service loads Coinbase BTC-USD hourly candles on startup and refreshes them every five minutes. Use **Fetch & store candles** to add history from Coinbase, Binance, or Kraken. Strategy analysis and backtests use stored candles. Paper automation is opt-in and requires an active paper arm.

```sh
./deploy/deploy.sh status
./deploy/deploy.sh logs
./deploy/deploy.sh stop
```

On Windows, install and start Docker Desktop, then double-click `launch-windows.bat`. The launcher builds/starts the Compose service, waits for its health endpoint, and opens the dashboard. It also accepts `stop`, `restart`, `status`, or `logs` arguments. It uses port 8000 when available, otherwise it selects the next available port through 8099. To choose a specific port, set `$env:SURGE_HOST_PORT = '8001'` in PowerShell before running the launcher.

SQLite data persists in the Docker volume `surge-data`. Public market data needs network access but no exchange credentials. Binance candle requests fall back to Binance.US if Binance's global API returns HTTP 451. Live trading is disabled unless explicitly configured; see `.env.example` and the deployment notes.

## Paper controls

- The strategy catalog includes EMA, Bollinger Bands, RSI, MACD, Donchian breakout, Markov regime analysis, and an explicitly heuristic Elliott Wave signal.
- Backtests use next-bar-open fills and configurable fees, and report returns, drawdown, trades, and an equity curve. Spread, slippage, and liquidity are not modeled.
- Paper orders execute at the latest stored Coinbase hourly close with a 0.1% simulated fee. Paper arm lasts at most 30 minutes; position exposure is capped at 5% and daily realized loss at 3%.
- Candle history, decisions, paper orders, and account state persist in SQLite. The service starts disarmed with its kill switch on.

See [deployment notes](deploy/DEPLOYMENT.md) for API details, security boundaries, and live trading configuration.
