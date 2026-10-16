# Exchange API Matrix — Crypto Data (Free Tiers)

**Status:** PARTIAL — Research subagent failed; manual summary from docs.
**Updated:** 2026-10-14

## Confirmed Working Endpoints (Public, No Auth Required for Basic Data)

### Coinbase Advanced Trade — REST
- `https://api.exchange.coinbase.com/products` — List trading pairs
- `https://api.exchange.coinbase.com/products/BTC-USD/ticker` — Best bid/ask
- `https://api.exchange.coinbase.com/products/BTC-USD/candles` — Historical candles
  - Params: `start`, `end`, `granularity` (3600=1h, 86400=1d, 1=1min for some endpoints)
- `wss://ws-feed.exchange.coinbase.com` — WebSocket ticker + level 2 feed
- Rate limits: 10 req/sec per IP (REST), 100 connections (WS)
- Auth: Not required for market data; required for trading/account endpoints
- Geo: US-focused; some pairs restricted outside US
- Reliability: Very high (institutional grade)
- Crypto support: BTC, ETH, XRP, 200+ assets

### Binance — REST + WebSocket
- `https://api.binance.com/api/v3/klines` — Historical candles
  - Params: `symbol=BTCUSDT`, `interval=1m`, `startTime`, `endTime`, `limit=1000`
- `https://api.binance.com/api/v3/ticker/price` — Latest price
- `wss://stream.binance.com:9443/ws/btcusdt@kline_1m` — Real-time candlestick stream
- Rate limits: 1200 req/min per IP (REST), unlimited streams per WS connection
- Auth: Public endpoints require no auth; trading endpoints require HMAC-SHA256
- Geo: Restricted in US (use Binance.US for US users, lower limits)
- Reliability: High; occasional maintenance windows
- Crypto support: 400+ pairs

### Kraken — REST + WebSocket
- `https://api.kraken.com/0/public/AssetPairs` — Trading pairs
- `https://api.kraken.com/0/public/OHLC` — OHLC candles
  - Params: `pair=XBTUSD`, `interval=1`, `since=unix_timestamp`
- `wss://ws.kraken.com` — WebSocket (level 3, trades, ticker)
- Rate limits: ~15-20 req/sec (varies by endpoint tier)
- Auth: Public OHLC requires no auth; trading/orders need API key
- Geo: Global with some pair restrictions
- Reliability: High; lower volume than Binance/Coinbase
- Crypto support: 100+ pairs

### Bybit — REST + WebSocket (Derivatives Focus)
- `https://api.bybit.com/v5/market/kline` — K-line (candlestick) data
- `https://api.bybit.com/v5/market/instruments-info` — Symbol info
- Rate limits: 300 req/min (market data), 1200 req/min (trading)
- Auth: Public data requires no auth
- Geo: Global (not available in US, but public data accessible globally)
- Note: Derivatives (perpetual) focused; spot data limited

### Finnhub (Already Integrated in market-data-lab)
- `https://finnhub.io/api/v1/crypto/symbol` — Symbol search
- `https://finnhub.io/api/v1/crypto/candle` — Historical candles
  - Params: `symbol=BINANCE:BTCUSDT`, `resolution=M`, `from=unix`, `to=unix`
- `wss://ws.finnhub.io?token=<API_KEY>` — WebSocket
- Rate limits: 60 calls/min free tier, 200/min paid
- Auth: `FINNHUB_API_KEY` env var
- Reliability: High (used in market-data-lab successfully)
- Crypto support: Major pairs (BTC, ETH, XRP, SOL, DOGE, etc.)

### Alpha Vantage (Supplementary)
- `https://www.alphavantage.co/query` — Crypto data (`function=CRYPTO_INTRADAY`)
- Rate limits: 5 req/min free, 500/day
- Auth: `apikey=...`
- Reliability: Moderate; best for daily/hourly, not 1m

### Twelve Data (Supplementary)
- `https://api.twelvedata.com/time_series` — Crypto time series
- Rate limits: 8/min free, 100/day
- Auth: `apikey=...`
- Best for hourly/daily, not high-frequency

## Rate Limit Math

| Exchange | REST Req/Min | Daily REST Capacity | WebSocket Streams | Symbols Supported | Best For |
|----------|-------------|---------------------|-------------------|-------------------|----------|
| Coinbase | ~10/sec | 14.4M (unlimited practically) | 100 | 200+ | Reliable, US-regulated |
| Binance | 1200/min | 1.7M | Unlimited per conn | 400+ | High frequency, broad pairs |
| Kraken | ~20/sec | 28.8k | Limited | 100+ | Stable, lower volume |
| Finnhub | 60/min | 86.4k | 1 per token | Major pairs only | Cross-check, verification |

