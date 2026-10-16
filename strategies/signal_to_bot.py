"""Executable EMA crossover strategy used by the SURGE paper-trading app."""
from __future__ import annotations

import time

BUILT_IN_STRATEGIES = [{
    "name": "ema_12_26_crossover",
    "type": "trend_following",
    "indicators": {"ema_fast": {"period": 12}, "ema_slow": {"period": 26}},
    "status": "implemented",
}]


def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    alpha = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for value in values[period:]:
        result.append(value * alpha + result[-1] * (1 - alpha))
    return result


def generate_signal(strategy_name: str, symbol: str, closes: list[float]) -> dict:
    if strategy_name != "ema_12_26_crossover":
        raise ValueError(f"Strategy is not implemented: {strategy_name}")
    if any(value <= 0 for value in closes):
        raise ValueError("Close prices must be positive")
    fast, slow = _ema(closes, 12), _ema(closes, 26)
    if len(fast) < 2 or len(slow) < 2:
        direction, strength, reason = "neutral", 0.0, "Need at least 27 hourly candles"
    else:
        diff_now = fast[-1] - slow[-1]
        diff_prev = fast[-2] - slow[-2]
        strength = min(1.0, abs(diff_now / max(abs(slow[-1]), 1e-12)) * 100)
        if diff_prev <= 0 < diff_now:
            direction, reason = "long", "EMA 12 crossed above EMA 26"
        elif diff_prev >= 0 > diff_now:
            direction, reason = "exit", "EMA 12 crossed below EMA 26"
        else:
            direction, reason = "neutral", "No new crossover"
    return {"strategy": strategy_name, "symbol": symbol, "direction": direction,
            "strength": strength, "reference_price": closes[-1] if closes else None,
            "reason": reason, "generated_at": time.time()}


if __name__ == "__main__":
    sample = [50_000 + i * 12 + (i % 5) * 40 for i in range(100)]
    print(generate_signal("ema_12_26_crossover", "BTC-USD", sample))
