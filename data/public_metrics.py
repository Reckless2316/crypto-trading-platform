"""Logged public global snapshot. Stores only fields the response actually contains."""
from __future__ import annotations

import time
from typing import Callable

import requests

from data.call_log import DataCallLog

GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
COIN_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}"
ETHPLORER_HOLDERS_URL = "https://api.ethplorer.io/getTopTokenHolders/{address}"


def fetch_raw_holder_rows(log: DataCallLog, ids: dict[str, str], *, getter: Callable = requests.get,
                          timeout: float = 20) -> dict:
    """Read raw top-holder rows for an Ethereum contract. These rows are not entity-adjusted."""
    import re
    rows = []
    unresolved = []
    address_re = re.compile(r"^0x[0-9a-fA-F]{40}$")
    for symbol, gecko_id in ids.items():
        if not gecko_id or "/" in gecko_id:
            unresolved.append({"symbol": symbol, "reason": "invalid_id"})
            continue
        started = time.time()
        coin_url = COIN_URL.format(coin_id=gecko_id)
        coin = None
        try:
            coin = getter(coin_url, params={"localization": "false", "tickers": "false", "market_data": "false",
                          "community_data": "false", "developer_data": "false"}, timeout=timeout,
                          headers={"User-Agent": "SURGE-paper-platform/1.0"})
            coin_body = coin.content
            coin_status = coin.status_code
            coin.raise_for_status()
            payload = coin.json()
        except Exception:
            status = getattr(coin, "status_code", None)
            body = getattr(coin, "content", b"") or b""
            log.record(source="coingecko-coin", symbol=symbol, timeframe=None, started_at=started,
                       elapsed_ms=int((time.time() - started) * 1000), ok=False, row_count=0,
                       status_code=status, body=body if isinstance(body, bytes) else b"")
            unresolved.append({"symbol": symbol, "reason": "rate_limited" if status == 429 else "coin_request_failed",
                               "status_code": status})
            continue
        log.record(source="coingecko-coin", symbol=symbol, timeframe=None, started_at=started,
                   elapsed_ms=int((time.time() - started) * 1000), ok=isinstance(payload, dict),
                   row_count=1, status_code=coin_status, body=coin_body)
        platforms = payload.get("platforms") if isinstance(payload, dict) else None
        address = platforms.get("ethereum") if isinstance(platforms, dict) else None
        if not isinstance(address, str) or address_re.fullmatch(address) is None:
            unresolved.append({"symbol": symbol, "gecko_id": gecko_id, "reason": "no_ethereum_contract"})
            continue
        started = time.time()
        holder_url = ETHPLORER_HOLDERS_URL.format(address=address)
        try:
            response = getter(holder_url, params={"apiKey": "freekey", "limit": 10}, timeout=timeout,
                              headers={"User-Agent": "SURGE-paper-platform/1.0"})
            holder_body = response.content
            holder_status = response.status_code
            response.raise_for_status()
            holder_payload = response.json()
        except Exception:
            log.record(source="ethplorer-holders", symbol=symbol, timeframe=None, started_at=started,
                       elapsed_ms=int((time.time() - started) * 1000), ok=False, row_count=0,
                       status_code=None, body=b"")
            unresolved.append({"symbol": symbol, "reason": "holder_request_failed"})
            continue
        holders = holder_payload.get("holders") if isinstance(holder_payload, dict) else None
        log.record(source="ethplorer-holders", symbol=symbol, timeframe=None, started_at=started,
                   elapsed_ms=int((time.time() - started) * 1000), ok=isinstance(holders, list),
                   row_count=len(holders) if isinstance(holders, list) else 0,
                   status_code=holder_status, body=holder_body)
        if not isinstance(holders, list):
            unresolved.append({"symbol": symbol, "reason": "holders_not_a_list"})
            continue
        kept = []
        for holder in holders:
            if not isinstance(holder, dict) or not isinstance(holder.get("address"), str):
                continue
            item = {"address": holder["address"]}
            share = holder.get("share")
            if isinstance(share, (int, float)) and not isinstance(share, bool):
                item["share"] = share
            if isinstance(holder.get("rawBalance"), str):
                item["raw_balance"] = holder["rawBalance"]
            kept.append(item)
        rows.append({"symbol": symbol, "gecko_id": gecko_id, "contract": address, "holders": kept,
                     "entity_adjusted": False, "eligible": False})
    observed = {"rows": rows, "unresolved": unresolved, "entity_adjusted": False,
                "eligible": False, "dispatched": False}
    pass
    return observed


PROTOCOL_URL = "https://api.llama.fi/protocol/{slug}"


