"""Deterministic, long-only candle backtests with next-bar execution."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from math import isfinite
from typing import Callable, Sequence

from .collectors.collector_base import Candle

Signal = Callable[[Sequence[Candle]], bool]


@dataclass(frozen=True)
class BacktestTrade:
    side: str
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    quantity: float
    fees: float
    pnl: float
    return_pct: float


@dataclass(frozen=True)
class BacktestResult:
    initial_cash: float
    final_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    win_rate_pct: float
    trade_count: int
    fees_paid: float
    candles: int
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[tuple[int, float], ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["trades"] = [asdict(t) for t in self.trades]
        data["equity_curve"] = [list(point) for point in self.equity_curve]
        return data


class BacktestEngine:
    """Run a bool signal against ordered candles; buys/sells fill next bar open.

    A signal is evaluated after each candle closes and sees only candles through
    that close. It means "target long" and is executed at the following candle's
    open. Any remaining position is liquidated at the final close. Fees are
    charged on both entry and exit; fractional quantities are allowed.
    """

    def __init__(self, initial_cash: float = 10_000.0, fee_rate: float = 0.001):
        if not isfinite(initial_cash) or initial_cash <= 0:
            raise ValueError("initial_cash must be finite and positive")
        if not isfinite(fee_rate) or fee_rate < 0 or fee_rate >= 1:
            raise ValueError("fee_rate must be finite and in [0, 1)")
        self.initial_cash = float(initial_cash)
        self.fee_rate = float(fee_rate)

    def run(self, candles: Sequence[Candle], signal_fn: Signal, symbol: str = "") -> BacktestResult:
        del symbol  # reserved for strategy APIs; each run represents one market.
        bars = list(candles)
        if not bars:
            raise ValueError("At least one candle is required")
        self._validate_candles(bars)
        cash = self.initial_cash
        qty = 0.0
        entry_price = entry_ts = entry_fee = 0.0
        trades: list[BacktestTrade] = []
        fees_paid = 0.0
        curve: list[tuple[int, float]] = []

        # At bar i, execute the decision made from bar i-1's completed prefix.
        # No strategy has access to the current bar before its open fill.
        for i, bar in enumerate(bars):
            if i:
                target_long = bool(signal_fn(tuple(bars[:i])))
                if target_long and qty == 0:
                    entry_price = bar.open
                    qty = cash / (entry_price * (1 + self.fee_rate))
                    notional = qty * entry_price
                    entry_fee = notional * self.fee_rate
                    cash -= notional + entry_fee
                    fees_paid += entry_fee
                    entry_ts = bar.ts_open
                elif not target_long and qty > 0:
                    exit_price = bar.open
                    notional = qty * exit_price
                    exit_fee = notional * self.fee_rate
                    cash += notional - exit_fee
                    fees_paid += exit_fee
                    pnl = cash - self.initial_cash if not trades else (notional - exit_fee) - (qty * entry_price + entry_fee)
                    # The expression above is per trade; cash also includes prior completed trades.
                    if trades:
                        prior_pnl = sum(t.pnl for t in trades)
                        pnl = cash - self.initial_cash - prior_pnl
                    denominator = qty * entry_price + entry_fee
                    trades.append(BacktestTrade("long", int(entry_ts), bar.ts_open,
                        float(entry_price), float(exit_price), float(qty),
                        float(entry_fee + exit_fee), float(pnl),
                        float(pnl / denominator * 100 if denominator else 0.0)))
                    qty = 0.0
                    entry_price = entry_fee = 0.0
            equity = cash + qty * bar.close
            curve.append((bar.ts_close, equity))

        if qty > 0:
            last = bars[-1]
            notional = qty * last.close
            exit_fee = notional * self.fee_rate
            cash += notional - exit_fee
            fees_paid += exit_fee
            prior_pnl = sum(t.pnl for t in trades)
            pnl = cash - self.initial_cash - prior_pnl
            denominator = qty * entry_price + entry_fee
            trades.append(BacktestTrade("long", int(entry_ts), last.ts_close,
                float(entry_price), float(last.close), float(qty),
                float(entry_fee + exit_fee), float(pnl),
                float(pnl / denominator * 100 if denominator else 0.0)))
            curve[-1] = (last.ts_close, cash)

        final = cash
        peak = self.initial_cash
        max_dd = 0.0
        for _, equity in curve:
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)
        winners = sum(t.pnl > 0 for t in trades)
        return BacktestResult(self.initial_cash, final,
            (final / self.initial_cash - 1) * 100, max_dd * 100,
            winners / len(trades) * 100 if trades else 0.0, len(trades), fees_paid,
            len(bars), tuple(trades), tuple(curve))

    @staticmethod
    def _validate_candles(candles: Sequence[Candle]) -> None:
        prior = -1
        for c in candles:
            values = (c.open, c.high, c.low, c.close, c.volume)
            if not all(isfinite(float(v)) for v in values) or min(c.open, c.high, c.low, c.close) <= 0 or c.volume < 0:
                raise ValueError("Candle prices must be positive finite numbers and volume nonnegative")
            if c.low > min(c.open, c.close) or c.high < max(c.open, c.close):
                raise ValueError("Candle high/low do not contain open and close")
            if c.ts_open <= prior or c.ts_close <= c.ts_open:
                raise ValueError("Candles must be strictly time ordered with positive duration")
            prior = c.ts_open


def ema_crossover_signal(fast_period: int = 12, slow_period: int = 26) -> Signal:
    """Return a causal close-based EMA crossover signal factory."""
    if fast_period < 1 or slow_period < 2 or fast_period >= slow_period:
        raise ValueError("Require 1 <= fast_period < slow_period")

    def signal(history: Sequence[Candle]) -> bool:
        if len(history) < slow_period:
            return False
        closes = [c.close for c in history]
        fast = _ema(closes[-max(slow_period * 3, fast_period):], fast_period)
        slow = _ema(closes[-slow_period * 3:], slow_period)
        return fast > slow
    return signal


def markov_momentum_signal(lookback: int = 100, threshold: float = 0.5) -> Signal:
    """Estimate next up probability from observed up/down state transitions.

    This is a small, deterministic first-order Markov baseline, not a promise
    of profitability. It only uses returns in the supplied historical prefix.
    """
    if lookback < 3 or not 0 <= threshold <= 1:
        raise ValueError("lookback must be >= 3 and threshold must be in [0, 1]")

    def signal(history: Sequence[Candle]) -> bool:
        changes = [history[i].close >= history[i - 1].close for i in range(1, len(history))]
        changes = changes[-lookback:]
        if len(changes) < 2:
            return False
        current = changes[-1]
        following = [changes[i + 1] for i in range(len(changes) - 1) if changes[i] == current]
        probability_up = sum(following) / len(following) if following else sum(changes) / len(changes)
        return probability_up > threshold
    return signal


def _ema(values: Sequence[float], period: int) -> float:
    alpha = 2 / (period + 1)
    value = float(values[0])
    for price in values[1:]:
        value = alpha * float(price) + (1 - alpha) * value
    return value