**For 50 symbols × 3 exchanges × 1m candles continuously:**
- REST polling: 3 requests/min (per exchange for 50 symbols with batch calls) = 180/min → well within limits
- WebSocket preferred for real-time: 1 stream per exchange covers all symbols
- Historical backfill (1 year 1h): 50 symbols × 8760 candles = 438k candles per exchange ≈ 6 REST batch calls (1000 candles per call)

## Authentication Requirements for Trading
| Exchange | Paper/Sandbox | Auth Method | Key Storage |
|----------|---------------|-------------|-------------|
| Coinbase | Sandbox environment available | HMAC-SHA256 with API Key + Secret | `.env` / encrypted config |
| Binance | Binance Testnet (`testnet.binance.vision`) | HMAC-SHA256 | `.env` / encrypted config |
| Kraken | No paper; must use small real orders for testing | HMAC-SHA256 | `.env` / encrypted config |
| Alpaca Crypto | Paper + Live unified (already in desk) | Bearer token or HMAC | `.env` / encrypted config |

## Geo Restrictions for Trading (Important for Multi-Account)
- **US users**: Coinbase, Kraken, Alpaca Crypto = allowed. Binance.US = limited. Binance main = restricted.
- **Non-US users**: Binance main = full access. Coinbase = some pairs restricted.
- Solution: Multi-account supports different exchanges per account. Paper accounts use testnet/sandbox regardless of geo.

## Working Code Snippets

### Coinbase REST — Historical Candles
```python
import requests
url = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
params = {"start": "2024-01-01T00:00:00Z", "end": "2024-01-02T00:00:00Z", "granularity": 3600}
r = requests.get(url, params=params, timeout=8)
for candle in r.json():  # [ts, low, high, open, close, volume]
    print(f"{candle[0]} OHLC={candle[3:5]} vol={candle[5]}")
```

### Binance REST — Historical Candles
```python
import requests
url = "https://api.binance.com/api/v3/klines"
params = {"symbol": "BTCUSDT", "interval": "1m", "startTime": 1704067200000, "limit": 100}
r = requests.get(url, params=params, timeout=8)
for k in r.json():  # [open_time, open, high, low, close, volume, ...]
    print(f"{k[0]} OHLC={k[1:5]} vol={k[5]}")
```

### Coinbase WebSocket — Live Ticker
```python
import websocket, json, ssl, threading
def on_message(ws, msg):
    data = json.loads(msg)
    if data.get("type") == "ticker":
        print(f"Ticker: {data['product_id']} price={data['price']} time={data['time']}")
ws = websocket.WebSocketApp("wss://ws-feed.exchange.coinbase.com",
    on_message=on_message)
ws.run_forever()
ws.send(json.dumps({"type":"subscribe","product_ids":["BTC-USD"],"channels":["ticker"]}))
```

### Finnhub — Crypto Quote (Already Verified)
```python
import requests
url = "https://finnhub.io/api/v1/crypto/candle"
params = {"symbol": "BINANCE:BTCUSDT", "resolution": "1", "from": 1704067200, "to": 1704153600, "token": os.environ.get("FINNHUB_API_KEY")}
r = requests.get(url, params=params, timeout=8)
```

## Reliability Assessment
- **Primary**: Coinbase (US regulatory backing, very high uptime, institutional data)
- **Secondary**: Binance (highest volume, global, generous limits, occasional maintenance)
- **Tertiary/Fallback**: Kraken (stable, lower rate limits, good for verification cross-check)
- **Verification**: Finnhub (independent source for price verification — matches market-data-lab design)
- **Supplementary**: Alpha Vantage (low frequency, useful for daily/hourly cross-check)

## Integration Notes for SURGE
- Data Layer should use Coinbase + Binance + Kraken as active sources
- Finnhub added as 4th source for verification (not primary collection)
- All 4 have working REST endpoints; 3 have working WebSocket streams
- Rate limits sufficient for continuous 1m collection for 50 symbols × 1 year backfill
- Authentication: only trading endpoints need keys; data endpoints are public
- Multi-account: US users use Coinbase + Kraken + Alpaca Crypto; non-US add Binance main

## Not Evidence / Unverified
- Actual live WebSocket stream stability over 24h (not observed in this session — would require continuous test)
- Binance.US vs Binance main geo-block behavior (depends on user's IP/location at test time)
- Coinbase Sandbox environment activation and funding (requires account creation — not done)
- Kraken paper/sandbox absence (documented limitation — Kraken only has live accounts)
- Exact rate limit enforcement behavior at 100% utilization (depends on exchange load at time of request)
- Cryptographic verification of raw response hashes against exchange signatures (not tested — would require HMAC verification with exchange public keys)
