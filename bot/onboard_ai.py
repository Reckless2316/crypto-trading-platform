"""Local, explainable strategy ensemble for trading decision support.

This is a deterministic onboard engine: it performs no network/model calls and
never submits orders. Automated execution belongs to the caller and must pass
through the account risk gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from strategies.strategy_suite import STRATEGIES, MarketData


DEFAULT_WEIGHTS = {
    "ema_12_26_crossover": 1.0,
    "bollinger_mean_reversion": 0.8,
    "rsi_reversal": 0.8,
    "macd_crossover": 1.0,
    "donchian_breakout": 0.8,
    "markov_regime": 0.8,
    # Elliott counts are subjective, so they have deliberately little weight.
    "elliott_wave_heuristic": 0.2,
}


@dataclass(frozen=True)
class OnboardDecision:
    action: str
    score: float
    confidence: float
    signals: list[dict]
    explanation: str

    def as_dict(self) -> dict:
        return {
            "engine": "local_strategy_ensemble",
            "action": self.action,
            "score": self.score,
            "confidence": self.confidence,
            "signals": self.signals,
            "explanation": self.explanation,
            "execution": "decision_only",
        }


class OnboardTradingAgent:
    """Combine locally calculated strategy signals into a cautious decision."""

    def __init__(self, weights: dict[str, float] | None = None, threshold: float = 0.18):
        self.weights = dict(DEFAULT_WEIGHTS if weights is None else weights)
        if any(name not in STRATEGIES for name in self.weights):
            raise ValueError("Weights contain an unknown strategy")
        if any(value < 0 for value in self.weights.values()) or not any(self.weights.values()):
            raise ValueError("Strategy weights must be nonnegative with a positive total")
        if not 0 < threshold <= 1:
            raise ValueError("threshold must be between 0 and 1")
        self.threshold = threshold

    def analyze(self, symbol: str, closes: list[float], *, highs: list[float] | None = None,
                lows: list[float] | None = None, strategies: Iterable[str] | None = None) -> dict:
        selected = list(strategies) if strategies is not None else list(self.weights)
        if not selected:
            raise ValueError("At least one strategy is required")
        market = MarketData(symbol=symbol, closes=closes, highs=highs, lows=lows)
        reports = []
        signed_total = 0.0
        weight_total = 0.0
        for name in selected:
            if name not in STRATEGIES:
                raise ValueError(f"Strategy is not implemented: {name}")
            weight = self.weights.get(name, 1.0)
            signal = STRATEGIES[name](market)
            reports.append(signal)
            signed = 1.0 if signal["direction"] == "long" else -1.0 if signal["direction"] == "exit" else 0.0
            signed_total += signed * float(signal["strength"]) * weight
            weight_total += weight
        score = signed_total / weight_total if weight_total else 0.0
        score = max(-1.0, min(1.0, score))
        if score >= self.threshold:
            action = "buy"
        elif score <= -self.threshold:
            action = "sell_or_reduce"
        else:
            action = "hold"
        confidence = min(1.0, abs(score))
        explanation = f"{action} from {len(reports)} local signals; signed consensus {score:+.2f}."
        if action == "hold":
            explanation += " Consensus did not reach the configured threshold."
        return OnboardDecision(action, score, confidence, reports, explanation).as_dict()

