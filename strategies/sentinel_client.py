"""Read-only client for the SURGE Sentinel engine (``sentinel serve``, Go).

Sentinel runs as its own local process and exposes JSON over HTTP. This client
reads its live briefs and calls its analysis tools. It never sends credentials,
never places orders, and every Sentinel signal is advisory: execution remains
with the authority process and its risk gates.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8090"
_ALLOWED_TOOLS = {
    "analyze_symbol", "scan_market", "get_readings", "volatility_forecast", "model_report",
    "compute_indicator", "candlestick_patterns", "derivatives_positioning", "options_sentiment", "signal_ledger",
}


class SentinelUnavailable(RuntimeError):
    """Sentinel is not running or returned an error."""


class SentinelClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 30.0):
        url = (base_url or os.environ.get("SENTINEL_URL") or DEFAULT_URL).rstrip("/")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("SENTINEL_URL must be an http(s) URL")
        self.base_url = url
        self.token = token if token is not None else os.environ.get("SENTINEL_TOKEN") or None
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - scheme validated above
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode()).get("error", exc.reason)
            except Exception:
                detail = exc.reason
            raise SentinelUnavailable(f"sentinel {path}: HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SentinelUnavailable(f"sentinel {path}: {type(exc).__name__}") from None

    def health(self) -> dict:
        return self._request("GET", "/healthz")

    def briefs(self) -> list[dict]:
        """Latest brief per symbol in the live universe."""
        return self._request("GET", "/api/briefs")

    def snapshot(self, symbol: str) -> dict:
        return self._request("GET", "/api/snapshot/" + urllib.parse.quote(symbol.upper(), safe="-"))

    def tool(self, name: str, **arguments: Any) -> Any:
        if name not in _ALLOWED_TOOLS:
            raise ValueError(f"unknown or disallowed Sentinel tool: {name}")
        return self._request("POST", "/api/tools/" + name, arguments)


def to_signal(brief: dict) -> dict:
    """Map a Sentinel brief onto the platform's advisory signal shape."""
    sig = brief.get("signal", {})
    action = sig.get("action", "NEUTRAL")
    direction = {"LONG": "long", "SHORT": "exit"}.get(action, "neutral")  # OKX US is spot-only
    actionable = bool(sig.get("actionable")) and direction != "neutral"
    return {
        "strategy": "sentinel",
        "symbol": brief.get("symbol"),
        "direction": direction if actionable else "neutral",
        "strength": round(min(1.0, abs(float(brief.get("sentinel_score", 0.0))) / 100), 4),
        "confidence": sig.get("confidence", 0.0),
        "tier": sig.get("tier", "-"),
        "edge_status": sig.get("edge_status", "n/a"),
        "reason": sig.get("summary", ""),
        "bar_time": brief.get("bar_time"),
        "advisory_only": True,
    }
