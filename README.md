# SURGE

SURGE is a local-first crypto market research and trading dashboard. It collects and stores public candle history from Coinbase, Binance, Kraken, and OKX; charts prices; runs technical and statistical strategies; backtests on stored data; and provides a persistent paper account with risk controls. An explainable local strategy ensemble can automate paper trading when separately enabled and armed.

Public candles come from Coinbase, Binance, Kraken, and OKX. A private Coinbase Advanced or Binance spot account is a CCXT credential slot (`SURGE_LIVE_EXCHANGE`, `SURGE_COINBASE_*` or `SURGE_BINANCE_*`) for a future authority process. The dashboard does not load those keys, and no running service is logged into an exchange. Read [live setup and deployment limits](deploy/DEPLOYMENT.md) before configuring exchange access. SURGE is a prototype and makes no profitability claims.

## Install and open the web UI

Python 3.12 or newer. From this directory:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn dashboard.backend.app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. The page serves the operator deck: Coinbase candlesticks, the paper-runtime snapshot, the strategy queue (including Markov and the Elliott Wave heuristic), and the paper STOP/KILL file commands. Those commands do not submit an exchange order. The web process does not read exchange API keys. `POST /api/live/orders` stays unavailable until an authority-owned venue path exists.

## OKX

OKX is a first-class exchange. In the dashboard pick **OKX** in the exchange selector (symbols like `BTC-USDT`) and press **Fetch & store candles**; strategy analysis and backtests then run on the stored OKX candles. OKX public candles need no credentials.

Live credentials (optional, for a future authority process; the web dashboard never loads them and `POST /api/live/orders` stays unavailable):

```sh
cp .env.example .env
# set SURGE_LIVE_EXCHANGE=okx, SURGE_OKX_API_KEY, SURGE_OKX_API_SECRET, SURGE_OKX_API_PASSPHRASE,
# SURGE_LIVE_MAX_ORDER_QUOTE, then SURGE_LIVE_TRADING_ENABLED=true and
# SURGE_LIVE_TRADING_ARMED=I_ACCEPT_LIVE_TRADING_RISK
```

Create the OKX key with Read + Trade permissions only (never Withdraw) and test with an OKX demo-trading key first. Paper trading is simulated locally and fills at the latest stored Coinbase hourly close; it does not place orders on OKX.

## Primary venue: OKX US

The platform's primary trading venue is **OKX US** (CCXT `okxus`, `SURGE_LIVE_EXCHANGE=okxus`). It is
the cheapest US-accessible exchange on the source list for spot. It excludes New York, Texas and US
territories; the operator is in Colorado. US-regulated perps on Kraken are far cheaper but have no documented API yet.
The operator's account is fee tier Lv1, confirmed through the API: 0.20% maker and 0.35% taker.
Prefer limit orders and low-turnover strategies. Reasoning, fee tables and sources:
[docs/research/2026-10-25-venue-selection.md](docs/research/2026-10-25-venue-selection.md). Live trading
stays disabled unless explicitly configured and armed.

## Data sources

SURGE reads 36 public market-data sources: 19 exchanges plus positioning, volatility, macro, ETF
flow, prediction-market and on-chain feeds. The dashboard's **API status** panel shows each one,
and the service snapshots them every 15 minutes (`SURGE_SOURCES_INTERVAL=0` turns that off).

```sh
.venv/bin/python -m data.sources status    # probe all sources once
```

From a US connection, 35 work once a free `COINALYZE_API_KEY` is in `.env`; Coinalyze also
covers Bybit and live Binance futures. BitMEX refuses US IPs and has no fallback. See
[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

## Historical archive and bracket backtests

```sh
.venv/bin/python -m data.binance_archive download --symbols BTCUSDT,ETHUSDT --start 2024-01-01
.venv/bin/python -m data.binance_archive import --symbols BTCUSDT --timeframe 1h
```

The first command downloads Binance USD-M 5m klines, 5m open interest and funding from the free
data.binance.vision archive, which works from the US. The second stores the candles for the
dashboard under the exchange `binance-usdm-archive`. `data/event_backtest.py` simulates entries
with a stop and a target, long or short. It uses stop-first fills, slippage, fees and funding, and
measures results in R. `research/trade_stats.py` holds the day-clustered bootstrap and the
random-subset filter test. For a worked, pre-registered example (a strategy that was killed),
see [docs/research/2026-10-25-fade-forced-flush/](docs/research/2026-10-25-fade-forced-flush/RESULTS.md).

## Stream exchange data to log files

```sh
.venv/bin/python -m data.stream            # optional: --interval 2 --log-dir logs/stream --max-ticks N
```

Polls OKX public tickers for BTC, ETH and XRP (vs USDT) and appends JSON lines to `logs/stream/BTC-USDT.log`, `ETH-USDT.log` and `XRP-USDT.log` (one file per coin). Ctrl-C stops it. No credentials are used.

## Using the dashboard

1. Open <http://127.0.0.1:8000>, choose exchange/symbol/timeframe, and click **Fetch & store candles**.
2. Review the chart and strategy queue; run a strategy or backtest on stored candles.
3. Paper automation is off by default: use the paper controls to arm (max 30 minutes), and STOP/KILL to halt.
4. Run tests with `pip install -r requirements-dev.txt && .venv/bin/python -m pytest tests`.

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
