"""Every external data source SURGE reads, how it is reached, and one probe per source.

A probe reads a small, current sample (usually BTC) from the source's own public endpoint and
returns a compact dict. Probes never place orders and never send credentials, except the
optional data-API keys named in `auth_env`, which are read from the environment.

`official=False` marks undocumented endpoints (the TradingView scanner, CNBC quotes, Farside
HTML, the Hyperliquid leaderboard file). They work today but can change without notice.
"""
from __future__ import annotations

import csv
import io
import os
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

import requests

from data.call_log import DataCallLog

USER_AGENT = "SURGE/1.0 (local research dashboard)"


class NeedsKey(RuntimeError):
    """The source works only with an API key that is not configured."""


class Unidentified(RuntimeError):
    """The source name does not map to a known public API."""


class Fetcher:
    """HTTP helper that records every call in the DataCallLog (the platform's data-quality log)."""

    def __init__(self, log: DataCallLog | None = None, session=None, timeout: float = 15):
        self.log = log
        self.session = session or requests.Session()
        self.timeout = timeout

    def _request(self, source: str, url: str, *, method="GET", params=None, body=None, headers=None):
        started = time.time()
        response = None
        error = None
        try:
            response = self.session.request(method, url, params=params, json=body, timeout=self.timeout,
                                            headers={"User-Agent": USER_AGENT, **(headers or {})})
            response.raise_for_status()
            return response
        except Exception as exc:  # recorded, then re-raised for the probe to classify
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if self.log is not None:
                self.log.record(source=source, symbol=None, timeframe=None, started_at=started,
                                elapsed_ms=int((time.time() - started) * 1000), ok=error is None,
                                row_count=1 if error is None else 0,
                                status_code=getattr(response, "status_code", None), error=error,
                                body=getattr(response, "content", None))

    def json(self, source: str, url: str, **kwargs):
        return self._request(source, url, **kwargs).json()

    def text(self, source: str, url: str, **kwargs) -> str:
        return self._request(source, url, **kwargs).text


@dataclass(frozen=True)
class Source:
    key: str
    name: str
    category: str          # exchange, positioning, volatility, sentiment, macro, flows, onchain, prediction, internal
    method: str            # how the data is delivered
    docs: str
    probe: Callable[[Fetcher], dict] | None
    auth_env: str | None = None
    official: bool = True
    us_trading: str | None = None   # exchanges only: can a US resident trade there?
    notes: str = ""
    streams: tuple[str, ...] = field(default_factory=tuple)  # websocket endpoints for live feeds


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _key(env: str) -> str:
    value = os.environ.get(env, "").strip()
    if not value:
        raise NeedsKey(f"set {env} in .env")
    return value


def _geo(exc: Exception) -> bool:
    return isinstance(exc, requests.HTTPError) and getattr(exc.response, "status_code", None) in (403, 451)


COINALYZE = "https://api.coinalyze.net/v1"


def _coinalyze_symbol(f: Fetcher, exchange: str, base: str = "BTC") -> str:
    """Find the BTC perpetual for an exchange in Coinalyze's market list (cached per Fetcher)."""
    if not hasattr(f, "_coinalyze_markets"):
        headers = {"api_key": _key("COINALYZE_API_KEY")}   # header, so the key never appears in a logged URL
        names = {e["code"]: e["name"] for e in f.json("coinalyze", f"{COINALYZE}/exchanges", headers=headers)}
        f._coinalyze_markets = [dict(m, exchange_name=names.get(m["exchange"], "")) for m in
                                f.json("coinalyze", f"{COINALYZE}/future-markets", headers=headers)]
    for m in f._coinalyze_markets:
        if m["exchange_name"].lower() == exchange.lower() and m.get("is_perpetual") and m.get("base_asset") == base \
                and m.get("quote_asset") in ("USDT", "USD"):
            return m["symbol"]
    raise RuntimeError(f"Coinalyze lists no {base} perpetual for {exchange}")


def coinalyze_perp(f: Fetcher, exchange: str) -> dict:
    symbol = _coinalyze_symbol(f, exchange)
    headers = {"api_key": _key("COINALYZE_API_KEY")}
    oi = f.json("coinalyze", f"{COINALYZE}/open-interest", headers=headers, params={"symbols": symbol, "convert_to_usd": "true"})
    fr = f.json("coinalyze", f"{COINALYZE}/funding-rate", headers=headers, params={"symbols": symbol})
    return {"via": f"coinalyze ({symbol})", "open_interest_usd": _f(oi[0]["value"]), "funding": _f(fr[0]["value"])}