def fetch_protocol_holder_rows(log: DataCallLog, identities: tuple[tuple[str, str, str], ...], *,
                               getter: Callable = requests.get, timeout: float = 20) -> dict:
    """Read holder rows from a protocol address only when its gecko id matches. Not entity-adjusted."""
    import re
    rows = []
    unresolved = []
    bare_address = re.compile(r"^0x[0-9a-fA-F]{40}$")
    for symbol, slug, gecko_id in identities:
        if not slug or "/" in slug:
            unresolved.append({"symbol": symbol, "reason": "no_protocol_slug"})
            continue
        started = time.time()
        protocol = None
        try:
            protocol = getter(PROTOCOL_URL.format(slug=slug), timeout=timeout,
                              headers={"User-Agent": "SURGE-paper-platform/1.0"})
            body = protocol.content
            status = protocol.status_code
            protocol.raise_for_status()
            payload = protocol.json()
        except Exception:
            status = getattr(protocol, "status_code", None)
            body = getattr(protocol, "content", b"") or b""
            log.record(source="defillama-protocol", symbol=symbol, timeframe=None, started_at=started,
                       elapsed_ms=int((time.time() - started) * 1000), ok=False, row_count=0,
                       status_code=status, body=body if isinstance(body, bytes) else b"")
            unresolved.append({"symbol": symbol, "reason": "protocol_request_failed", "status_code": status})
            continue
        log.record(source="defillama-protocol", symbol=symbol, timeframe=None, started_at=started,
                   elapsed_ms=int((time.time() - started) * 1000), ok=isinstance(payload, dict),
                   row_count=1, status_code=status, body=body)
        if not isinstance(payload, dict) or payload.get("gecko_id") != gecko_id:
            unresolved.append({"symbol": symbol, "reason": "gecko_mismatch"})
            continue
        address = payload.get("address")
        if not isinstance(address, str) or bare_address.fullmatch(address) is None:
            unresolved.append({"symbol": symbol, "reason": "non_ethereum_address"})
            continue
        started = time.time()
        holder = None
        try:
            holder = getter(ETHPLORER_HOLDERS_URL.format(address=address), params={"apiKey": "freekey", "limit": 10},
                            timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
            holder_body = holder.content
            holder_status = holder.status_code
            holder.raise_for_status()
            holder_payload = holder.json()
        except Exception:
            status = getattr(holder, "status_code", None)
            raw = getattr(holder, "content", b"") or b""
            log.record(source="ethplorer-holders", symbol=symbol, timeframe=None, started_at=started,
                       elapsed_ms=int((time.time() - started) * 1000), ok=False, row_count=0,
                       status_code=status, body=raw if isinstance(raw, bytes) else b"")
            unresolved.append({"symbol": symbol, "reason": "holder_request_failed", "status_code": status})
            continue
        holders = holder_payload.get("holders") if isinstance(holder_payload, dict) else None
        log.record(source="ethplorer-holders", symbol=symbol, timeframe=None, started_at=started,
                   elapsed_ms=int((time.time() - started) * 1000), ok=isinstance(holders, list),
                   row_count=len(holders) if isinstance(holders, list) else 0,
                   status_code=holder_status, body=holder_body)
        if not isinstance(holders, list):
            unresolved.append({"symbol": symbol, "reason": "holders_not_a_list"})
            continue
        kept = []
        for item in holders:
            if not isinstance(item, dict) or not isinstance(item.get("address"), str):
                continue
            kept_item = {"address": item["address"]}
            share = item.get("share")
            if isinstance(share, (int, float)) and not isinstance(share, bool):
                kept_item["share"] = share
            if isinstance(item.get("rawBalance"), str):
                kept_item["raw_balance"] = item["rawBalance"]
            kept.append(kept_item)
        rows.append({"symbol": symbol, "gecko_id": gecko_id, "slug": slug, "contract": address,
                     "holders": kept, "entity_adjusted": False, "eligible": False})
    observed = {"rows": rows, "unresolved": unresolved, "entity_adjusted": False,
                "eligible": False, "dispatched": False}
    pass
    return observed


SEARCH_URL = "https://api.coingecko.com/api/v3/search"
MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"




def fetch_global(log: DataCallLog, *, getter: Callable = requests.get, timeout: float = 12) -> dict:
    started = time.time()
    try:
        response = getter(GLOBAL_URL, timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
        body = response.content
        elapsed = int((time.time() - started) * 1000)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            log.record(source="coingecko-global", symbol=None, timeframe=None, started_at=started,
                       elapsed_ms=elapsed, ok=False, row_count=0, status_code=response.status_code,
                       error="missing data object", body=body)
            raise ValueError("CoinGecko global payload has no data object")
        market_cap = data.get("total_market_cap") if isinstance(data.get("total_market_cap"), dict) else {}
        dominance = data.get("market_cap_percentage") if isinstance(data.get("market_cap_percentage"), dict) else {}
        log.record(source="coingecko-global", symbol=None, timeframe=None, started_at=started,
                   elapsed_ms=elapsed, ok=True, row_count=1, status_code=response.status_code, body=body)
        observed = {
            "source": "coingecko-global",
            "source_url": GLOBAL_URL,
            "btc_dominance_pct": dominance.get("btc"),
            "eth_dominance_pct": dominance.get("eth"),
            "total_market_cap_usd": market_cap.get("usd"),
            "source_updated_at": data.get("updated_at") if type(data.get("updated_at")) is int else None,
            "missing": [name for name, value in (
                ("btc_dominance_pct", dominance.get("btc")),
                ("eth_dominance_pct", dominance.get("eth")),
                ("total_market_cap_usd", market_cap.get("usd")),
            ) if value is None],
        }
        pass
        return observed
    except Exception as exc:
        pass
        if not isinstance(exc, ValueError):
            log.record(source="coingecko-global", symbol=None, timeframe=None, started_at=started,
                       elapsed_ms=int((time.time() - started) * 1000), ok=False, row_count=0,
                       error=type(exc).__name__)
        raise


DOMINANCE_HISTORY_URL = "https://api.coinmarketcap.com/data-api/v3/global-metrics/quotes/historical"
GLOBAL_CHART_URL = "https://api.coingecko.com/api/v3/global/market_cap_chart"


def fetch_dominance_history(log: DataCallLog, *, getter: Callable = requests.get, timeout: float = 20) -> dict:
    """Read a 7-day dominance pair. The shared slot does not name BTC or ETH, so it stays invalid."""
    import json
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    chart_started = time.time()
    chart_status = None
    try:
        chart = getter(GLOBAL_CHART_URL, params={"days": "7"}, timeout=timeout,
                       headers={"User-Agent": "SURGE-paper-platform/1.0"})
        chart_status = chart.status_code
        log.record(source="coingecko-global-chart", symbol=None, timeframe="7d", started_at=chart_started,
                   elapsed_ms=int((time.time() - chart_started) * 1000), ok=chart.status_code == 200,
                   row_count=0, status_code=chart.status_code, body=chart.content)
    except Exception as exc:
        log.record(source="coingecko-global-chart", symbol=None, timeframe="7d", started_at=chart_started,
                   elapsed_ms=int((time.time() - chart_started) * 1000), ok=False, row_count=0,
                   error=type(exc).__name__)
    started = time.time()
    today = datetime.now(timezone.utc).date()
    response = getter(DOMINANCE_HISTORY_URL, params={
        "format": "chart", "interval": "1d", "convertId": "2781",
        "timeStart": (today - timedelta(days=7)).isoformat(), "timeEnd": today.isoformat()},
                      timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
    body = response.content
    response.raise_for_status()
    payload = json.loads(body, parse_float=Decimal)
    quotes = payload.get("data", {}).get("quotes") if isinstance(payload, dict) else None
    log.record(source="coinmarketcap-dominance-history", symbol=None, timeframe="1d", started_at=started,
               elapsed_ms=int((time.time() - started) * 1000), ok=isinstance(quotes, list),
               row_count=len(quotes) if isinstance(quotes, list) else 0,
               status_code=response.status_code, body=body)
    now = datetime.now(timezone.utc)
    parsed = []
    if isinstance(quotes, list):
        for row in quotes:
            if not isinstance(row, dict) or not isinstance(row.get("timestamp"), str):
                continue
            stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp > now:
                continue
            values = {}
            for name in ("btcDominance", "ethDominance"):
                value = row.get(name)
                if type(value) is int:
                    values[name] = Decimal(value)
                elif isinstance(value, Decimal):
                    values[name] = value
            if len(values) == 2:
                parsed.append((stamp, values))
    latest = max(parsed, default=None, key=lambda item: item[0])
    prior = None
    if latest is not None:
        target = latest[0] - timedelta(days=7)
        matches = [item for item in parsed if item[0] == target]
        prior = matches[0] if len(matches) == 1 else None
    rows = []
    valid = latest is not None and prior is not None
    if valid:
        for name, label in (("btcDominance", "BTC"), ("ethDominance", "ETH")):
            change = latest[1][name] - prior[1][name]
            rows.append({"asset": label, "latest_percent": format(latest[1][name], "f"),
                         "prior_percent": format(prior[1][name], "f"),
                         "percentage_point_change": format(change, "f"),
                         "fraction_change": format(change / Decimal(100), "f")})
    observed = {"source": "coinmarketcap-public-dominance-history", "source_url": DOMINANCE_HISTORY_URL,
                "coingecko_chart_status": chart_status,
                "latest_time": None if latest is None else latest[0].isoformat(),
                "prior_time": None if prior is None else prior[0].isoformat(),
                "rows": rows, "change_valid": valid, "unit_note": "Source values are percent. fraction_change is the percentage-point difference divided by 100.",
                "copied_to_state": False, "state_slot_dominance_change_7d": "left_invalid",
                "reason": "The shared dominance slot does not name BTC or ETH. CoinGecko market-cap history was not used.",
                "regime_valid": False, "eligible": False, "dispatched": False}
    pass
    return observed


STABLECOIN_URL = "https://stablecoins.llama.fi/stablecoins"


def fetch_stablecoins(log: DataCallLog, *, getter: Callable = requests.get, timeout: float = 20) -> dict:
    """Sum circulating.peggedUSD only on rows that contain it. This does not set a regime."""
    import json
    from decimal import Decimal
    started = time.time()
    try:
        response = getter(STABLECOIN_URL, timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
        body = response.content
        elapsed = int((time.time() - started) * 1000)
        response.raise_for_status()
        payload = json.loads(body, parse_float=Decimal)
        assets = payload.get("peggedAssets") if isinstance(payload, dict) else None
        if not isinstance(assets, list):
            log.record(source="defillama-stablecoins", symbol=None, timeframe=None, started_at=started,
                       elapsed_ms=elapsed, ok=False, row_count=0, status_code=response.status_code,
                       error="missing peggedAssets", body=body)
            raise ValueError("DefiLlama stablecoin payload has no peggedAssets list")
        current_sum = Decimal(0)
        current_rows = 0
        paired_current = Decimal(0)
        paired_prev = Decimal(0)
        paired_rows = 0
        without = 0
        for row in assets:
            circulating = row.get("circulating") if isinstance(row, dict) else None
            previous = row.get("circulatingPrevDay") if isinstance(row, dict) else None
            usd = circulating.get("peggedUSD") if isinstance(circulating, dict) else None
            prev_usd = previous.get("peggedUSD") if isinstance(previous, dict) else None
            if type(usd) in (int, Decimal):
                current_sum += Decimal(usd)
                current_rows += 1
            else:
                without += 1
            if type(usd) in (int, Decimal) and type(prev_usd) in (int, Decimal):
                paired_current += Decimal(usd)
                paired_prev += Decimal(prev_usd)
                paired_rows += 1
        change = None if paired_rows == 0 or paired_prev == 0 else (paired_current - paired_prev) / paired_prev
        log.record(source="defillama-stablecoins", symbol=None, timeframe=None, started_at=started,
                   elapsed_ms=elapsed, ok=True, row_count=current_rows, status_code=response.status_code, body=body)
        observed = {
            "source": "defillama-stablecoins",
            "source_url": STABLECOIN_URL,
            "pegged_usd_sum": str(current_sum) if current_rows else None,
            "rows_with_pegged_usd": current_rows,
            "rows_without_pegged_usd": without,
            "day_change_rows": paired_rows,
            "day_change_fraction": None if change is None else str(change),
            "regime_valid": False,
            "eligible": False,
            "dispatched": False,
        }
        pass
        return observed
    except Exception as exc:
        if not isinstance(exc, ValueError):
            log.record(source="defillama-stablecoins", symbol=None, timeframe=None, started_at=started,
                       elapsed_ms=int((time.time() - started) * 1000), ok=False, row_count=0,
                       error=type(exc).__name__)
        raise


STABLECOIN_CHART_URL = "https://stablecoins.llama.fi/stablecoincharts/all"
_WEEK_S = 7 * 86_400


def fetch_stablecoin_chart(log: DataCallLog, *, getter: Callable = requests.get, timeout: float = 30) -> dict:
    """Change in the chart's totalCirculatingUSD map across an exact 7-day pair. Not copied into state."""
    import json
    from decimal import Decimal
    started = time.time()
    response = getter(STABLECOIN_CHART_URL, timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
    body = response.content
    response.raise_for_status()
    payload = json.loads(body, parse_float=Decimal)
    points = payload if isinstance(payload, list) else []
    log.record(source="defillama-stablecoin-chart", symbol=None, timeframe="7d", started_at=started,
               elapsed_ms=int((time.time() - started) * 1000), ok=bool(points),
               row_count=len(points), status_code=response.status_code, body=body)
    now_s = int(time.time())
    parsed = []
    for point in points:
        if not isinstance(point, dict) or not isinstance(point.get("date"), str) or not point["date"].isdigit():
            continue
        stamp = int(point["date"])
        if stamp <= now_s:
            parsed.append((stamp, point))
    latest = max(parsed, default=None, key=lambda item: item[0])
    prior = None
    if latest is not None:
        prior = next((item for item in parsed if item[0] == latest[0] - _WEEK_S), None)

    def usd_map(point):
        raw = point.get("totalCirculatingUSD") if isinstance(point, dict) else None
        if not isinstance(raw, dict) or not raw:
            return None
        mapped = {}
        for key, value in raw.items():
            if type(value) is int:
                mapped[key] = Decimal(value)
            elif isinstance(value, Decimal):
                mapped[key] = value
            else:
                return None
        return mapped

    latest_map = usd_map(latest[1]) if latest is not None else None
    prior_map = usd_map(prior[1]) if prior is not None else None
    valid = latest_map is not None and prior_map is not None and set(latest_map) == set(prior_map) and sum(prior_map.values()) > 0
    change = None
    pegged_change = None
    if valid:
        change = (sum(latest_map.values()) - sum(prior_map.values())) / sum(prior_map.values())
        if prior_map.get("peggedUSD", 0) > 0 and "peggedUSD" in latest_map:
            pegged_change = (latest_map["peggedUSD"] - prior_map["peggedUSD"]) / prior_map["peggedUSD"]
    observed = {
        "source": "defillama-stablecoin-chart",
        "source_url": STABLECOIN_CHART_URL,
        "latest_time": None if latest is None else str(latest[0]),
        "prior_time": None if prior is None else str(prior[0]),
        "peg_keys": 0 if not valid else len(latest_map),
        "total_circulating_usd_change_7d": None if change is None else format(change, "f"),
        "pegged_usd_change_7d": None if pegged_change is None else format(pegged_change, "f"),
        "change_valid": valid,
        "copied_to_state": False,
        "state_slot_stablecoin_change_7d": "left_invalid",
        "reason": "The change sums totalCirculatingUSD across the same peg keys on both dates. The running state engine has not consumed it.",
        "regime_valid": False,
        "eligible": False,
        "dispatched": False,
    }
    pass
    return observed


def fetch_perp_quotes(log: DataCallLog, *, getter: Callable = requests.get, timeout: float = 15) -> dict:
    """Read public swap funding and open interest. A quote does not set the regime or send an order."""
    import json
    instruments = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
    started = time.time()
    binance_status = None
    try:
        refused = getter("https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"},
                         timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
        binance_status = refused.status_code
        log.record(source="binance-fapi-premium", symbol="BTCUSDT", timeframe=None, started_at=started,
                   elapsed_ms=int((time.time() - started) * 1000), ok=refused.status_code == 200,
                   row_count=0, status_code=refused.status_code, body=refused.content)
    except Exception as exc:
        log.record(source="binance-fapi-premium", symbol="BTCUSDT", timeframe=None, started_at=started,
                   elapsed_ms=int((time.time() - started) * 1000), ok=False, row_count=0, error=type(exc).__name__)
        binance_status = None
    rows = []
    for inst_id in instruments:
        funding_started = time.time()
        funding_response = getter("https://www.okx.com/api/v5/public/funding-rate", params={"instId": inst_id},
                                  timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
        funding_body = funding_response.content
        funding_response.raise_for_status()
        funding_payload = funding_response.json()
        funding_rows = funding_payload.get("data") if isinstance(funding_payload, dict) else None
        if not isinstance(funding_payload, dict) or funding_payload.get("code") != "0" or not isinstance(funding_rows, list) or not funding_rows:
            log.record(source="okx-funding", symbol=inst_id, timeframe=None, started_at=funding_started,
                       elapsed_ms=int((time.time() - funding_started) * 1000), ok=False, row_count=0,
                       status_code=funding_response.status_code, error="missing funding row", body=funding_body)
            raise ValueError("OKX funding payload has no row")
        log.record(source="okx-funding", symbol=inst_id, timeframe=None, started_at=funding_started,
                   elapsed_ms=int((time.time() - funding_started) * 1000), ok=True, row_count=1,
                   status_code=funding_response.status_code, body=funding_body)
        oi_started = time.time()
        oi_response = getter("https://www.okx.com/api/v5/public/open-interest", params={"instId": inst_id},
                             timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
        oi_body = oi_response.content
        oi_response.raise_for_status()
        oi_payload = oi_response.json()
        oi_rows = oi_payload.get("data") if isinstance(oi_payload, dict) else None
        if not isinstance(oi_payload, dict) or oi_payload.get("code") != "0" or not isinstance(oi_rows, list) or not oi_rows:
            log.record(source="okx-open-interest", symbol=inst_id, timeframe=None, started_at=oi_started,
                       elapsed_ms=int((time.time() - oi_started) * 1000), ok=False, row_count=0,
                       status_code=oi_response.status_code, error="missing open interest row", body=oi_body)
            raise ValueError("OKX open-interest payload has no row")
        log.record(source="okx-open-interest", symbol=inst_id, timeframe=None, started_at=oi_started,
                   elapsed_ms=int((time.time() - oi_started) * 1000), ok=True, row_count=1,
                   status_code=oi_response.status_code, body=oi_body)
        funding = funding_rows[0]
        oi = oi_rows[0]
        row = {
            "inst_id": inst_id,
            "funding_rate": funding.get("fundingRate") if isinstance(funding.get("fundingRate"), str) else None,
            "funding_time": funding.get("fundingTime") if isinstance(funding.get("fundingTime"), str) else None,
            "open_interest": oi.get("oi") if isinstance(oi.get("oi"), str) else None,
            "open_interest_usd": oi.get("oiUsd") if isinstance(oi.get("oiUsd"), str) else None,
            "open_interest_time": oi.get("ts") if isinstance(oi.get("ts"), str) else None,
        }
        row["missing"] = [name for name, value in row.items() if name != "inst_id" and value is None]
        rows.append(row)
    observed = {"source": "okx-public-swap", "binance_premium_status": binance_status, "rows": rows,
                "regime_valid": False, "eligible": False, "dispatched": False}
    pass
    return observed


OI_HISTORY_URL = "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history"
FUNDING_HISTORY_URL = "https://www.okx.com/api/v5/public/funding-rate-history"
_DAY_MS = 86_400_000
_FUNDING_GAP_MS = 28_800_000


def _decimal_text(value: object):
    from decimal import Decimal, InvalidOperation
    if not isinstance(value, str) or not value:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return number


def fetch_swap_history(log: DataCallLog, *, getter: Callable = requests.get, timeout: float = 20) -> dict:
    """Read OKX public history. An 8-hour funding gap is not a 1-hour change, and neither series is copied into state."""
    import json
    now_ms = int(time.time() * 1000)
    rows = []
    for inst_id in ("BTC-USDT-SWAP", "ETH-USDT-SWAP"):
        item = {"inst_id": inst_id, "oi_change_24h_valid": False, "funding_change_1h_valid": False,
                "copied_to_state": False}
        started = time.time()
        oi_response = getter(OI_HISTORY_URL, params={"instId": inst_id, "period": "1H"}, timeout=timeout,
                             headers={"User-Agent": "SURGE-paper-platform/1.0"})
        oi_body = oi_response.content
        oi_response.raise_for_status()
        oi_payload = oi_response.json()
        oi_rows = oi_payload.get("data") if isinstance(oi_payload, dict) else None
        oi_ok = isinstance(oi_payload, dict) and oi_payload.get("code") == "0" and isinstance(oi_rows, list)
        log.record(source="okx-open-interest-history", symbol=inst_id, timeframe="1H", started_at=started,
                   elapsed_ms=int((time.time() - started) * 1000), ok=oi_ok,
                   row_count=len(oi_rows) if isinstance(oi_rows, list) else 0,
                   status_code=oi_response.status_code, body=oi_body)
        parsed = []
        if oi_ok:
            for raw in oi_rows:
                if not isinstance(raw, list) or len(raw) != 4 or not isinstance(raw[0], str) or not raw[0].isdigit():
                    continue
                usd = _decimal_text(raw[3])
                if usd is None:
                    continue
                parsed.append((int(raw[0]), raw[3], usd))
        latest = max(parsed, default=None, key=lambda row: row[0])
        prior = None
        if latest is not None and latest[0] <= now_ms:
            prior = next((row for row in parsed if row[0] == latest[0] - _DAY_MS), None)
        if latest is not None and prior is not None and prior[2] > 0:
            change = (latest[2] - prior[2]) / prior[2]
            item.update({"oi_usd_latest": latest[1], "oi_usd_prior": prior[1],
                         "oi_latest_time": str(latest[0]), "oi_prior_time": str(prior[0]),
                         "oi_usd_change_24h": format(change, "f"), "oi_change_24h_valid": True})
        else:
            item["oi_gap"] = "missing_exact_24h_oi_usd"
        funding_started = time.time()
        funding_response = getter(FUNDING_HISTORY_URL, params={"instId": inst_id, "limit": "2"}, timeout=timeout,
                                  headers={"User-Agent": "SURGE-paper-platform/1.0"})
        funding_body = funding_response.content
        funding_response.raise_for_status()
        funding_payload = funding_response.json()
        funding_rows = funding_payload.get("data") if isinstance(funding_payload, dict) else None
        funding_ok = isinstance(funding_payload, dict) and funding_payload.get("code") == "0" and isinstance(funding_rows, list)
        log.record(source="okx-funding-history", symbol=inst_id, timeframe=None, started_at=funding_started,
                   elapsed_ms=int((time.time() - funding_started) * 1000), ok=funding_ok,
                   row_count=len(funding_rows) if isinstance(funding_rows, list) else 0,
                   status_code=funding_response.status_code, body=funding_body)
        settled = []
        if funding_ok:
            for raw in funding_rows:
                if not isinstance(raw, dict) or not isinstance(raw.get("fundingTime"), str) or not raw["fundingTime"].isdigit():
                    continue
                rate = _decimal_text(raw.get("fundingRate"))
                if rate is None:
                    continue
                settled.append((int(raw["fundingTime"]), raw["fundingRate"], rate))
        settled.sort(key=lambda row: row[0], reverse=True)
        if len(settled) >= 2 and settled[0][0] <= now_ms and settled[0][0] - settled[1][0] == _FUNDING_GAP_MS:
            item.update({"funding_rate_latest": settled[0][1], "funding_rate_prior": settled[1][1],
                         "funding_latest_time": str(settled[0][0]), "funding_prior_time": str(settled[1][0]),
                         "funding_gap_ms": _FUNDING_GAP_MS,
                         "funding_difference_8h": format(settled[0][2] - settled[1][2], "f")})
        else:
            item["funding_gap"] = "settled_gap_is_not_8h"
        rows.append(item)
    observed = {"source": "okx-public-history", "rows": rows,
                "state_slot_oi_change_24h": "left_invalid", "state_slot_funding_change": "left_invalid",
                "reason": "History is BTC-USDT-SWAP and ETH-USDT-SWAP. The shared slots do not name an instrument, and settled funding is 8 hours rather than 1 hour.",
                "regime_valid": False, "eligible": False, "dispatched": False}
    pass
    return observed


DERIBIT_BOOK_URL = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
_DERIBIT_FIELDS = ("open_interest", "funding_8h", "current_funding", "mark_price", "creation_timestamp")


def fetch_deribit_summaries(log: DataCallLog, *, getter: Callable = requests.get, timeout: float = 20) -> dict:
    """Read Deribit public perpetual summaries. A summary does not set the regime or send an order."""
    import json
    rows = []
    for currency in ("BTC", "ETH"):
        started = time.time()
        response = getter(DERIBIT_BOOK_URL, params={"currency": currency, "kind": "future"},
                          timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
        body = response.content
        elapsed = int((time.time() - started) * 1000)
        response.raise_for_status()
        payload = json.loads(body, parse_int=str, parse_float=str)
        result = payload.get("result") if isinstance(payload, dict) else None
        name = f"{currency}-PERPETUAL"
        perp = next((row for row in result if isinstance(row, dict) and row.get("instrument_name") == name), None) if isinstance(result, list) else None
        if perp is None:
            log.record(source="deribit-book-summary", symbol=name, timeframe=None, started_at=started,
                       elapsed_ms=elapsed, ok=False, row_count=0, status_code=response.status_code,
                       error="missing perpetual", body=body)
            raise ValueError("Deribit book summary has no perpetual")
        log.record(source="deribit-book-summary", symbol=name, timeframe=None, started_at=started,
                   elapsed_ms=elapsed, ok=True, row_count=1, status_code=response.status_code, body=body)
        row = {"instrument_name": name}
        for field in _DERIBIT_FIELDS:
            value = perp.get(field)
            row[field] = value if isinstance(value, str) else None
        row["missing"] = [field for field in _DERIBIT_FIELDS if row[field] is None]
        rows.append(row)
    observed = {"source": "deribit-public-perpetual", "source_url": DERIBIT_BOOK_URL, "rows": rows,
                "unit_note": "Values are the API strings. No unit was added.",
                "regime_valid": False, "eligible": False, "dispatched": False}
    pass
    return observed


FED_STATEMENT_URL = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm"
FED_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")


def fetch_fed_schedule(log: DataCallLog, *, getter: Callable = requests.get, timeout: float = 25) -> dict:
    """Read the cited FOMC statement and calendar. A rate decision does not set the crypto regime."""
    import re
    started = time.time()
    statement = getter(FED_STATEMENT_URL, timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
    statement_body = statement.content
    statement.raise_for_status()
    calendar_started = time.time()
    calendar = getter(FED_CALENDAR_URL, timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
    calendar_body = calendar.content
    calendar.raise_for_status()
    log.record(source="fed-statement", symbol=None, timeframe=None, started_at=started,
               elapsed_ms=int((calendar_started - started) * 1000), ok=True, row_count=1,
               status_code=statement.status_code, body=statement_body)
    log.record(source="fed-calendar", symbol=None, timeframe=None, started_at=calendar_started,
               elapsed_ms=int((time.time() - calendar_started) * 1000), ok=True, row_count=1,
               status_code=calendar.status_code, body=calendar_body)

    def plain(html: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

    statement_text = plain(statement.text)
    decision_match = re.search(r"The Committee decided to raise the target range[^.]+\.", statement_text)
    decision = decision_match.group(0) if decision_match else None
    release_dates = set(re.findall(r"September \d{1,2}, 2026", statement_text))
    release_times = set(re.findall(r"For release at \d{1,2}:\d{2} [ap]\.m\. EDT", statement_text))
    calendar_text = plain(calendar.text)
    start = calendar_text.find("2026 FOMC Meetings")
    end = calendar_text.find("2025 FOMC Meetings", start + 1) if start >= 0 else -1
    chunk = calendar_text[start:end] if start >= 0 and end > start else ""
    meeting_re = re.compile(r"(" + "|".join(_MONTHS) + r") (\d{1,2}(?:-\d{1,2})?)(\*)?")
    found = list(meeting_re.finditer(chunk))
    meetings = []
    for index, match in enumerate(found):
        before = chunk[max(0, match.start() - 24):match.start()]
        after = chunk[match.end():match.end() + 8]
        if before.endswith("Released ") or after.startswith(", 2026"):
            continue
        nxt = found[index + 1].start() if index + 1 < len(found) else len(chunk)
        meetings.append({"label": match.group(0), "statement_linked": "Statement:" in chunk[match.end():nxt]})
    observed = {
        "source": "federal-reserve",
        "statement_url": FED_STATEMENT_URL,
        "calendar_url": FED_CALENDAR_URL,
        "decision_sentence": decision,
        "release_date": next(iter(release_dates)) if len(release_dates) == 1 else None,
        "release_time": next(iter(release_times)) if len(release_times) == 1 else None,
        "meetings_2026": meetings,
        "without_statement_link": [row["label"] for row in meetings if not row["statement_linked"]],
        "regime_valid": False, "eligible": False, "dispatched": False,
    }
    pass
    return observed


def fetch_listed_markets(log: DataCallLog, symbols: tuple[str, ...], *, getter: Callable = requests.get,
                         timeout: float = 12) -> dict:
    """Resolve exact CoinGecko symbols and store only market fields the payload contains."""
    found = {}
    missing = []
    for symbol in symbols:
        started = time.time()
        response = getter(SEARCH_URL, params={"query": symbol}, timeout=timeout,
                          headers={"User-Agent": "SURGE-paper-platform/1.0"})
        elapsed = int((time.time() - started) * 1000)
        response.raise_for_status()
        payload = response.json()
        coins = payload.get("coins") if isinstance(payload, dict) else None
        match = None
        if isinstance(coins, list):
            for coin in coins:
                if isinstance(coin, dict) and str(coin.get("symbol", "")).upper() == symbol.upper() and coin.get("id"):
                    match = {"id": coin["id"], "name": coin.get("name")}
                    break
        log.record(source="coingecko-search", symbol=symbol, timeframe=None, started_at=started,
                   elapsed_ms=elapsed, ok=match is not None, row_count=1 if match else 0,
                   status_code=response.status_code, error=None if match else "no exact symbol", body=response.content)
        if match is None:
            missing.append(symbol)
        else:
            found[symbol.upper()] = match
    rows = []
    if found:
        started = time.time()
        ids = ",".join(item["id"] for item in found.values())
        response = getter(MARKETS_URL, params={"vs_currency": "usd", "ids": ids}, timeout=timeout,
                          headers={"User-Agent": "SURGE-paper-platform/1.0"})
        elapsed = int((time.time() - started) * 1000)
        response.raise_for_status()
        payload = response.json()
        log.record(source="coingecko-markets", symbol=ids, timeframe=None, started_at=started,
                   elapsed_ms=elapsed, ok=isinstance(payload, list),
                   row_count=len(payload) if isinstance(payload, list) else 0,
                   status_code=response.status_code, body=response.content)
        by_id = {row.get("id"): row for row in payload if isinstance(row, dict)} if isinstance(payload, list) else {}
        for symbol, item in found.items():
            row = by_id.get(item["id"], {})
            kept = {"symbol": symbol, "id": item["id"], "name": item["name"]}
            absent = []
            for field in ("current_price", "market_cap", "total_volume", "circulating_supply", "total_supply", "max_supply"):
                value = row.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    absent.append(field)
                else:
                    kept[field] = value
            kept["absent"] = absent
            rows.append(kept)
    observed = {"source_url": MARKETS_URL, "rows": rows, "unresolved": missing}
    pass
    pass
    return observed


BOOK_URL = "https://api.exchange.coinbase.com/products/{product}/book"


def fetch_coinbase_book(log: DataCallLog, product: str = "BTC-USD", *, level: int = 1, getter: Callable = requests.get,
                        timeout: float = 12) -> dict:
    """Read one public Coinbase book. This does not place an order."""
    if not product or "/" in product or level not in (1, 2):
        raise ValueError("invalid product")
    url = BOOK_URL.format(product=product)
    started = time.time()
    response = getter(url, params={"level": level}, timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
    elapsed = int((time.time() - started) * 1000)
    response.raise_for_status()
    payload = response.json()
    log.record(source="coinbase-book", symbol=product, timeframe=None, started_at=started, elapsed_ms=elapsed,
               ok=isinstance(payload, dict), row_count=1, status_code=response.status_code, body=response.content)
    if not isinstance(payload, dict):
        raise ValueError("Coinbase book payload is not an object")

    def levels(name):
        rows = payload.get(name)
        parsed = []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, list) and len(row) >= 2:
                    parsed.append((float(row[0]), float(row[1])))
        return parsed

    observed = {"source_url": url, "product": product, "sequence": payload.get("sequence"),
                "exchange_time": payload.get("time"), "bids": levels("bids"), "asks": levels("asks"),
                "dispatched": False}
    pass
    return observed


PROTOCOLS_URL = "https://api.llama.fi/protocols"


def fetch_listed_tvl(log: DataCallLog, ids: dict[str, str], *, getter: Callable = requests.get,
                     timeout: float = 20) -> dict:
    """Keep a TVL row only when its gecko id matches one CoinGecko id. This does not make it eligible."""
    started = time.time()
    response = getter(PROTOCOLS_URL, timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
    elapsed = int((time.time() - started) * 1000)
    response.raise_for_status()
    payload = response.json()
    log.record(source="defillama-protocols", symbol=",".join(ids), timeframe=None, started_at=started,
               elapsed_ms=elapsed, ok=isinstance(payload, list), row_count=len(payload) if isinstance(payload, list) else 0,
               status_code=response.status_code, body=response.content)
    if not isinstance(payload, list):
        raise ValueError("DefiLlama protocols payload is not a list")
    rows = []
    unresolved = []
    for symbol, gecko_id in ids.items():
        matches = [row for row in payload if isinstance(row, dict) and row.get("gecko_id") == gecko_id]
        if len(matches) != 1:
            unresolved.append({"symbol": symbol, "gecko_id": gecko_id, "matches": len(matches)})
            continue
        row = matches[0]
        kept = {"symbol": symbol, "slug": row.get("slug"), "name": row.get("name"), "eligible": False}
        if isinstance(row.get("tvl"), (int, float)):
            kept["tvl_usd"] = row["tvl"]
        else:
            kept["tvl_absent"] = True
        rows.append(kept)
    unjoined = []
    for symbol, gecko_id in ids.items():
        for row in payload:
            if not isinstance(row, dict) or str(row.get("symbol") or "").upper() != symbol.upper():
                continue
            if row.get("gecko_id") == gecko_id:
                continue
            item = {"symbol": symbol, "slug": row.get("slug"), "name": row.get("name"),
                    "gecko_id": row.get("gecko_id"), "joined": False}
            if isinstance(row.get("tvl"), (int, float)) and not isinstance(row.get("tvl"), bool):
                item["tvl_usd"] = row["tvl"]
            else:
                item["tvl_absent"] = True
            unjoined.append(item)
    parent_slugs = {}
    for symbol in ids:
        found = []
        for row in payload:
            if not isinstance(row, dict) or str(row.get("symbol") or "").upper() != symbol.upper():
                continue
            parent = row.get("parentProtocol")
            if not isinstance(parent, str) or not parent.startswith("parent#"):
                continue
            slug = parent[len("parent#"):]
            if slug and "/" not in slug and slug not in found:
                found.append(slug)
        parent_slugs[symbol] = found
    observed = {"source_url": PROTOCOLS_URL, "rows": rows, "unresolved": unresolved,
                "unjoined_same_symbol": unjoined, "parent_slugs": parent_slugs,
                "eligible": False, "dispatched": False}
    pass
    pass
    return observed


FEES_URL = "https://api.llama.fi/summary/fees/{slug}"


def fetch_protocol_fees(log: DataCallLog, slug: str, gecko_id: str, *, getter: Callable = requests.get,
                        timeout: float = 20) -> dict:
    """Read fee totals for one slug only when its gecko id matches. Fees do not confer eligibility."""
    if not slug or "/" in slug or not gecko_id:
        raise ValueError("invalid fee identity")
    url = FEES_URL.format(slug=slug)
    started = time.time()
    response = getter(url, timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
    elapsed = int((time.time() - started) * 1000)
    response.raise_for_status()
    payload = response.json()
    log.record(source="defillama-fees", symbol=slug, timeframe=None, started_at=started, elapsed_ms=elapsed,
               ok=isinstance(payload, dict), row_count=1, status_code=response.status_code, body=response.content)
    if not isinstance(payload, dict) or payload.get("gecko_id") != gecko_id:
        raise ValueError("fee payload gecko id does not match")
    kept = {"slug": slug, "gecko_id": gecko_id, "name": payload.get("name"), "eligible": False, "dispatched": False}
    absent = []
    for field in ("total24h", "total7d", "total30d", "totalAllTime"):
        value = payload.get(field)
        if isinstance(value, (int, float)):
            kept[field] = value
        else:
            absent.append(field)
    kept["absent"] = absent
    method = payload.get("methodology") if isinstance(payload.get("methodology"), dict) else {}
    kept["methodology"] = {key: value for key, value in method.items() if isinstance(value, str)}
    kept["numeric_holder_fields"] = [key for key in payload if "holder" in key.lower() and isinstance(payload[key], (int, float))]
    pass
    return kept


HOLDER_REVENUE_URL = "https://api.llama.fi/overview/fees"


def fetch_holder_revenue(log: DataCallLog, slugs: tuple[str, ...], *, getter: Callable = requests.get,
                         timeout: float = 30) -> dict:
    """Read dailyHoldersRevenue totals for already identified slugs. A number does not confer eligibility."""
    started = time.time()
    response = getter(HOLDER_REVENUE_URL, params={"excludeTotalDataChart": "true",
                      "excludeTotalDataChartBreakdown": "true", "dataType": "dailyHoldersRevenue"},
                      timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
    elapsed = int((time.time() - started) * 1000)
    response.raise_for_status()
    payload = response.json()
    protocols = payload.get("protocols") if isinstance(payload, dict) else None
    log.record(source="defillama-holder-revenue", symbol=",".join(slugs), timeframe="dailyHoldersRevenue",
               started_at=started, elapsed_ms=elapsed, ok=isinstance(protocols, list),
               row_count=len(protocols) if isinstance(protocols, list) else 0,
               status_code=response.status_code, body=response.content)
    if not isinstance(protocols, list):
        raise ValueError("holder revenue payload has no protocol list")
    rows = []
    unresolved = []
    for slug in slugs:
        matches = [row for row in protocols if isinstance(row, dict) and (row.get("slug") or row.get("module")) == slug]
        if len(matches) != 1:
            unresolved.append({"slug": slug, "matches": len(matches)})
            continue
        row = matches[0]
        kept = {"slug": slug, "gecko_id": row.get("gecko_id"), "name": row.get("name"),
                "eligible": False, "data_type": "dailyHoldersRevenue"}
        absent = []
        for field in ("total24h", "total7d", "total30d"):
            value = row.get(field)
            if isinstance(value, (int, float)):
                kept[field] = value
            else:
                absent.append(field)
        kept["absent"] = absent
        rows.append(kept)
    observed = {"source_url": HOLDER_REVENUE_URL, "rows": rows, "unresolved": unresolved,
                "eligible": False, "dispatched": False}
    pass
    return observed


EMISSIONS_URL = "https://api.llama.fi/emissions"


def fetch_unlock_status(log: DataCallLog, *, getter: Callable = requests.get, timeout: float = 20) -> dict:
    """Record whether the public emissions document is available. A refusal is not a zero unlock."""
    started = time.time()
    status_code = None
    body = b""
    try:
        response = getter(EMISSIONS_URL, timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
        status_code = response.status_code
        body = response.content
    except Exception:
        status_code = None
    log.record(source="defillama-emissions", symbol=None, timeframe=None, started_at=started,
               elapsed_ms=int((time.time() - started) * 1000), ok=status_code == 200,
               row_count=0, status_code=status_code, body=body)
    observed = {"source_url": EMISSIONS_URL, "status_code": status_code, "unlocks_present": False,
                "rows": [], "eligible": False, "dispatched": False,
                "note": "No unlock rows were parsed. A non-200 response does not become a zero unlock."}
    pass
    return observed


CIRCLE_URL = "https://www.circle.com/transparency"
_CIRCLE_IDS = ("usdc-in-circulation", "usdc-issued-7", "usdc-redeemed-7", "usdc-issued-30", "usdc-redeemed-30")


def fetch_circle_transparency(log: DataCallLog, *, getter: Callable = requests.get, timeout: float = 20) -> dict:
    """Read issuer USDC disclosure attributes. This does not set a regime or admit a token."""
    import re
    from html.parser import HTMLParser
    started = time.time()
    response = getter(CIRCLE_URL, timeout=timeout, headers={"User-Agent": "SURGE-paper-platform/1.0"})
    body = response.content
    elapsed = int((time.time() - started) * 1000)
    response.raise_for_status()
    text = response.text

    class _Spans(HTMLParser):
        def __init__(self):
            super().__init__()
            self.found = {}
        def handle_starttag(self, tag, attrs):
            attr = dict(attrs)
            element_id = attr.get("id")
            if element_id in _CIRCLE_IDS and element_id not in self.found:
                self.found[element_id] = {"data_point": attr.get("data-point"), "data_coin": attr.get("data-coin")}

    parser = _Spans()
    parser.feed(text)
    points = {}
    missing = []
    for element_id in _CIRCLE_IDS:
        item = parser.found.get(element_id)
        point = None if item is None else item.get("data_point")
        coin = None if item is None else item.get("data_coin")
        if not isinstance(point, str) or not re.fullmatch(r"\d+(\.\d+)?", point) or coin != "usdc":
            missing.append(element_id)
            continue
        points[element_id] = point
    labels = set(re.findall(r"As of [A-Z][a-z]{2} \d{1,2}, \d{4}", text))
    as_of = next(iter(labels)) if len(labels) == 1 else None
    log.record(source="circle-transparency", symbol="USDC", timeframe=None, started_at=started,
               elapsed_ms=elapsed, ok=not missing, row_count=len(points), status_code=response.status_code,
               error=None if not missing else "missing " + ",".join(missing), body=body)
    observed = {"source": "circle-transparency", "source_url": CIRCLE_URL, "as_of_label": as_of,
                "points": points, "missing": missing,
                "unit_note": "Page formatter maps usdc to billions of USD. Values are the data-point strings.",
                "regime_valid": False, "eligible": False, "dispatched": False}
    pass
    return observed
