"""Composable, close-series strategy signals for the SURGE paper trading app.

Signals are research outputs, not profit guarantees or trade instructions. The
module deliberately has no exchange or order-submission dependencies.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import time
from typing import Callable

try:
    from prediction.markov_prototype import CryptoRegimeHMM
except ImportError:  # Supports running this file directly from the strategies directory.
    from ..prediction.markov_prototype import CryptoRegimeHMM

try:
    from prediction.emission_model import classify_observation
except ImportError:
    from ..prediction.emission_model import classify_observation


@dataclass(frozen=True)
class MarketData:
    symbol: str
    closes: list[float]
    highs: list[float] | None = None
    lows: list[float] | None = None


def _check(data: MarketData, minimum: int = 1) -> None:
    if not data.symbol:
        raise ValueError("Symbol is required")
    if len(data.closes) < minimum:
        raise ValueError(f"Need at least {minimum} close prices")
    if any(not math.isfinite(value) or value <= 0 for value in data.closes):
        raise ValueError("Close prices must be finite and positive")
    for label, values in (("highs", data.highs), ("lows", data.lows)):
        if values is not None and (len(values) != len(data.closes) or any(
                not math.isfinite(value) or value <= 0 for value in values)):
            raise ValueError(f"{label} must contain one finite positive value per close")


def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    alpha = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for value in values[period:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def _signal(name: str, data: MarketData, direction: str, strength: float,
            reason: str, **details: object) -> dict:
    return {"strategy": name, "symbol": data.symbol, "direction": direction,
            "strength": max(0.0, min(1.0, float(strength))),
            "reference_price": data.closes[-1] if data.closes else None,
            "reason": reason, "generated_at": time.time(), **details}


def ema_crossover(data: MarketData) -> dict:
    _check(data)
    fast, slow = _ema(data.closes, 12), _ema(data.closes, 26)
    if len(fast) < 2 or len(slow) < 2:
        return _signal("ema_12_26_crossover", data, "neutral", 0,
                       "Need at least 27 candles to confirm a crossover")
    now, previous = fast[-1] - slow[-1], fast[-2] - slow[-2]
    strength = min(1, abs(now / slow[-1]) * 100)
    if previous <= 0 < now:
        direction, reason = "long", "EMA 12 crossed above EMA 26"
    elif previous >= 0 > now:
        direction, reason = "exit", "EMA 12 crossed below EMA 26"
    else:
        direction, reason = "neutral", "No new EMA crossover"
    return _signal("ema_12_26_crossover", data, direction, strength, reason)


def bollinger_mean_reversion(data: MarketData, period: int = 20, deviations: float = 2) -> dict:
    _check(data, period)
    if period < 2 or deviations <= 0:
        raise ValueError("Bollinger period must be >= 2 and deviations positive")
    sample = data.closes[-period:]
    mean = sum(sample) / period
    sd = math.sqrt(sum((x - mean) ** 2 for x in sample) / period)
    lower, upper = mean - deviations * sd, mean + deviations * sd
    price = sample[-1]
    if sd == 0:
        direction, strength, reason = "neutral", 0, "No price dispersion in Bollinger window"
    elif price < lower:
        direction, strength, reason = "long", min(1, (lower - price) / (deviations * sd)), "Close below lower Bollinger band"
    elif price > upper:
        direction, strength, reason = "exit", min(1, (price - upper) / (deviations * sd)), "Close above upper Bollinger band"
    else:
        direction, strength, reason = "neutral", 0, "Close is within Bollinger bands"
    return _signal("bollinger_mean_reversion", data, direction, strength, reason,
                   lower_band=lower, middle_band=mean, upper_band=upper)


def rsi_reversal(data: MarketData, period: int = 14, oversold: float = 30,
                 overbought: float = 70) -> dict:
    _check(data, period + 1)
    if period < 2 or not 0 <= oversold < overbought <= 100:
        raise ValueError("Invalid RSI period or thresholds")
    changes = [b - a for a, b in zip(data.closes[-period-1:-1], data.closes[-period:])]
    gains = sum(max(x, 0) for x in changes) / period
    losses = sum(max(-x, 0) for x in changes) / period
    rsi = 100.0 if losses == 0 and gains > 0 else 50.0 if losses == gains == 0 else (
        0.0 if gains == 0 else 100 - 100 / (1 + gains / losses))
    if rsi <= oversold:
        direction, strength, reason = "long", (oversold - rsi) / max(oversold, 1), "RSI is at or below oversold threshold"
    elif rsi >= overbought:
        direction, strength, reason = "exit", (rsi - overbought) / max(100 - overbought, 1), "RSI is at or above overbought threshold"
    else:
        direction, strength, reason = "neutral", 0, "RSI is between thresholds"
    return _signal("rsi_reversal", data, direction, strength, reason, rsi=rsi)


def macd_crossover(data: MarketData, fast: int = 12, slow: int = 26, signal_period: int = 9) -> dict:
    _check(data)
    if not 1 < fast < slow or signal_period < 2:
        raise ValueError("MACD periods must satisfy 1 < fast < slow and signal >= 2")
    fast_series, slow_series = _ema(data.closes, fast), _ema(data.closes, slow)
    if len(slow_series) < signal_period + 1:
        return _signal("macd_crossover", data, "neutral", 0, "Not enough candles for MACD signal line")
    offset = slow - fast
    line = [fast_series[i + offset] - value for i, value in enumerate(slow_series)]
    signal_line = _ema(line, signal_period)
    if len(signal_line) < 2:
        return _signal("macd_crossover", data, "neutral", 0, "Not enough candles for MACD crossover")
    histogram = [a - b for a, b in zip(line[-len(signal_line):], signal_line)]
    if histogram[-2] <= 0 < histogram[-1]:
        direction, reason = "long", "MACD crossed above its signal line"
    elif histogram[-2] >= 0 > histogram[-1]:
        direction, reason = "exit", "MACD crossed below its signal line"
    else:
        direction, reason = "neutral", "No new MACD crossover"
    strength = min(1, abs(histogram[-1]) / max(data.closes[-1], 1e-12) * 100)
    return _signal("macd_crossover", data, direction, strength, reason,
                   macd=line[-1], macd_signal=signal_line[-1])


def donchian_breakout(data: MarketData, period: int = 20) -> dict:
    _check(data, period + 1)
    if period < 2:
        raise ValueError("Donchian period must be >= 2")
    prior = data.closes[-period-1:-1]
    high = max(data.highs[-period-1:-1]) if data.highs else max(prior)
    low = min(data.lows[-period-1:-1]) if data.lows else min(prior)
    price = data.closes[-1]
    if price > high:
        direction, strength, reason = "long", min(1, (price - high) / high * 100), "Close broke above prior Donchian high"
    elif price < low:
        direction, strength, reason = "exit", min(1, (low - price) / low * 100), "Close broke below prior Donchian low"
    else:
        direction, strength, reason = "neutral", 0, "No Donchian breakout"
    return _signal("donchian_breakout", data, direction, strength, reason,
                   channel_high=high, channel_low=low)


def markov_regime(data: MarketData, model: CryptoRegimeHMM | None = None) -> dict:
    _check(data, 2)
    returns = [(b / a - 1) * 100 for a, b in zip(data.closes, data.closes[1:])]
    hmm = model or CryptoRegimeHMM()
    path = hmm.viterbi(returns)
    try:
        current_index = hmm.states.index(path[-1])
        probabilities = hmm.predict_next_state_prob(current_index)
    except (ValueError, IndexError, KeyError):
        return _signal("markov_regime", data, "neutral", 0, "Could not infer current Markov regime")
    up = probabilities.get("HU", 0.0)
    down = probabilities.get("HD", 0.0)
    margin = abs(up - down)
    if up > down:
        direction, reason = "long", "Markov transition model favors the high-up regime"
    elif down > up:
        direction, reason = "exit", "Markov transition model favors the high-down regime"
    else:
        direction, reason = "neutral", "Markov model has no directional transition edge"
    return _signal("markov_regime", data, direction, margin, reason,
                   current_regime=path[-1], next_state_probabilities=probabilities,
                   caveat="Uses fixed prototype transition and emission probabilities; not calibrated or validated")


def elliott_wave_heuristic(data: MarketData, min_wave_deviation: float = 0.005) -> dict:
    """Return a low-confidence pivot-pattern hint; Elliott counts are subjective."""
    _check(data, 7)
    if not 0 < min_wave_deviation < 1:
        raise ValueError("min_wave_deviation must be between 0 and 1")
    # Thresholded alternating pivots reduce noise, but do not establish wave counts.
    pivots: list[tuple[int, float]] = []
    direction = 0
    extreme_i = 0
    extreme = data.closes[0]
    for i, price in enumerate(data.closes[1:], 1):
        if direction == 0:
            if price >= extreme * (1 + min_wave_deviation):
                direction, extreme_i, extreme = 1, i, price
            elif price <= extreme * (1 - min_wave_deviation):
                direction, extreme_i, extreme = -1, i, price
        elif direction > 0:
            if price > extreme:
                extreme_i, extreme = i, price
            elif price <= extreme * (1 - min_wave_deviation):
                pivots.append((extreme_i, extreme))
                direction, extreme_i, extreme = -1, i, price
        else:
            if price < extreme:
                extreme_i, extreme = i, price
            elif price >= extreme * (1 + min_wave_deviation):
                pivots.append((extreme_i, extreme))
                direction, extreme_i, extreme = 1, i, price
    pivots.append((extreme_i, extreme))
    reason = "Insufficient alternating pivots for even a heuristic wave pattern"
    direction_out, strength = "neutral", 0.0
    if len(pivots) >= 5:
        vals = [p for _, p in pivots[-5:]]
        rising_structure = vals[0] < vals[2] < vals[4] and vals[1] < vals[3]
        falling_structure = vals[0] > vals[2] > vals[4] and vals[1] > vals[3]
        if rising_structure:
            direction_out, strength = "long", 0.2
            reason = "Pivots loosely resemble a rising five-swing impulse (highly subjective)"
        elif falling_structure:
            direction_out, strength = "exit", 0.2
            reason = "Pivots loosely resemble a falling five-swing impulse (highly subjective)"
        else:
            reason = "Recent pivots do not match the simple heuristic pattern"
    return _signal("elliott_wave_heuristic", data, direction_out, strength, reason,
                   heuristic=True, caveat="Elliott Wave labeling is subjective and ambiguous; this is an uncertain heuristic, not an objective wave count")


STRATEGIES: dict[str, Callable[[MarketData], dict]] = {
    "ema_12_26_crossover": ema_crossover,
    "bollinger_mean_reversion": bollinger_mean_reversion,
    "rsi_reversal": rsi_reversal,
    "macd_crossover": macd_crossover,
    "donchian_breakout": donchian_breakout,
    "markov_regime": markov_regime,
    "elliott_wave_heuristic": elliott_wave_heuristic,
}

STRATEGY_CATALOG = [
    {"name": name, "status": "implemented", "signal_only": True}
    for name in STRATEGIES
]


def generate_signal(strategy_name: str, symbol: str, closes: list[float], **kwargs: object) -> dict:
    """Common adapter signature; optional highs/lows support OHLC-aware strategies."""
    if strategy_name not in STRATEGIES:
        raise ValueError(f"Strategy is not implemented: {strategy_name}")
    allowed = {"highs", "lows"}
    market_args = {key: kwargs.pop(key) for key in tuple(kwargs) if key in allowed}
    data = MarketData(symbol=symbol, closes=closes, **market_args)
    strategy = STRATEGIES[strategy_name]
    if kwargs:
        # Tunables are deliberately restricted to the corresponding standalone functions.
        funcs = {"bollinger_mean_reversion": bollinger_mean_reversion,
                 "rsi_reversal": rsi_reversal, "macd_crossover": macd_crossover,
                 "donchian_breakout": donchian_breakout,
                 "elliott_wave_heuristic": elliott_wave_heuristic}
        if strategy_name not in funcs:
            raise TypeError(f"No parameters supported for {strategy_name}")
        return funcs[strategy_name](data, **kwargs)
    return strategy(data)


class IncrementalBacktestSignal:
    """Track the current long/flat decision without rebuilding candle prefixes."""

    _WINDOWS = {
        "bollinger_mean_reversion": 20,
        "rsi_reversal": 15,
        "donchian_breakout": 21,
    }

    def __init__(self, strategy_name: str, symbol: str):
        if strategy_name not in STRATEGIES:
            raise ValueError(f"Strategy is not implemented: {strategy_name}")
        self.strategy_name = strategy_name
        self.symbol = symbol
        self.count = 0
        self.closes = deque(maxlen=self._WINDOWS.get(strategy_name, 1))
        self.highs = deque(maxlen=self._WINDOWS.get(strategy_name, 1))
        self.lows = deque(maxlen=self._WINDOWS.get(strategy_name, 1))
        self.previous_close: float | None = None
        self.fast_seed: list[float] = []
        self.slow_seed: list[float] = []
        self.fast_ema: float | None = None
        self.slow_ema: float | None = None
        self.previous_ema_difference: float | None = None
        self.macd_seed: list[float] = []
        self.macd_signal: float | None = None
        self.previous_macd_histogram: float | None = None
        self.hmm = CryptoRegimeHMM() if strategy_name == "markov_regime" else None
        self.hmm_scores: list[float] | None = None
        self.wave_direction = 0
        self.wave_extreme_index = 0
        self.wave_extreme: float | None = None
        self.wave_pivots = deque(maxlen=5)

    def update(self, close: float, high: float, low: float) -> bool:
        self.count += 1
        self.closes.append(close)
        self.highs.append(high)
        self.lows.append(low)

        if not self.symbol:
            return False
        if self.strategy_name in {"ema_12_26_crossover", "macd_crossover"}:
            return self._update_ema(close)
        if self.strategy_name == "markov_regime":
            return self._update_markov(close)
        if self.strategy_name == "elliott_wave_heuristic":
            return self._update_elliott(close)
        try:
            return generate_signal(self.strategy_name, self.symbol, list(self.closes),
                                   highs=list(self.highs), lows=list(self.lows))["direction"] == "long"
        except (ValueError, TypeError):
            return False

    def _update_ema(self, close: float) -> bool:
        if self.fast_ema is None:
            self.fast_seed.append(close)
            if len(self.fast_seed) == 12:
                self.fast_ema = sum(self.fast_seed) / 12
        else:
            self.fast_ema = (2 / 13) * close + (1 - 2 / 13) * self.fast_ema

        if self.slow_ema is None:
            self.slow_seed.append(close)
            if len(self.slow_seed) == 26:
                self.slow_ema = sum(self.slow_seed) / 26
        else:
            self.slow_ema = (2 / 27) * close + (1 - 2 / 27) * self.slow_ema

        if self.fast_ema is None or self.slow_ema is None:
            return False
        difference = self.fast_ema - self.slow_ema
        is_long = (self.previous_ema_difference is not None
                   and self.previous_ema_difference <= 0 < difference)
        self.previous_ema_difference = difference

        if self.strategy_name == "ema_12_26_crossover":
            return is_long

        if self.macd_signal is None:
            self.macd_seed.append(difference)
            if len(self.macd_seed) < 9:
                return False
            self.macd_signal = sum(self.macd_seed) / 9
        else:
            self.macd_signal = (2 / 10) * difference + (1 - 2 / 10) * self.macd_signal
        histogram = difference - self.macd_signal
        is_long = (self.previous_macd_histogram is not None
                   and self.previous_macd_histogram <= 0 < histogram)
        self.previous_macd_histogram = histogram
        return is_long

    def _update_markov(self, close: float) -> bool:
        if self.previous_close is None:
            self.previous_close = close
            return False
        change = (close / self.previous_close - 1) * 100
        self.previous_close = close
        model = self.hmm
        symbol = classify_observation(change)
        if self.hmm_scores is None:
            self.hmm_scores = [
                model._log(model.initial[i])
                + model._log(model.emissions[state].get(symbol, 0.0))
                for i, state in enumerate(model.states)
            ]
        else:
            scores = self.hmm_scores
            next_scores = []
            for destination, state in enumerate(model.states):
                candidates = [
                    scores[source] + model._log(model.trans[source][destination])
                    for source in range(len(model.states))
                ]
                best_source = max(range(len(candidates)), key=candidates.__getitem__)
                next_scores.append(
                    candidates[best_source] + model._log(model.emissions[state].get(symbol, 0.0))
                )
            self.hmm_scores = next_scores

        current = model.states[max(range(len(self.hmm_scores)), key=self.hmm_scores.__getitem__)]
        probabilities = model.predict_next_state_prob(model.states.index(current))
        return probabilities.get("HU", 0.0) > probabilities.get("HD", 0.0)

    def _update_elliott(self, close: float) -> bool:
        if self.wave_extreme is None:
            self.wave_extreme = close
            return False

        deviation = 0.005
        if self.wave_direction == 0:
            if close >= self.wave_extreme * (1 + deviation):
                self.wave_direction, self.wave_extreme_index, self.wave_extreme = 1, self.count - 1, close
            elif close <= self.wave_extreme * (1 - deviation):
                self.wave_direction, self.wave_extreme_index, self.wave_extreme = -1, self.count - 1, close
        elif self.wave_direction > 0:
            if close > self.wave_extreme:
                self.wave_extreme_index, self.wave_extreme = self.count - 1, close
            elif close <= self.wave_extreme * (1 - deviation):
                self.wave_pivots.append(self.wave_extreme)
                self.wave_direction, self.wave_extreme_index, self.wave_extreme = -1, self.count - 1, close
        else:
            if close < self.wave_extreme:
                self.wave_extreme_index, self.wave_extreme = self.count - 1, close
            elif close >= self.wave_extreme * (1 + deviation):
                self.wave_pivots.append(self.wave_extreme)
                self.wave_direction, self.wave_extreme_index, self.wave_extreme = 1, self.count - 1, close

        if self.count < 7:
            return False
        values = [*self.wave_pivots, self.wave_extreme]
        if len(values) < 5:
            return False
        vals = values[-5:]
        return vals[0] < vals[2] < vals[4] and vals[1] < vals[3]
