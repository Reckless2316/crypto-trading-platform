"""Read-only check of the configured live venue's API credentials. It never places an order.

    python -m bot.venue_check            # reads SURGE_* settings from the environment, then .env

Reports whether the key authenticates, its permissions (and warns on withdraw or a missing IP
allowlist), the account's actual fee tier, and which assets hold a balance. It prints no
secret material. Live trading flags are ignored: only the CCXT client is built.
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from bot.live_coinbase_adapter import LiveConfig, LiveTradingDisabled, _ccxt_client

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def read_env(path: Path = ENV_FILE, environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """SURGE_* settings from .env, overridden by the process environment."""
    values: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                if key.strip().startswith("SURGE_"):
                    values[key.strip()] = value.strip()
    except OSError:
        pass
    values.update({k: v for k, v in environ.items() if k.startswith("SURGE_") and v})
    return values


def check(env: Mapping[str, str], client_factory: Callable[[LiveConfig], Any] = _ccxt_client,
          symbol: str = "BTC/USDT") -> list[str]:
    config = replace(LiveConfig.from_env({**env, "SURGE_LIVE_MAX_ORDER_QUOTE": "0"}), enabled=False, armed=False)
    if not config.api_key or not config.api_secret:
        raise LiveTradingDisabled(f"no API key/secret configured for {config.exchange_id}")
    client = client_factory(config)
    lines = [f"venue: {config.exchange_id} ({getattr(client, 'hostname', None) or 'default host'})"]
    if config.exchange_id in {"okx", "okxus"}:
        account = client.privateGetAccountConfig()["data"][0]   # raises on a bad key, secret, passphrase or host
        perms = account.get("perm", "")
        lines.append(f"authenticated; permissions: {perms}; account level {account.get('acctLv')}")
        if "withdraw" in perms:
            lines.append("WARNING: this key can withdraw funds. Recreate it with Read + Trade only.")
        if not account.get("ip"):
            lines.append("note: no IP allowlist. OKX recommends binding trade keys to an IP; unbound trade keys "
                         "can be deleted after a period of inactivity.")
    fee = client.fetch_trading_fee(symbol)
    lines.append(f"{symbol} fee: maker {fee['maker']:.4%}, taker {fee['taker']:.4%}")
    balances = {k: v for k, v in client.fetch_balance().get("total", {}).items() if v}
    lines.append(f"assets with a balance: {sorted(balances) or 'none'}")
    return lines


def main() -> int:
    try:
        for line in check(read_env()):
            print(line)
    except Exception as exc:  # report the failure class and message, never the request headers
        print(f"FAILED: {type(exc).__name__}: {str(exc)[:300]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