def binance_archive_metrics(f: Fetcher, symbol: str = "BTCUSDT") -> dict:
    """Latest published day of Binance's 5m metrics archive (OI, account long/short); about a day behind."""
    today = datetime.now(timezone.utc).date()
    for back in (1, 2, 3):
        day = (today - timedelta(days=back)).isoformat()
        url = f"https://data.binance.vision/data/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{day}.zip"
        try:
            body = f._request("binance-archive", url).content
        except requests.HTTPError as exc:
            if getattr(exc.response, "status_code", None) == 404:
                continue
            raise
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            rows = [r for r in csv.reader(io.TextIOWrapper(archive.open(archive.namelist()[0]), "utf-8"))
                    if r and r[0][:1].isdigit()]
        last = rows[-1]
        return {"as_of": last[0], "open_interest_btc": _f(last[2]), "account_long_short": _f(last[6])}
    raise RuntimeError("no Binance metrics archive in the last 3 days")


# ---------------------------------------------------------------- exchanges

def binance_spot(f: Fetcher) -> dict:
    try:
        d = f.json("binance-spot", "https://api.binance.com/api/v3/ticker/24hr", params={"symbol": "BTCUSDT"})
        venue = "binance.com"
    except requests.HTTPError as exc:
        if getattr(exc.response, "status_code", None) not in (403, 451):
            raise
        # Binance.com refuses US IPs with 451; Binance.US serves the same public shape.
        d = f.json("binance-us-spot", "https://api.binance.us/api/v3/ticker/24hr", params={"symbol": "BTCUSDT"})
        venue = "binance.us (fallback)"
    return {"btc_last": _f(d["lastPrice"]), "quote_volume_24h": _f(d["quoteVolume"]), "venue": venue}


def binance_futures(f: Fetcher) -> dict:
    try:
        return _binance_futures_live(f)
    except requests.HTTPError as exc:
        if not _geo(exc):
            raise
    # fapi.binance.com refuses US IPs. Prefer Coinalyze (live); otherwise the public archive (about a day behind).
    if os.environ.get("COINALYZE_API_KEY"):
        return coinalyze_perp(f, "Binance")
    return {"via": "data.binance.vision archive (live API geo-blocked)", **binance_archive_metrics(f)}


def _binance_futures_live(f: Fetcher) -> dict:
    p = f.json("binance-futures", "https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"})
    oi = f.json("binance-futures", "https://fapi.binance.com/fapi/v1/openInterest", params={"symbol": "BTCUSDT"})
    return {"btc_mark": _f(p["markPrice"]), "funding": _f(p["lastFundingRate"]), "open_interest_btc": _f(oi["openInterest"])}


def bybit(f: Fetcher) -> dict:
    try:
        return _bybit_live(f)
    except requests.HTTPError as exc:
        if not _geo(exc) or not os.environ.get("COINALYZE_API_KEY"):
            raise
    return coinalyze_perp(f, "Bybit")


def _bybit_live(f: Fetcher) -> dict:
    d = f.json("bybit", "https://api.bybit.com/v5/market/tickers", params={"category": "linear", "symbol": "BTCUSDT"})
    t = d["result"]["list"][0]
    return {"btc_last": _f(t["lastPrice"]), "funding": _f(t["fundingRate"]), "open_interest_btc": _f(t["openInterest"])}


def okx(f: Fetcher) -> dict:
    t = f.json("okx", "https://www.okx.com/api/v5/market/ticker", params={"instId": "BTC-USDT-SWAP"})["data"][0]
    fr = f.json("okx", "https://www.okx.com/api/v5/public/funding-rate", params={"instId": "BTC-USDT-SWAP"})["data"][0]
    oi = f.json("okx", "https://www.okx.com/api/v5/public/open-interest", params={"instId": "BTC-USDT-SWAP"})["data"][0]
    return {"btc_last": _f(t["last"]), "funding": _f(fr["fundingRate"]), "open_interest_btc": _f(oi["oiCcy"])}


def okx_us(f: Fetcher) -> dict:
    t = f.json("okx-us", "https://us.okx.com/api/v5/market/ticker", params={"instId": "BTC-USD"})
    if t.get("code") != "0" or not t.get("data"):
        t = f.json("okx-us", "https://us.okx.com/api/v5/market/ticker", params={"instId": "BTC-USDC"})
    row = t["data"][0]
    return {"instrument": row["instId"], "btc_last": _f(row["last"]), "bid": _f(row["bidPx"]), "ask": _f(row["askPx"])}


def deribit(f: Fetcher) -> dict:
    r = f.json("deribit", "https://www.deribit.com/api/v2/public/ticker", params={"instrument_name": "BTC-PERPETUAL"})["result"]
    return {"btc_last": _f(r["last_price"]), "funding_8h": _f(r.get("funding_8h")), "open_interest_usd": _f(r["open_interest"])}


