# Data sources

SURGE reads every source on the operator's API-status list from that source's own endpoint.
The registry lives in [`data/sources/catalog.py`](../data/sources/catalog.py). Each source has a
probe that reads a small current sample (usually BTC), and every call goes into the platform's
data-call log, which plays the role of the "API Data Quality Log".

```sh
python -m data.sources status              # probe everything once and print a status table
python -m data.sources list                # method, key variable and docs URL for each source
python -m data.sources collect --every 300 # keep snapshots flowing into data/surge.sqlite3
```

The dashboard's **API status** panel shows the latest snapshot for each source, and its
**Probe all sources** button refreshes them. The web service also snapshots every source every
15 minutes (`SURGE_SOURCES_INTERVAL`, in seconds; `0` turns it off).

## Status on 2026-10-25, probed from a US IP

**33 of 38 work with no setup.** The other five need the operator:

| Source | State | What to do |
|---|---|---|
| Coinalyze | needs a free key | Sign up at coinalyze.net, copy the API key from the account page, and set `COINALYZE_API_KEY=` in `.env`. The key is sent in a header and is never logged. |
| Bybit | geo-blocked (HTTP 403) | Bybit's API refuses US IPs. With `COINALYZE_API_KEY` set, the probe reads Bybit's BTC OI and funding through Coinalyze instead. |
| BitMEX | geo-blocked (HTTP 403) | Same as Bybit: set `COINALYZE_API_KEY`. |
| Bentley | unidentified | No public crypto data API by this name was found. Send its URL or API docs and it can be added. |
| Market Intel Feed | unidentified | This looks like a feature of the dashboard the list came from, not a public API. If it is a news or alerts feed, name the provider. |

Sources that already work through a fallback:

- **Binance Futures, Binance long/short ratio:** the live API returns HTTP 451 to US IPs. The
  probes read Binance's public data.binance.vision archive instead (5m OI and account
  long/short ratio, about one day behind), or Coinalyze when a key is set.
- **Binance quarterly basis:** HTTP 451 from the US. The probe reports the OKX BTC quarterly
  basis instead and says so in its `via` field.
- **Binance Spot:** falls back to Binance.US, which has the same public shape.
- **Spot ETF flows:** reads Farside's public table. Setting `SOSOVALUE_API_KEY` (free at
  sosovalue.com/developer) switches to SoSoValue's API, which is sturdier than parsing HTML.
- **CoinGecko:** works without a key at low rate limits. `COINGECKO_API_KEY` (free demo key)
  raises them.

Routing around a geo-block with a VPN is deliberately not offered. Those venues bar US
persons in their terms, and Coinalyze serves the same data legitimately.

## How each source is reached

| Source | Method | Endpoint used by the probe | Key |
|---|---|---|---|
| Binance Spot | public REST + WebSocket | `api.binance.com/api/v3/ticker/24hr` (→ `api.binance.us`) | — |
| Binance Futures | public REST + WebSocket | `fapi.binance.com/fapi/v1/premiumIndex`, `/openInterest` (→ archive) | — |
| Bybit | public REST v5 + WebSocket | `api.bybit.com/v5/market/tickers` (→ Coinalyze) | — |
| OKX | public REST v5 + WebSocket | `okx.com/api/v5/market/ticker`, `/public/funding-rate`, `/public/open-interest` | — |
| OKX US | public REST v5 | `us.okx.com/api/v5/market/ticker` | — |
| Deribit | public JSON-RPC + WebSocket | `deribit.com/api/v2/public/ticker` | — |
| Hyperliquid | public POST + WebSocket | `api.hyperliquid.xyz/info` (`metaAndAssetCtxs`) | — |
| Gate.io | public REST v4 | `api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT` | — |
| KuCoin Futures | public REST | `api-futures.kucoin.com/api/v1/contracts/XBTUSDTM` | — |
| Coinbase Exchange | public REST + WebSocket | `api.exchange.coinbase.com/products/BTC-USD/ticker` | — |
| Kraken | public REST + WebSocket v2 | `api.kraken.com/0/public/Ticker` | — |
| BitMEX | public REST + WebSocket | `bitmex.com/api/v1/instrument` (→ Coinalyze) | — |
| Bitget | public REST v2 | `api.bitget.com/api/v2/mix/market/ticker` | — |
| MEXC | public REST | `contract.mexc.com/api/v1/contract/ticker` | — |
| HTX | public REST | `api.hbdm.com/linear-swap-ex/market/detail/merged`, `/swap_funding_rate` | — |
| Phemex | public REST | `api.phemex.com/md/v2/ticker/24hr` | — |
| dYdX | public indexer REST | `indexer.dydx.trade/v4/perpetualMarkets` | — |
| Bitfinex | public REST v2 | `api-pub.bitfinex.com/v2/ticker/tBTCUSD`, `/status/deriv` | — |
| WEEX | REST via CCXT | `ccxt.weex().fetch_ticker` | — |
| CoinGecko | public REST | `api.coingecko.com/api/v3/simple/price` | optional |
| Fear & Greed | public REST | `api.alternative.me/fng/` | — |
| Coinalyze | REST | `api.coinalyze.net/v1/open-interest`, `/funding-rate` | required |
| Economic calendar | public JSON (unofficial mirror) | `nfs.faireconomy.media/ff_calendar_thisweek.json` | — |
| Long/short accounts | public REST, three venues | Binance `globalLongShortAccountRatio`, Bybit `account-ratio`, OKX `rubik/stat/contracts/long-short-account-ratio` | — |
| Deribit DVOL | public JSON-RPC | `get_volatility_index_data` | — |
| Coinbase / Korea premiums | derived | Coinbase vs OKX × Kraken USDT/USD; Upbit KRW ÷ open.er-api.com USDKRW | — |
| Binance quarterly basis | public REST | `fapi.binance.com/futures/data/basis` (→ OKX quarterly) | — |
| Macro markets | unofficial | TradingView `scanner.tradingview.com/global/scan`, CNBC quote service as fallback | — |
| Spot ETF flows | HTML / REST | Farside table, or SoSoValue `historicalInflowChart` | optional |
| CFTC on CME | public Socrata REST (weekly) | `publicreporting.cftc.gov/resource/gpe5-46if.json`, code 133741 | — |
| Bitfinex margin | public REST v2 | `stats1/pos.size:1m:tBTCUSD:{long,short}/last` | — |
| OKX margin loan ratio | public REST v5 | `rubik/stat/margin/loan-ratio` | — |
| Polymarket | public REST | `gamma-api.polymarket.com/events?tag_slug=crypto` | — |
| Hyperliquid top traders | unofficial JSON | `stats-data.hyperliquid.xyz/Mainnet/leaderboard` | — |
| mempool.space | public REST + WebSocket | `mempool.space/api/v1/fees/recommended`, `/api/mempool` | — |

"Unofficial" endpoints (TradingView scanner, CNBC quotes, the ForexFactory mirror, Farside
HTML, the Hyperliquid leaderboard file) work today but are undocumented and can change
without notice. The registry marks them `official=False`, and the panel labels them.

The probes poll REST. Each registry entry also lists the exchange's public WebSocket URL
(`streams`) for when a source needs a live feed rather than snapshots. `data/stream.py`
already streams OKX tickers this way.

## Historical data

`python -m data.binance_archive download` fetches years of Binance USD-M 5m klines, 5m open
interest and funding from data.binance.vision. It is free, needs no key, and works from the
US. `python -m data.binance_archive import --symbols BTCUSDT --timeframe 1h` loads those
candles into the dashboard's candle store under the exchange name `binance-usdm-archive`.
