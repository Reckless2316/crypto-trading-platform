"""Fade the forced flush: sweep detection, flush/crowding flags, and per-signal trade simulation.

Rules are pinned in docs/research/2026-10-25-fade-forced-flush/PREREG.md. Signal detection walks
15m bars in order and reads only what was known at each bar's close. Trade simulation, which
necessarily looks forward, is a separate step. Every signal carries both filter flags, so the
four ablation variants are just subsets of one trade list.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from data.binance_archive import FIVE_MIN_MS, Series
from data.event_backtest import Bracket, CostModel, simulate_bracket

M15 = 900_000
H1 = 3_600_000
H4 = 4 * H1
DAY = 86_400_000

# FOMC statement days (UTC dates).
FOMC_DAYS = frozenset("""
2021-01-27 2021-03-17 2021-04-28 2021-06-16 2021-07-28 2021-09-22 2021-11-03 2021-12-15
2022-01-26 2022-03-16 2022-05-04 2022-06-15 2022-07-27 2022-09-21 2022-11-02 2022-12-14
2023-02-01 2023-03-22 2023-05-03 2023-06-14 2023-07-26 2023-09-20 2023-11-01 2023-12-13
2024-01-31 2024-03-20 2024-05-01 2024-06-12 2024-07-31 2024-09-18 2024-11-07 2024-12-18
2025-01-29 2025-03-19 2025-05-07 2025-06-18 2025-07-30 2025-09-17 2025-10-29 2025-12-10
2026-01-28 2026-03-18 2026-04-29 2026-06-17 2026-07-29 2026-09-16 2026-10-28 2026-12-09
""".split())

MAJORS = frozenset({"BTCUSDT", "ETHUSDT"})


@dataclass(frozen=True)
class Params:
    atr_period: int = 14
    min_level_age_ms: int = 12 * H1
    fractal_width: int = 2
    sweep_level_life_ms: int = 7 * DAY      # 4h swing levels
    target_1h_life_ms: int = 3 * DAY        # 1h swing levels (targets only)
    max_sweep_bars: int = 3
    min_pen_atr: float = 0.25
    max_pen_atr: float = 1.5
    oi_sigma_window_ms: int = 30 * DAY
    oi_sigma_min_obs: int = 1000
    flush_z: float = -2.0
    funding_window_ms: int = 24 * H1
    funding_rank_window_ms: int = 30 * DAY
    funding_rank_min_obs: int = 45
    crowd_rank: float = 0.70
    stop_buffer_atr: float = 0.15
    min_target_r: float = 2.0
    cost_gate_mult: float = 5.0
    time_stop_bars: int = 12
    max_hold_bars: int = 96
    funding_skip_ms: int = 15 * 60_000
    taker_fee: float = 0.0005
    maker_fee: float = 0.0002
    slip_major: float = 0.0002
    slip_alt: float = 0.0005
    slip_mult: float = 1.0

    def slippage(self, symbol: str) -> float:
        return (self.slip_major if symbol in MAJORS else self.slip_alt) * self.slip_mult

    def round_trip_cost(self, symbol: str) -> float:
        return 2 * self.taker_fee + 2 * self.slippage(symbol)


@dataclass
class Bars:
    ts: list[int]
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]
    low_ts: list[int]    # open time of the sub-bar that printed the low
    high_ts: list[int]


def resample(ts, o, h, l, c, step: int, sub: int, low_ts=None, high_ts=None) -> Bars:
    """Aggregate to UTC-aligned buckets of `step` ms. Incomplete buckets are dropped."""
    need = step // sub
    out = Bars([], [], [], [], [], [], [])
    low_ts = low_ts or ts
    high_ts = high_ts or ts
    i, n = 0, len(ts)
    while i < n:
        bucket = ts[i] - ts[i] % step
        j = i
        while j < n and ts[j] - ts[j] % step == bucket:
            j += 1
        if j - i == need:
            lo = min(range(i, j), key=l.__getitem__)
            hi = max(range(i, j), key=h.__getitem__)
            out.ts.append(bucket)
            out.open.append(o[i])
            out.high.append(h[hi])
            out.low.append(l[lo])
            out.close.append(c[j - 1])
            out.low_ts.append(low_ts[lo])
            out.high_ts.append(high_ts[hi])
        i = j
    return out


def wilder_atr(bars: Bars, period: int) -> list[float]:
    out = [math.nan] * len(bars.ts)
    trs: list[float] = []
    prev_close = None
    atr = math.nan
    for i in range(len(bars.ts)):
        h, l = bars.high[i], bars.low[i]
        tr = h - l if prev_close is None else max(h - l, abs(h - prev_close), abs(l - prev_close))
        prev_close = bars.close[i]
        if len(trs) < period:
            trs.append(tr)
            if len(trs) == period:
                atr = sum(trs) / period
        else:
            atr = (atr * (period - 1) + tr) / period
        out[i] = atr
    return out


@dataclass
class Level:
    price: float
    side: str            # "low" or "high"
    print_ts: int
    active_from: int
    expires: int
    sweepable: bool
    source: str


def fractal_levels(bars: Bars, step: int, width: int, life_ms: int, sweepable: bool, source: str) -> list[Level]:
    """Swing points strictly beyond `width` bars on each side, usable once bar k+width has closed."""
    levels = []
    for k in range(width, len(bars.ts) - width):
        if bars.ts[k + width] - bars.ts[k - width] != 2 * width * step:
            continue  # gap in the data; the fractal is not well defined
        around = [*range(k - width, k), *range(k + 1, k + width + 1)]
        confirm = bars.ts[k + width] + step
        if all(bars.low[k] < bars.low[j] for j in around):
            levels.append(Level(bars.low[k], "low", bars.low_ts[k], confirm, bars.ts[k] + life_ms, sweepable, source))
        if all(bars.high[k] > bars.high[j] for j in around):
            levels.append(Level(bars.high[k], "high", bars.high_ts[k], confirm, bars.ts[k] + life_ms, sweepable, source))
    levels.sort(key=lambda lv: lv.active_from)
    return levels


@dataclass
class Signal:
    symbol: str
    side: str                 # "long" (lows swept) or "short" (highs swept)
    bar_index: int            # reclaim bar
    signal_ts: int            # reclaim bar close
    level: float
    level_source: str
    level_age_h: float
    sweep_extreme: float
    sweep_bars: int
    atr: float
    pen_atr: float
    oi_z: float | None
    f24: float | None
    f_rank: float | None
    flush: bool | None = None
    crowd: bool | None = None
    targets: list[float] = field(default_factory=list)


class OiIndex:
    """OI lookups at 15m boundaries and a causal rolling std of 15m log changes."""

    def __init__(self, series: Series, bar_ts: list[int], params: Params):
        self.at: dict[int, float] = {}
        snaps = dict(zip(series.oi_ts, series.oi))
        for t in bar_ts:
            for edge in (t, t + M15):
                if edge not in self.at:
                    value = snaps.get(edge) or snaps.get(edge - FIVE_MIN_MS)
                    if value:
                        self.at[edge] = value
        self.ts = bar_ts
        cnt, s1, s2 = [0], [0.0], [0.0]
        for t in bar_ts:
            a, b = self.at.get(t), self.at.get(t + M15)
            r = math.log(b / a) if a and b else None
            cnt.append(cnt[-1] + (r is not None))
            s1.append(s1[-1] + (r or 0.0))
            s2.append(s2[-1] + (r * r if r is not None else 0.0))
        self.cnt, self.s1, self.s2 = cnt, s1, s2
        self.params = params

    def z(self, start: int, end: int) -> float | None:
        """z of the OI log change from bar `start` open to bar `end` close, against the prior 30 days."""
        a, b = self.at.get(self.ts[start]), self.at.get(self.ts[end] + M15)
        if not a or not b:
            return None
        lo = bisect_left(self.ts, self.ts[start] - self.params.oi_sigma_window_ms)
        n = self.cnt[start] - self.cnt[lo]
        if n < self.params.oi_sigma_min_obs:
            return None
        mean = (self.s1[start] - self.s1[lo]) / n
        var = (self.s2[start] - self.s2[lo]) / n - mean * mean
        if var <= 0:
            return None
        return math.log(b / a) / (math.sqrt(var) * math.sqrt(end - start + 1))


class FundingIndex:
    def __init__(self, series: Series, params: Params):
        self.ts = series.funding_ts
        self.rate = series.funding
        self.params = params
        # Summed exactly and rounded: funding often sits at the 0.01% default for weeks, and the
        # resulting ties must stay ties rather than be broken by float noise.
        f24 = []
        lo = 0
        for k, t in enumerate(self.ts):
            while self.ts[lo] <= t - params.funding_window_ms:
                lo += 1
            f24.append(round(math.fsum(self.rate[lo:k + 1]), 12))
        self.f24 = f24

    def crowding(self, t: int) -> tuple[float, float] | None:
        """(F24, rank of F24 among the prior 30 days of settlements) as of time t."""
        last = bisect_right(self.ts, t) - 1
        if last < 0 or self.ts[last] < t - 9 * H1:
            return None
        lo = bisect_right(self.ts, t - self.params.funding_rank_window_ms)
        window = self.f24[lo:last + 1]
        if len(window) < self.params.funding_rank_min_obs:
            return None
        cur = self.f24[last]
        below = sum(v < cur for v in window)
        ties = sum(v == cur for v in window)
        return cur, (below + 0.5 * ties) / len(window)   # mid-rank, so a flat history ranks 0.5

    def near_settlement(self, t: int) -> bool:
        k = bisect_left(self.ts, t)
        return any(0 <= j < len(self.ts) and abs(self.ts[j] - t) <= self.params.funding_skip_ms for j in (k - 1, k))

    def paid(self, start: int, end: int) -> float:
        """Sum of rates settled in (start, end]."""
        return sum(self.rate[bisect_right(self.ts, start):bisect_right(self.ts, end)])


def build_bars(series: Series) -> tuple[Bars, Bars, Bars]:
    b15 = resample(series.ts, series.open, series.high, series.low, series.close, M15, FIVE_MIN_MS)
    b1h = resample(b15.ts, b15.open, b15.high, b15.low, b15.close, H1, M15, b15.low_ts, b15.high_ts)
    b4h = resample(b15.ts, b15.open, b15.high, b15.low, b15.close, H4, M15, b15.low_ts, b15.high_ts)
    return b15, b1h, b4h


def detect(series: Series, params: Params = Params()) -> tuple[list[Signal], Bars]:
    b15, b1h, b4h = build_bars(series)
    atr = wilder_atr(b15, params.atr_period)
    oi = OiIndex(series, b15.ts, params)
    funding = FundingIndex(series, params)
    pending = fractal_levels(b4h, H4, params.fractal_width, params.sweep_level_life_ms, True, "4h") + \
        fractal_levels(b1h, H1, params.fractal_width, params.target_1h_life_ms, False, "1h")
    pending.sort(key=lambda lv: lv.active_from)
    p = 0
    active: list[Level] = []
    sweeps: list[dict] = []
    signals: list[Signal] = []
    day = None
    day_lo = day_hi = None   # (price, ts) running extremes of the current UTC day

    for i, t in enumerate(b15.ts):
        today = t - t % DAY
        if today != day:
            active = [lv for lv in active if lv.source != "pd"]
            if day is not None and today - day == DAY and day_lo and day_hi:
                active.append(Level(day_lo[0], "low", day_lo[1], today, today + DAY, True, "pd"))
                active.append(Level(day_hi[0], "high", day_hi[1], today, today + DAY, True, "pd"))
            day, day_lo, day_hi = today, None, None
        while p < len(pending) and pending[p].active_from <= t:
            active.append(pending[p])
            p += 1
        active = [lv for lv in active if lv.expires > t]

        hi, lo, close = b15.high[i], b15.low[i], b15.close[i]
        survivors = []
        for lv in active:
            pierced = lo < lv.price if lv.side == "low" else hi > lv.price
            if not pierced:
                survivors.append(lv)
            elif lv.sweepable and t - lv.print_ts >= params.min_level_age_ms and i > 0 \
                    and b15.ts[i - 1] == t - M15 and not math.isnan(atr[i - 1]):
                sweeps.append({"level": lv, "start": i, "atr": atr[i - 1], "ext": lv.price})
        active = survivors

        fired: dict[str, list[dict]] = {"long": [], "short": []}
        still = []
        for sw in sweeps:
            lv = sw["level"]
            if t - b15.ts[sw["start"]] != (i - sw["start"]) * M15:
                continue  # data gap inside the sweep window
            if lv.side == "low":
                sw["ext"] = min(sw["ext"], lo)
                pen = (lv.price - sw["ext"]) / sw["atr"]
                reclaimed = close > lv.price
            else:
                sw["ext"] = max(sw["ext"], hi)
                pen = (sw["ext"] - lv.price) / sw["atr"]
                reclaimed = close < lv.price
            if pen > params.max_pen_atr:
                continue
            if reclaimed:
                if pen >= params.min_pen_atr:
                    sw["pen"] = pen
                    fired["long" if lv.side == "low" else "short"].append(sw)
                continue
            if i - sw["start"] + 1 < params.max_sweep_bars:
                still.append(sw)
        sweeps = still

        for side, group in fired.items():
            if not group:
                continue
            sw = min(group, key=lambda s: s["level"].price) if side == "long" else \
                max(group, key=lambda s: s["level"].price)
            lv = sw["level"]
            sig_ts = t + M15
            z = oi.z(sw["start"], i)
            crowd = funding.crowding(sig_ts)
            opposite = "high" if side == "long" else "low"
            sig = Signal(series.symbol, side, i, sig_ts, lv.price, lv.source, (t - lv.print_ts) / H1,
                         sw["ext"], i - sw["start"] + 1, sw["atr"], sw["pen"], z,
                         crowd[0] if crowd else None, crowd[1] if crowd else None,
                         targets=sorted(x.price for x in active if x.side == opposite))
            if z is not None:
                sig.flush = z <= params.flush_z
            if crowd is not None:
                f24, rank = crowd
                sig.crowd = (f24 > 0 and rank >= params.crowd_rank) if side == "long" else \
                    (f24 < 0 and rank <= 1 - params.crowd_rank)
            signals.append(sig)

        if day_lo is None or lo < day_lo[0]:
            day_lo = (lo, b15.low_ts[i])
        if day_hi is None or hi > day_hi[0]:
            day_hi = (hi, b15.high_ts[i])
    return signals, b15


@dataclass
class Trade:
    symbol: str
    side: str
    signal_ts: int
    entry_ts: int
    exit_ts: int
    entry: float
    stop: float
    target: float
    target_r: float
    risk_pct: float
    exit_price: float
    exit_reason: str
    gross_r: float
    fee_r: float
    funding_r: float
    net_r: float
    flush: bool | None
    crowd: bool | None
    oi_z: float | None
    f_rank: float | None
    pen_atr: float
    level_source: str

    def to_dict(self) -> dict:
        return asdict(self)


def _utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts / 1000, timezone.utc)


def simulate(sig: Signal, b15: Bars, funding: FundingIndex, params: Params) -> Trade | str:
    """Return a Trade, or the name of the gate that rejected the signal."""
    k0 = sig.bar_index + 1
    if k0 >= len(b15.ts) or b15.ts[k0] != sig.signal_ts:
        return "no_next_bar"
    entry_ts = b15.ts[k0]
    when = _utc(entry_ts)
    if funding.near_settlement(entry_ts):
        return "skip_funding_settlement"
    minute = when.hour * 60 + when.minute
    if when.date().isoformat() in FOMC_DAYS and 17 * 60 + 30 <= minute <= 21 * 60:
        return "skip_fomc"
    slip = params.slippage(sig.symbol)
    d = 1 if sig.side == "long" else -1
    entry = b15.open[k0] * (1 + d * slip)
    stop = sig.sweep_extreme - d * params.stop_buffer_atr * sig.atr
    risk = (entry - stop) * d
    if risk <= 0:
        return "entry_beyond_stop"
    beyond = [x for x in sig.targets if (x - entry) * d > 0]
    if not beyond:
        return "no_target"
    target = min(beyond) if d == 1 else max(beyond)
    target_r = (target - entry) * d / risk
    if target_r < params.min_target_r:
        return "target_below_2r"
    if risk / entry < params.cost_gate_mult * params.round_trip_cost(sig.symbol):
        return "cost_gate"

    fill = simulate_bracket(b15, Bracket(sig.side, k0, stop, target, params.time_stop_bars, params.max_hold_bars),
                            CostModel(params.taker_fee, params.maker_fee, slip), M15, funding.paid)
    return Trade(sig.symbol, sig.side, sig.signal_ts, fill.entry_ts, fill.exit_ts, fill.entry, stop, target, target_r,
                 risk / entry, fill.exit_price, fill.exit_reason, fill.gross_r, fill.fee_r, fill.funding_r,
                 fill.net_r, sig.flush, sig.crowd, sig.oi_z, sig.f_rank, sig.pen_atr, sig.level_source)


def run_symbol(series: Series, params: Params = Params()) -> tuple[list[Trade], dict[str, int], int]:
    signals, b15 = detect(series, params)
    funding = FundingIndex(series, params)
    trades: list[Trade] = []
    rejected: dict[str, int] = {}
    for sig in signals:
        result = simulate(sig, b15, funding, params)
        if isinstance(result, str):
            rejected[result] = rejected.get(result, 0) + 1
        else:
            trades.append(result)
    return trades, rejected, len(signals)