def hyperliquid(f: Fetcher) -> dict:
    meta, ctxs = f.json("hyperliquid", "https://api.hyperliquid.xyz/info", method="POST", body={"type": "metaAndAssetCtxs"})
    i = next(k for k, a in enumerate(meta["universe"]) if a["name"] == "BTC")
    c = ctxs[i]
    return {"btc_mark": _f(c["markPx"]), "funding_1h": _f(c["funding"]), "open_interest_btc": _f(c["openInterest"])}


def gate(f: Fetcher) -> dict:
    c = f.json("gate", "https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT")
    return {"btc_mark": _f(c["mark_price"]), "funding": _f(c["funding_rate"]), "position_size": _f(c.get("position_size"))}


def kucoin_futures(f: Fetcher) -> dict:
    c = f.json("kucoin-futures", "https://api-futures.kucoin.com/api/v1/contracts/XBTUSDTM")["data"]
    return {"btc_mark": _f(c["markPrice"]), "funding": _f(c.get("fundingFeeRate")), "open_interest": _f(c.get("openInterest"))}


def coinbase_exchange(f: Fetcher) -> dict:
    t = f.json("coinbase-exchange", "https://api.exchange.coinbase.com/products/BTC-USD/ticker")
    return {"btc_last": _f(t["price"]), "bid": _f(t["bid"]), "ask": _f(t["ask"])}


def kraken(f: Fetcher) -> dict:
    d = f.json("kraken", "https://api.kraken.com/0/public/Ticker", params={"pair": "XBTUSD"})
    if d.get("error"):
        raise RuntimeError(d["error"])
    t = next(iter(d["result"].values()))
    return {"btc_last": _f(t["c"][0]), "bid": _f(t["b"][0]), "ask": _f(t["a"][0])}


def bitmex(f: Fetcher) -> dict:
    try:
        return _bitmex_live(f)
    except requests.HTTPError as exc:
        if not _geo(exc) or not os.environ.get("COINALYZE_API_KEY"):
            raise
    return coinalyze_perp(f, "BitMEX")


def _bitmex_live(f: Fetcher) -> dict:
    r = f.json("bitmex", "https://www.bitmex.com/api/v1/instrument", params={"symbol": "XBTUSD"})[0]
    return {"btc_last": _f(r["lastPrice"]), "funding": _f(r.get("fundingRate")), "open_interest_usd": _f(r.get("openInterest"))}


def bitget(f: Fetcher) -> dict:
    t = f.json("bitget", "https://api.bitget.com/api/v2/mix/market/ticker",
               params={"symbol": "BTCUSDT", "productType": "USDT-FUTURES"})["data"][0]
    return {"btc_last": _f(t["lastPr"]), "funding": _f(t.get("fundingRate")), "open_interest_btc": _f(t.get("holdingAmount"))}


def mexc(f: Fetcher) -> dict:
    t = f.json("mexc", "https://contract.mexc.com/api/v1/contract/ticker", params={"symbol": "BTC_USDT"})["data"]
    return {"btc_last": _f(t["lastPrice"]), "funding": _f(t.get("fundingRate")), "open_interest_contracts": _f(t.get("holdVol"))}


def htx(f: Fetcher) -> dict:
    t = f.json("htx", "https://api.hbdm.com/linear-swap-ex/market/detail/merged", params={"contract_code": "BTC-USDT"})["tick"]
    fr = f.json("htx", "https://api.hbdm.com/linear-swap-api/v1/swap_funding_rate", params={"contract_code": "BTC-USDT"})["data"]
    return {"btc_last": _f(t["close"]), "funding": _f(fr.get("funding_rate"))}


def phemex(f: Fetcher) -> dict:
    r = f.json("phemex", "https://api.phemex.com/md/v2/ticker/24hr", params={"symbol": "BTCUSDT"})["result"]
    return {"btc_last": _f(r.get("closeRp")), "funding": _f(r.get("fundingRateRr")), "open_interest_btc": _f(r.get("openInterestRv"))}


def dydx(f: Fetcher) -> dict:
    m = f.json("dydx", "https://indexer.dydx.trade/v4/perpetualMarkets", params={"ticker": "BTC-USD"})["markets"]["BTC-USD"]
    return {"btc_oracle": _f(m["oraclePrice"]), "next_funding_1h": _f(m.get("nextFundingRate")), "open_interest_btc": _f(m.get("openInterest"))}


def bitfinex(f: Fetcher) -> dict:
    t = f.json("bitfinex", "https://api-pub.bitfinex.com/v2/ticker/tBTCUSD")
    deriv = f.json("bitfinex", "https://api-pub.bitfinex.com/v2/status/deriv", params={"keys": "tBTCF0:USTF0"})[0]
    # status/deriv row: [KEY, MTS, _, DERIV_PRICE, SPOT_PRICE, _, INSURANCE, _, NEXT_FUNDING_TS, NEXT_FUNDING_ACCRUED,
    #   NEXT_FUNDING_STEP, _, CURRENT_FUNDING, _, _, MARK_PRICE, _, _, OPEN_INTEREST, ...]
    return {"btc_last": _f(t[6]), "perp_funding": _f(deriv[12]), "perp_open_interest": _f(deriv[18])}


