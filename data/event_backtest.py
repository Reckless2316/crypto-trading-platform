"""Event-driven bracket backtests measured in R (multiples of the initial risk).

`data.backtesting.BacktestEngine` answers "what if I held long while a signal was on". This
module answers the question most discretionary-style strategies ask instead: "I enter here
with this stop and this target, so what happened?" It handles longs and shorts, stop-first
fills when a bar spans both levels, a no-progress time stop, a hard maximum hold, slippage
inside fill prices, maker/taker fees, and perpetual funding.

Bars are any object with parallel `ts`, `open`, `high`, `low`, `close` sequences. `ts` is bar
open time in ms.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, Sequence


class BarSeries(Protocol):
    ts: Sequence[int]
    open: Sequence[float]
    high: Sequence[float]
    low: Sequence[float]
    close: Sequence[float]


@dataclass(frozen=True)
class CostModel:
    taker_fee: float = 0.0005
    maker_fee: float = 0.0002
    slippage: float = 0.0002      # per side, applied to market fills (entry, stop, time exits)

    def round_trip(self) -> float:
        """Worst-case round trip: taker both sides plus slippage both sides."""
        return 2 * self.taker_fee + 2 * self.slippage


@dataclass(frozen=True)
class Bracket:
    side: str                       # "long" or "short"
    entry_index: int                # bar whose open is the market entry
    stop: float
    target: float                   # resting limit; fills only on a trade-through
    time_stop_bars: int | None = None   # exit at close if +1R not touched by then
    max_hold_bars: int = 96


@dataclass(frozen=True)
class Fill:
    side: str
    entry_ts: int
    exit_ts: int
    entry: float
    stop: float
    target: float
    exit_price: float
    exit_reason: str                # stop, target, time_stop, max_hold, data_gap
    risk: float                     # entry-to-stop distance, price units
    gross_r: float                  # price move including slippage
    fee_r: float                    # negative
    funding_r: float                # negative when the position paid funding
    net_r: float


def entry_price(bars: BarSeries, bracket: Bracket, costs: CostModel) -> float:
    d = 1 if bracket.side == "long" else -1
    return bars.open[bracket.entry_index] * (1 + d * costs.slippage)


def simulate_bracket(bars: BarSeries, bracket: Bracket, costs: CostModel, bar_ms: int,
                     funding_paid: Callable[[int, int], float] | None = None) -> Fill:
    """Walk forward from the entry bar until an exit. `funding_paid(start_ms, end_ms)` returns the
    sum of funding rates settled in (start, end]; longs pay positive rates and shorts receive them."""
    if bracket.side not in ("long", "short"):
        raise ValueError("side must be long or short")
    d = 1 if bracket.side == "long" else -1
    k0 = bracket.entry_index
    entry = entry_price(bars, bracket, costs)
    stop, target = bracket.stop, bracket.target
    risk = (entry - stop) * d
    if risk <= 0:
        raise ValueError("entry is at or beyond the stop")
    if (target - entry) * d <= 0:
        raise ValueError("target is not beyond entry")
    one_r = entry + d * risk
    touched = False
    exit_fee = costs.taker_fee
    slip = costs.slippage
    last = min(len(bars.ts) - 1, k0 + bracket.max_hold_bars - 1)
    k = k0
    while True:
        o, h, l, c = bars.open[k], bars.high[k], bars.low[k], bars.close[k]
        adverse, favorable = (l, h) if d == 1 else (h, l)
        if (adverse - stop) * d <= 0:          # stop first when both are inside one bar
            fill = stop if (o - stop) * d > 0 else o
            exit_px, reason = fill * (1 - d * slip), "stop"
            break
        if (favorable - target) * d > 0:
            exit_px, reason, exit_fee = target, "target", costs.maker_fee
            break
        touched = touched or (favorable - one_r) * d >= 0
        held = k - k0 + 1
        if held >= bracket.max_hold_bars:
            exit_px, reason = c * (1 - d * slip), "max_hold"
            break
        if bracket.time_stop_bars is not None and held >= bracket.time_stop_bars and not touched:
            exit_px, reason = c * (1 - d * slip), "time_stop"
            break
        if k + 1 > last or bars.ts[k + 1] != bars.ts[k] + bar_ms:
            exit_px, reason = c * (1 - d * slip), "data_gap"
            break
        k += 1
    entry_ts, exit_ts = bars.ts[k0], bars.ts[k] + bar_ms
    gross = (exit_px - entry) * d
    fees = costs.taker_fee * entry + exit_fee * exit_px
    fund = d * funding_paid(entry_ts, exit_ts) * entry if funding_paid else 0.0
    return Fill(bracket.side, entry_ts, exit_ts, entry, stop, target, exit_px, reason, risk,
                gross / risk, -fees / risk, -fund / risk, (gross - fees - fund) / risk)
