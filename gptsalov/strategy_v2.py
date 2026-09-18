"""Research-only signal candidates for historical replay. Never places orders.

All functions take float arrays of CLOSED bars and, for index i, read only
data at indexes <= i. Parameters are predeclared hypotheses, not fitted optima.
The v1 baseline itself is NOT reimplemented here: the backtest calls the exact
production `core.strategy` on the same 199-bar window the live scanner sees.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Series:
    """Column-oriented OHLCV bars of one timeframe. t = open time in ms."""
    t: list
    o: list
    h: list
    l: list
    c: list
    v: list
    qv: list

    def __len__(self):
        return len(self.t)


@dataclass(frozen=True)
class Sig:
    side: int            # 1 long, -1 short
    reference: float     # close of the signal bar
    stop_dist: float     # price distance, re-anchored at the actual entry in v2 variants
    target_r: float | None  # target as multiple of stop_dist; None = trailing exit only
    atr: float
    score: float         # ranking heuristic, NOT a probability
    stop: float | None = None    # absolute stop/target (baseline keeps signal prices)
    target: float | None = None


def resample(s: Series, factor: int, bar_ms: int) -> Series:
    """Aggregate complete, contiguous groups (e.g. 4 x 15m -> 1h). Incomplete groups dropped."""
    span = bar_ms*factor
    out = {k: [] for k in ('t', 'o', 'h', 'l', 'c', 'v', 'qv')}
    i, n = 0, len(s)
    while i < n:
        start = s.t[i]//span*span
        j = i
        while j < n and s.t[j] < start+span:
            j += 1
        if j-i == factor and s.t[i] == start and s.t[j-1] == start+(factor-1)*bar_ms:
            out['t'].append(start)
            out['o'].append(s.o[i])
            out['h'].append(max(s.h[i:j]))
            out['l'].append(min(s.l[i:j]))
            out['c'].append(s.c[j-1])
            out['v'].append(sum(s.v[i:j]))
            out['qv'].append(sum(s.qv[i:j]))
        i = j
    return Series(**out)


def ema(values, n):
    out = [None]*len(values)
    if len(values) < n:
        return out
    e = sum(values[:n])/n
    out[n-1] = e
    k = 2/(n+1)
    for i in range(n, len(values)):
        e += k*(values[i]-e)
        out[i] = e
    return out


def atr(s: Series, n):
    """Simple mean of the last n true ranges (same definition as core.strategy)."""
    tr = [s.h[0]-s.l[0]] + [max(s.h[i]-s.l[i], abs(s.h[i]-s.c[i-1]), abs(s.l[i]-s.c[i-1]))
                            for i in range(1, len(s))]
    out = [None]*len(s)
    run = 0.0
    for i, x in enumerate(tr):
        run += x
        if i >= n:
            run -= tr[i-n]
        if i >= n:  # first TR has no previous close; start after it drops out
            out[i] = run/n
    return out


def prior_extremes(s: Series, n):
    """Highest high / lowest low / mean volume of the n bars BEFORE i."""
    hi, lo, vol = [None]*len(s), [None]*len(s), [None]*len(s)
    for i in range(n, len(s)):
        hi[i] = max(s.h[i-n:i])
        lo[i] = min(s.l[i-n:i])
        vol[i] = sum(s.v[i-n:i])/n
    return hi, lo, vol


def efficiency(closes, n):
    """Kaufman efficiency ratio: |net move| / path length over n bars (0..1)."""
    out = [None]*len(closes)
    for i in range(n, len(closes)):
        path = sum(abs(closes[k]-closes[k-1]) for k in range(i-n+1, i+1))
        out[i] = abs(closes[i]-closes[i-n])/path if path > 0 else 0.0
    return out


@dataclass(frozen=True)
class V2Params:
    donchian: int = 20
    volume_mult: float = 1.2
    trend_ema: int = 50
    slope_bars: int = 5
    er_bars: int = 20
    er_min: float = 0.30          # trend quality: avoid choppy breakouts
    atr_bars: int = 14
    atr_slow: int = 50
    max_vol_expansion: float = 1.3  # ATR14/ATR50 before breakout; skip already-exploded moves
    max_chase_atr: float = 0.75   # close beyond breakout level, in ATR
    min_close_location: float = 0.6  # close near the bar extreme in trade direction
    stop_atr: float = 1.5
    target_r: float | None = 2.0
    btc_filter: bool = True


def v2_signals(s: Series, p: V2Params = V2Params(), btc: Series | None = None) -> dict:
    """Breakout-with-trend candidate on the supplied timeframe (intended: 1h).

    Returns {index: Sig}. BTC alignment uses the BTC bar with the same open time.
    """
    hi, lo, vol = prior_extremes(s, p.donchian)
    trend = ema(s.c, p.trend_ema)
    a, a_slow = atr(s, p.atr_bars), atr(s, p.atr_slow)
    er = efficiency(s.c, p.er_bars)
    btc_trend = btc_index = None
    if p.btc_filter and btc is not None:
        btc_trend = ema(btc.c, p.trend_ema)
        btc_index = {t: i for i, t in enumerate(btc.t)}
    out = {}
    start = max(p.donchian, p.trend_ema+p.slope_bars, p.atr_slow+1, p.er_bars)
    for i in range(start, len(s)):
        if None in (hi[i], trend[i], trend[i-p.slope_bars], a[i], a_slow[i-1], er[i]):
            continue
        c, rng = s.c[i], s.h[i]-s.l[i]
        if vol[i] <= 0 or s.v[i] < vol[i]*p.volume_mult or rng <= 0 or a[i] <= 0:
            continue
        side = 0
        if c > hi[i] and c > trend[i] > trend[i-p.slope_bars]:
            side, level, loc = 1, hi[i], (c-s.l[i])/rng
        elif c < lo[i] and c < trend[i] < trend[i-p.slope_bars]:
            side, level, loc = -1, lo[i], (s.h[i]-c)/rng
        if not side:
            continue
        if er[i] < p.er_min or loc < p.min_close_location:
            continue
        if abs(c-level) > p.max_chase_atr*a[i]:
            continue
        if a[i-1]/a_slow[i-1] > p.max_vol_expansion:
            continue
        if btc_trend is not None:
            j = btc_index.get(s.t[i])
            if j is None or btc_trend[j] is None:
                continue
            if side*(btc.c[j]-btc_trend[j]) < 0:
                continue
        out[i] = Sig(side, c, p.stop_atr*a[i], p.target_r, a[i],
                     score=er[i]*s.v[i]/vol[i])
    return out
