"""Fail-closed paper-order risk checks. Runtime state is supplied by the caller."""
from __future__ import annotations

import time
from typing import Any


class RiskGates:
    def __init__(self, account_config: dict[str, Any]):
        self.max_position_pct = float(account_config.get("max_position_pct", 5.0))
        self.max_daily_loss_pct = float(account_config.get("max_daily_loss_pct", 3.0))
        self.max_open_positions = int(account_config.get("max_open_positions", 3))

    def evaluate(self, request: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        reasons = []
        now = float(request.get("now", time.time()))
        armed_until = float(request.get("armed_until", 0))
        if request.get("kill_switch", True):
            reasons.append("Kill switch is ON")
        if armed_until <= now:
            reasons.append("Paper trading is disarmed or the deadman timer expired")
        equity = max(0.0, float(request.get("starting_equity", 0)))
        realized = float(request.get("daily_realized_pnl", 0))
        if equity and realized <= -(equity * self.max_daily_loss_pct / 100):
            reasons.append("Daily realized-loss limit reached")
        if request.get("side") == "buy":
            if request.get("is_new_position") and int(request.get("open_positions", 0)) >= self.max_open_positions:
                reasons.append("Maximum number of open positions reached")
            exposure = float(request.get("current_position_value", 0)) + float(request.get("order_value", 0))
            if equity and exposure > equity * self.max_position_pct / 100 + 1e-8:
                reasons.append("Per-symbol position limit exceeded")
            if float(request.get("order_value", 0)) > float(request.get("available_cash", 0)):
                reasons.append("Insufficient available cash")
        details = {"allowed": not reasons, "reasons": reasons, "max_position_pct": self.max_position_pct,
                   "max_daily_loss_pct": self.max_daily_loss_pct, "max_open_positions": self.max_open_positions,
                   "deadman_seconds_remaining": max(0, int(armed_until-now))}
        return not reasons, "; ".join(reasons) if reasons else "All gates open", details

    @staticmethod
    def deadman_check(last_renewal_ts: float, timeout_min: int = 30, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) - last_renewal_ts <= timeout_min * 60