def weex(f: Fetcher) -> dict:
    # WEEX documents its REST API but changes paths often; CCXT tracks them, so use it here.
    import ccxt
    t = ccxt.weex({"timeout": int(f.timeout * 1000)}).fetch_ticker("BTC/USDT")
    return {"btc_last": _f(t.get("last")), "via": "ccxt"}


# ---------------------------------------------------------------- market data and positioning

def coingecko(f: Fetcher) -> dict:
    headers = {"x-cg-demo-api-key": os.environ["COINGECKO_API_KEY"]} if os.environ.get("COINGECKO_API_KEY") else None
    d = f.json("coingecko", "https://api.coingecko.com/api/v3/simple/price", headers=headers,
               params={"ids": "bitcoin,ethereum", "vs_currencies": "usd", "include_market_cap": "true"})
    return {"btc_usd": _f(d["bitcoin"]["usd"]), "eth_usd": _f(d["ethereum"]["usd"]), "btc_mcap": _f(d["bitcoin"]["usd_market_cap"])}


def fear_greed(f: Fetcher) -> dict:
    d = f.json("fear-greed", "https://api.alternative.me/fng/", params={"limit": "1"})["data"][0]
    return {"value": int(d["value"]), "label": d["value_classification"], "timestamp": int(d["timestamp"])}


def coinalyze(f: Fetcher) -> dict:
    _key("COINALYZE_API_KEY")
    out = {}
    for venue in ("Binance", "Bybit", "OKX", "BitMEX"):
        try:
            row = coinalyze_perp(f, venue)
            out[venue.lower()] = {"oi_usd": row["open_interest_usd"], "funding": row["funding"]}
        except Exception as exc:
            out[f"{venue.lower()}_error"] = str(exc)[:80]
    return out


def economic_calendar(f: Fetcher) -> dict:
    events = f.json("forexfactory", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    high = [e for e in events if e.get("impact") == "High" and e.get("country") == "USD"]
    return {"events_this_week": len(events), "usd_high_impact": [f"{e['date'][:16]} {e['title']}" for e in high][:8]}


def long_short_accounts(f: Fetcher) -> dict:
    """Account long/short ratios. Each venue is independent: one refusal must not hide the others."""
    out = {}

    def binance_():
        try:
            b = f.json("ls-binance", "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                       params={"symbol": "BTCUSDT", "period": "5m", "limit": "1"})
            return _f(b[0]["longShortRatio"])
        except requests.HTTPError as exc:
            if not _geo(exc):
                raise
            m = binance_archive_metrics(f)
            out["binance_as_of"] = m["as_of"] + " (archive; live API geo-blocked)"
            return m["account_long_short"]

    def bybit_():
        y = f.json("ls-bybit", "https://api.bybit.com/v5/market/account-ratio",
                   params={"category": "linear", "symbol": "BTCUSDT", "period": "5min", "limit": "1"})["result"]["list"][0]
        return _f(y["buyRatio"]) / _f(y["sellRatio"])

    def okx_():
        o = f.json("ls-okx", "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio",
                   params={"ccy": "BTC", "period": "5m"})["data"][0]
        return _f(o[1])

    for name, read in (("binance", binance_), ("bybit", bybit_), ("okx", okx_)):
        try:
            out[name] = read()
        except Exception as exc:
            out[f"{name}_error"] = "geo-blocked" if _geo(exc) else type(exc).__name__
    if not any(k in out for k in ("binance", "bybit", "okx")):
        raise RuntimeError(f"no venue answered: {out}")
    return out


def deribit_dvol(f: Fetcher) -> dict:
    now = int(time.time() * 1000)
    r = f.json("deribit-dvol", "https://www.deribit.com/api/v2/public/get_volatility_index_data",
               params={"currency": "BTC", "start_timestamp": now - 7_200_000, "end_timestamp": now, "resolution": "60"})["result"]
    last = r["data"][-1]   # [ts, open, high, low, close]
    return {"btc_dvol": _f(last[4]), "ts": last[0]}


def premiums(f: Fetcher) -> dict:
    cb = _f(f.json("premium-coinbase", "https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])
    ref = _f(f.json("premium-okx", "https://www.okx.com/api/v5/market/ticker", params={"instId": "BTC-USDT"})["data"][0]["last"])
    usdt = _f(f.json("premium-usdt", "https://api.kraken.com/0/public/Ticker", params={"pair": "USDTUSD"})["result"]
              ["USDTZUSD"]["c"][0])
    upbit = _f(f.json("premium-upbit", "https://api.upbit.com/v1/ticker", params={"markets": "KRW-BTC"})[0]["trade_price"])
    krw = _f(f.json("premium-fx", "https://open.er-api.com/v6/latest/USD")["rates"]["KRW"])
    offshore_usd = ref * usdt
    return {"coinbase_premium_pct": (cb / offshore_usd - 1) * 100, "korea_premium_pct": (upbit / krw / cb - 1) * 100,
            "coinbase_btc_usd": cb, "okx_btc_usdt": ref, "upbit_btc_krw": upbit, "usd_krw": krw}


def binance_quarterly_basis(f: Fetcher) -> dict:
    try:
        return _binance_basis_live(f)
    except requests.HTTPError as exc:
        if not _geo(exc):
            raise
    return {"via": "OKX quarterly (Binance basis API geo-blocked)", **okx_quarterly_basis(f)}


def okx_quarterly_basis(f: Fetcher) -> dict:
    # OKX lists BTC dated futures only as coin-margined BTC-USD contracts.
    inst = f.json("okx-basis", "https://www.okx.com/api/v5/public/instruments",
                  params={"instType": "FUTURES", "instFamily": "BTC-USD"})["data"]
    quarter = next((i for i in inst if i.get("alias") == "quarter"), None)
    if quarter is None:
        raise RuntimeError("OKX lists no BTC quarterly future")
    fut = _f(f.json("okx-basis", "https://www.okx.com/api/v5/market/ticker", params={"instId": quarter["instId"]})["data"][0]["last"])
    idx = _f(f.json("okx-basis", "https://www.okx.com/api/v5/market/index-tickers", params={"instId": "BTC-USD"})["data"][0]["idxPx"])
    days = (int(quarter["expTime"]) / 1000 - time.time()) / 86400
    basis = fut / idx - 1
    return {"contract": quarter["instId"], "basis_rate": basis, "annualized_basis": basis * 365 / days if days > 0 else None,
            "futures_price": fut, "index_price": idx}


def _binance_basis_live(f: Fetcher) -> dict:
    d = f.json("binance-basis", "https://fapi.binance.com/futures/data/basis",
               params={"pair": "BTCUSDT", "contractType": "CURRENT_QUARTER", "period": "5m", "limit": "1"})[0]
    return {"basis_rate": _f(d["basisRate"]), "annualized_basis": _f(d.get("annualizedBasisRate")),
            "futures_price": _f(d["futuresPrice"]), "index_price": _f(d["indexPrice"])}


MACRO = {"SPX": "SP:SPX", "NDX": "NASDAQ:NDX", "DXY": "TVC:DXY", "US10Y": "TVC:US10Y", "GOLD": "TVC:GOLD", "VIX": "TVC:VIX"}
CNBC = {"SPX": ".SPX", "NDX": ".NDX", "DXY": ".DXY", "US10Y": "US10Y", "GOLD": "@GC.1", "VIX": ".VIX"}


def macro_markets(f: Fetcher) -> dict:
    try:
        d = f.json("tradingview-scanner", "https://scanner.tradingview.com/global/scan", method="POST",
                   body={"symbols": {"tickers": list(MACRO.values())}, "columns": ["close", "change"]})
        by = {row["s"]: row["d"] for row in d["data"]}
        return {"via": "tradingview", **{k: {"last": by[v][0], "change_pct": by[v][1]} for k, v in MACRO.items() if v in by}}
    except Exception:
        d = f.json("cnbc-quotes", "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol",
                   params={"symbols": "|".join(CNBC.values()), "requestMethod": "itv", "noform": "1",
                           "partnerId": "2", "fund": "1", "exthrs": "1", "output": "json"})
        rows = d["FormattedQuoteResult"]["FormattedQuote"]
        by = {r["symbol"]: r for r in rows}
        return {"via": "cnbc", **{k: {"last": _f(str(by[v]["last"]).replace(",", "").rstrip("%")),
                                     "change_pct": _f(str(by[v].get("change_pct", "")).replace("%", ""))}
                                 for k, v in CNBC.items() if v in by}}


def etf_flows(f: Fetcher) -> dict:
    if os.environ.get("SOSOVALUE_API_KEY"):
        d = f.json("sosovalue", "https://openapi.sosovalue.com/openapi/v2/etf/historicalInflowChart", method="POST",
                   headers={"x-soso-api-key": os.environ["SOSOVALUE_API_KEY"]}, body={"type": "us-btc-spot"})
        last = d["data"][0]
        return {"via": "sosovalue", "date": last["date"], "btc_net_inflow_usd": _f(last["totalNetInflow"])}
    html = f.text("farside", "https://farside.co.uk/bitcoin-etf-flow-all-data/")
    rows = re.findall(r"<tr>\s*<td><span class=\"tabletext\">(\d{2} \w{3} \d{4})</span></td>(.*?)</tr>", html, re.S)
    if not rows:
        raise RuntimeError("Farside layout not recognised (or Cloudflare challenge); set SOSOVALUE_API_KEY")
    date, cells = rows[-1]
    texts = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", cells, re.S)]
    total = texts[-1]   # the last column is the day's total across all funds; (x) means an outflow
    value = 0.0 if total in ("-", "") else _f(total.replace(",", "").replace("(", "-").replace(")", ""))
    return {"via": "farside", "date": date, "btc_net_flow_musd": value}


def cftc_cme(f: Fetcher) -> dict:
    # Traders in Financial Futures, futures only; 133741 is CME Bitcoin.
    rows = f.json("cftc", "https://publicreporting.cftc.gov/resource/gpe5-46if.json",
                  params={"cftc_contract_market_code": "133741", "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": "1"})
    r = rows[0]
    lev_long, lev_short = _f(r["lev_money_positions_long"]), _f(r["lev_money_positions_short"])
    am_long, am_short = _f(r["asset_mgr_positions_long"]), _f(r["asset_mgr_positions_short"])
    return {"report_date": r["report_date_as_yyyy_mm_dd"][:10], "open_interest": _f(r["open_interest_all"]),
            "leveraged_funds_net": lev_long - lev_short, "asset_managers_net": am_long - am_short}


def bitfinex_margin(f: Fetcher) -> dict:
    out = {}
    for side in ("long", "short"):
        row = f.json("bitfinex-margin", f"https://api-pub.bitfinex.com/v2/stats1/pos.size:1m:tBTCUSD:{side}/last")
        out[f"btc_{side}s"] = _f(row[1])
    out["long_short_ratio"] = out["btc_longs"] / out["btc_shorts"] if out["btc_shorts"] else None
    return out


def okx_margin_loan_ratio(f: Fetcher) -> dict:
    row = f.json("okx-loan-ratio", "https://www.okx.com/api/v5/rubik/stat/margin/loan-ratio",
                 params={"ccy": "BTC", "period": "1H"})["data"][0]
    return {"btc_loan_ratio": _f(row[1]), "ts": int(row[0])}


def polymarket(f: Fetcher) -> dict:
    import json as _json
    events = f.json("polymarket", "https://gamma-api.polymarket.com/events",
                    params={"tag_slug": "crypto", "active": "true", "closed": "false", "limit": "10",
                            "order": "volume24hr", "ascending": "false"})
    top = []
    for e in events[:5]:
        markets = sorted(e.get("markets", []), key=lambda m: _f(m.get("volume24hr")) or 0, reverse=True)
        m = markets[0] if markets else {}
        prices = _json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices")
        top.append({"event": e.get("title"), "market": m.get("question"),
                    "yes": _f(prices[0]) if prices else None, "volume_24h": _f(e.get("volume24hr"))})
    return {"crypto_events": len(events), "top": top}


def hyperliquid_top_traders(f: Fetcher) -> dict:
    rows = f.json("hyperliquid-leaderboard", "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")["leaderboardRows"]

    def week_pnl(row):
        return _f(dict(row["windowPerformances"]).get("week", {}).get("pnl")) or 0.0
    top = sorted(rows, key=week_pnl, reverse=True)[:10]
    return {"traders": len(rows), "top_week": [{"address": r["ethAddress"], "week_pnl": week_pnl(r),
                                                "account_value": _f(r["accountValue"])} for r in top]}


def mempool(f: Fetcher) -> dict:
    fees = f.json("mempool", "https://mempool.space/api/v1/fees/recommended")
    pool = f.json("mempool", "https://mempool.space/api/mempool")
    return {"fastest_sat_vb": fees["fastestFee"], "hour_sat_vb": fees["hourFee"], "mempool_tx": pool["count"],
            "mempool_vsize": pool["vsize"]}


def unidentified(name: str) -> Callable[[Fetcher], dict]:
    def probe_(f: Fetcher) -> dict:
        raise Unidentified(f"'{name}' does not match a known public API; tell us its URL")
    return probe_


def data_quality_log(f: Fetcher) -> dict:
    """SURGE's own call log, summarised: the equivalent of an API data-quality log."""
    if f.log is None:
        return {"calls": 0, "note": "no call log configured"}
    rows = f.log.recent(200)
    failed = [r for r in rows if not r["ok"]]
    return {"recent_calls": len(rows), "failed": len(failed),
            "failing_sources": sorted({r["source"] for r in failed})}


# ---------------------------------------------------------------- the registry

US_NO = "no (US residents restricted by terms)"

SOURCES: tuple[Source, ...] = (
    Source("binance-spot", "Binance Spot", "exchange", "public REST + WebSocket", "https://developers.binance.com/docs/binance-spot-api-docs",
           binance_spot, us_trading="no (Binance.US is the separate US entity)", notes="binance.com returns HTTP 451 to US IPs; probe falls back to api.binance.us",
           streams=("wss://stream.binance.com:9443/ws/btcusdt@trade",)),
    Source("binance-futures", "Binance Futures", "exchange", "public REST + WebSocket", "https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info",
           binance_futures, us_trading=US_NO, notes="Historical 5m OI/funding archive: data.binance.vision (data/binance_archive.py)",
           streams=("wss://fstream.binance.com/ws/btcusdt@markPrice",)),
    Source("bybit", "Bybit", "exchange", "public REST v5 + WebSocket", "https://bybit-exchange.github.io/docs/v5/intro", bybit,
           us_trading=US_NO, notes="Public API refuses US IPs (HTTP 403)", streams=("wss://stream.bybit.com/v5/public/linear",)),
    Source("okx", "OKX", "exchange", "public REST v5 + WebSocket", "https://www.okx.com/docs-v5/en/", okx,
           us_trading="via OKX US (us.okx.com): spot only; not NY/TX", streams=("wss://ws.okx.com:8443/ws/v5/public",)),
    Source("okx-us", "OKX US", "exchange", "public REST v5 (us.okx.com) + WebSocket", "https://www.okx.com/docs-v5/en/", okx_us,
           us_trading="yes, spot only; excludes NY, TX and US territories",
           notes="Primary trading venue (see docs/research/2026-10-25-venue-selection.md). CCXT id: okxus"),
    Source("deribit", "Deribit", "exchange", "public JSON-RPC over HTTP + WebSocket", "https://docs.deribit.com/", deribit,
           us_trading=US_NO, streams=("wss://www.deribit.com/ws/api/v2",)),
    Source("hyperliquid", "Hyperliquid", "exchange", "public POST /info + WebSocket", "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api",
           hyperliquid, us_trading=US_NO + "; on-chain, US persons barred by terms", streams=("wss://api.hyperliquid.xyz/ws",)),
    Source("gate", "Gate.io", "exchange", "public REST v4 + WebSocket", "https://www.gate.io/docs/developers/apiv4/", gate, us_trading=US_NO),
    Source("kucoin-futures", "KuCoin Futures", "exchange", "public REST + WebSocket (token)", "https://www.kucoin.com/docs-new", kucoin_futures,
           us_trading=US_NO),
    Source("coinbase-exchange", "Coinbase Exchange", "exchange", "public REST + WebSocket", "https://docs.cdp.coinbase.com/exchange/docs/welcome",
           coinbase_exchange, us_trading="yes (Advanced spot; US perpetual-style futures via Coinbase Financial Markets)",
           streams=("wss://ws-feed.exchange.coinbase.com",)),
    Source("kraken", "Kraken", "exchange", "public REST + WebSocket v2", "https://docs.kraken.com/api/", kraken,
           us_trading="yes (spot; US perps via Kraken Derivatives US, no documented API)", streams=("wss://ws.kraken.com/v2",)),
    Source("bitmex", "BitMEX", "exchange", "public REST + WebSocket", "https://www.bitmex.com/app/apiOverview", bitmex, us_trading=US_NO),
    Source("bitget", "Bitget", "exchange", "public REST v2 + WebSocket", "https://www.bitget.com/api-doc/common/intro", bitget, us_trading=US_NO),
    Source("mexc", "MEXC", "exchange", "public REST + WebSocket", "https://mexcdevelop.github.io/apidocs/", mexc, us_trading=US_NO),
    Source("htx", "HTX", "exchange", "public REST + WebSocket", "https://www.htx.com/en-us/opend/newApiPages/", htx, us_trading=US_NO),
    Source("phemex", "Phemex", "exchange", "public REST + WebSocket", "https://phemex-docs.github.io/", phemex, us_trading=US_NO),
    Source("dydx", "dYdX", "exchange", "public indexer REST + WebSocket", "https://docs.dydx.xyz/", dydx,
           us_trading=US_NO + "; front end geoblocks US"),
    Source("bitfinex", "Bitfinex", "exchange", "public REST v2 + WebSocket", "https://docs.bitfinex.com/docs", bitfinex, us_trading=US_NO),
    Source("weex", "WEEX", "exchange", "REST (via CCXT)", "https://www.weex.com/api-doc", weex, us_trading=US_NO),
    Source("coingecko", "CoinGecko", "market", "public REST (optional free demo key)", "https://docs.coingecko.com/", coingecko,
           auth_env="COINGECKO_API_KEY", notes="Works keyless at low rate limits; the key is optional"),
    Source("fear-greed", "Fear & Greed", "sentiment", "public REST (alternative.me)", "https://alternative.me/crypto/fear-and-greed-index/", fear_greed),
    Source("coinalyze", "Coinalyze", "positioning", "REST, free API key", "https://api.coinalyze.net/v1/doc/", coinalyze, auth_env="COINALYZE_API_KEY"),
    Source("bentley", "Bentley", "unknown", "unidentified", "", unidentified("Bentley")),
    Source("market-intel", "Market Intel Feed", "unknown", "unidentified (a feature of the source dashboard)", "", unidentified("Market Intel Feed")),
    Source("data-quality-log", "API Data Quality Log", "internal", "SURGE DataCallLog (SQLite)", "data/call_log.py", data_quality_log),
    Source("economic-calendar", "Economic calendar (ForexFactory)", "macro", "public JSON mirror (faireconomy.media)",
           "https://nfs.faireconomy.media/ff_calendar_thisweek.json", economic_calendar, official=False),
    Source("long-short", "Long/short accounts (Binance, Bybit, OKX)", "positioning", "public REST (three venues)",
           "https://www.okx.com/docs-v5/en/#trading-statistics-rest-api", long_short_accounts),
    Source("deribit-dvol", "Deribit volatility index", "volatility", "public JSON-RPC", "https://docs.deribit.com/#public-get_volatility_index_data", deribit_dvol),
    Source("premiums", "Coinbase and Korea premiums", "flows", "derived: Coinbase vs OKX, Upbit vs FX", "https://global-docs.upbit.com/", premiums,
           notes="Coinbase premium vs OKX BTC-USDT × Kraken USDT/USD; Korea premium = Upbit KRW / USDKRW vs Coinbase"),
    Source("binance-basis", "Binance quarterly basis", "positioning", "public REST", "https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Basis",
           binance_quarterly_basis),
    Source("macro", "Macro markets (TradingView, CNBC fallback)", "macro", "undocumented scanner / quote endpoints", "https://www.tradingview.com/",
           macro_markets, official=False),
    Source("etf-flows", "Spot ETF flows (Farside, SoSoValue)", "flows", "Farside HTML or SoSoValue REST (key)", "https://sosovalue.com/developer",
           etf_flows, auth_env="SOSOVALUE_API_KEY", official=False, notes="Farside is HTML behind Cloudflare; SoSoValue needs a free key"),
    Source("cftc", "CFTC positioning on CME", "positioning", "public Socrata REST (weekly)", "https://publicreporting.cftc.gov/", cftc_cme),
    Source("bitfinex-margin", "Bitfinex margin longs and shorts", "positioning", "public REST v2 stats", "https://docs.bitfinex.com/reference/rest-public-stats",
           bitfinex_margin),
    Source("okx-loan-ratio", "OKX margin loan ratio", "positioning", "public REST v5 (rubik)", "https://www.okx.com/docs-v5/en/#trading-statistics-rest-api-get-margin-lending-ratio",
           okx_margin_loan_ratio),
    Source("polymarket", "Polymarket odds", "prediction", "public REST (gamma API)", "https://docs.polymarket.com/", polymarket),
    Source("hyperliquid-top", "Hyperliquid top traders", "positioning", "undocumented leaderboard JSON", "https://app.hyperliquid.xyz/leaderboard",
           hyperliquid_top_traders, official=False),
    Source("mempool", "Bitcoin network (mempool.space)", "onchain", "public REST + WebSocket", "https://mempool.space/docs/api/rest", mempool,
           streams=("wss://mempool.space/api/v1/ws",)),
)

BY_KEY = {s.key: s for s in SOURCES}


def probe(source: Source, fetcher: Fetcher) -> dict:
    started = time.time()
    result = {"key": source.key, "name": source.name, "category": source.category, "fetched_at": started}
    try:
        result.update(state="ok", data=source.probe(fetcher))
    except NeedsKey as exc:
        result.update(state="needs_key", error=str(exc))
    except Unidentified as exc:
        result.update(state="unidentified", error=str(exc))
    except requests.HTTPError as exc:
        code = getattr(exc.response, "status_code", None)
        result.update(state="geo_blocked" if code in (403, 451) else "error", error=f"HTTP {code}")
    except Exception as exc:
        result.update(state="error", error=f"{type(exc).__name__}: {str(exc)[:200]}")
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


def probe_all(fetcher: Fetcher, keys=None, workers: int = 8) -> list[dict]:
    chosen = [BY_KEY[k] for k in keys] if keys else list(SOURCES)
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(lambda s: probe(s, fetcher), chosen))
