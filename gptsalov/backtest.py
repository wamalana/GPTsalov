"""Historical replay for strategy research. Public static files only; never places orders.

Purpose: answer "does the signal have an edge AFTER costs?" on years of data in
minutes, instead of waiting weeks for a handful of forward-paper trades.

    # on a machine that may reach data.binance.vision (e.g. the VPS)
    python3 -m gptsalov.backtest download --dir data/hist --start 2024-01 --end 2026-08
    python3 -m gptsalov.backtest run --dir data/hist --split 2026-01-01
    python3 -m gptsalov.backtest run --dir data/hist --split 2026-01-01 --cost-mult 1.5

Execution model mirrors paper.py where it matters: signal on a closed bar,
entry at the NEXT bar open with adverse slippage, stop-first when stop and
target touch in one bar, gap fills at the worse open, one position at a time,
risk-based sizing. Known simplifications (see STRATEGY_V2.md): no exchange
step/minimum rounding, no partial fills, universe limited to downloaded
symbols (survivorship bias), float arithmetic, risk locks counted not enforced.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import random
import statistics
import zipfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .core import BAR_MS, Candle, strategy
from .strategy_v2 import Series, Sig, V2Params, resample, v2_signals

HOUR_MS = 3_600_000
BANGKOK = ZoneInfo("Asia/Bangkok")
VISION = "https://data.binance.vision/data/futures/um/monthly"
# Liquid USDT perpetuals. Survivorship caveat: chosen with today's knowledge.
DEFAULT_SYMBOLS = ("BTCUSDT ETHUSDT SOLUSDT XRPUSDT DOGEUSDT BNBUSDT ADAUSDT LINKUSDT "
                   "AVAXUSDT SUIUSDT LTCUSDT BCHUSDT DOTUSDT TRXUSDT NEARUSDT AAVEUSDT "
                   "UNIUSDT FILUSDT APTUSDT ARBUSDT").split()


# ----------------------------------------------------------------- data

def months(start: str, end: str):
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        y, m = (y+1, 1) if m == 12 else (y, m+1)


def download(root, symbols, start, end, funding=True):
    """Fetch monthly zips from Binance's public data archive. Skips existing files."""
    root = Path(root)
    for sym in symbols:
        for ym in months(start, end):
            jobs = [(f"{VISION}/klines/{sym}/15m/{sym}-15m-{ym}.zip", root/"klines"/sym/f"{ym}.csv")]
            if funding:
                jobs.append((f"{VISION}/fundingRate/{sym}/{sym}-fundingRate-{ym}.zip",
                             root/"funding"/sym/f"{ym}.csv"))
            for url, dest in jobs:
                if dest.exists():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with urlopen(Request(url, headers={"User-Agent": "GPTsalov-research"}), timeout=30) as r:
                        blob = r.read()
                except Exception as exc:  # missing month (listing date) is normal
                    print(f"skip {url}: {type(exc).__name__}")
                    continue
                with zipfile.ZipFile(io.BytesIO(blob)) as z:
                    dest.write_bytes(z.read(z.namelist()[0]))
                print(f"ok {dest}")


def _rows(path):
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if row and row[0][:1].isdigit():
                yield row


def load_klines(root, sym) -> Series | None:
    files = sorted((Path(root)/"klines"/sym).glob("*.csv"))
    if not files:
        return None
    bars = {}
    for path in files:
        for r in _rows(path):
            t = int(r[0])
            t = t//1000 if t > 10**14 else t  # tolerate microsecond archives
            bars[t] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7]))
    ts = sorted(bars)
    cols = list(zip(*(bars[t] for t in ts)))
    return Series(ts, *map(list, cols))


def load_funding(root, sym):
    out = []
    for path in sorted((Path(root)/"funding"/sym).glob("*.csv")):
        for r in _rows(path):
            out.append((int(r[0]), float(r[2])))  # calc_time, last_funding_rate
    return sorted(out)


def synthetic(n=6000, seed=1, drift=0.0, vol=0.004, start=1_700_000_000_000//BAR_MS*BAR_MS) -> Series:
    """Random walk with volatility clusters. A strategy with no edge must lose ~costs here."""
    rnd = random.Random(seed)
    t, o, h, l, c, v, qv = [], [], [], [], [], [], []
    price, sigma = 100.0, vol
    for i in range(n):
        sigma = max(0.0015, min(0.02, sigma*(1+rnd.gauss(0, 0.05))))
        ret = rnd.gauss(drift, sigma)
        close = price*(1+ret)
        hi = max(price, close)*(1+abs(rnd.gauss(0, sigma/2)))
        lo = min(price, close)*(1-abs(rnd.gauss(0, sigma/2)))
        vol_ = 1000*(1+abs(ret)/sigma)*rnd.uniform(0.6, 1.4)
        t.append(start+i*BAR_MS); o.append(price); h.append(hi); l.append(lo); c.append(close)
        v.append(vol_); qv.append(vol_*close*1e4)
        price = close
    return Series(t, o, h, l, c, v, qv)


# ----------------------------------------------------------------- signals

def baseline_signals(sym, s: Series) -> dict:
    """EXACT production core.strategy on the same 199 closed bars the live scan sees.

    A cheap float pre-filter (breakout + volume, both necessary conditions)
    avoids building Decimal windows on bars that cannot signal.
    """
    out, cache = {}, {}

    def candle(k):
        if k not in cache:
            cache[k] = Candle(s.t[k], s.t[k]+BAR_MS-1, repr(s.o[k]), repr(s.h[k]),
                              repr(s.l[k]), repr(s.c[k]), repr(s.v[k]))
        return cache[k]

    for i in range(198, len(s)):
        prior_v = sum(s.v[i-20:i])/20
        if prior_v <= 0 or s.v[i] < prior_v*1.2:
            continue
        if not (s.c[i] > max(s.h[i-20:i]) or s.c[i] < min(s.l[i-20:i])):
            continue
        if s.t[i]-s.t[i-198] != 198*BAR_MS:  # live system halts on gaps
            continue
        sig = strategy(sym, [candle(k) for k in range(i-198, i+1)])
        if sig:
            ref, stop, target = float(sig.reference), float(sig.stop), float(sig.target)
            out[i] = Sig(sig.side, ref, abs(ref-stop), None, abs(ref-stop)/1.5,
                         float(sig.score), stop=stop, target=target)
    return out


# ----------------------------------------------------------------- engine

@dataclass(frozen=True)
class Engine:
    bar_ms: int = BAR_MS
    initial_equity: float = 100.0
    risk_fraction: float = 0.005
    leverage: float = 2.0
    max_notional_fraction: float = 1.0
    fee_bps: float = 5.0
    slippage_bps: float = 3.0
    funding_reserve_bps: float = 10.0
    cost_mult: float = 1.0          # stress multiplier on fee + slippage
    max_hold_bars: int = 16
    max_entry_drift: float = 0.005  # baseline absolute drift guard
    reanchor: bool = False          # stop/target measured from the ACTUAL entry
    max_adverse_drift_r: float | None = None  # reject if entry already moved > x * stop_dist
    cost_gate: float | None = None  # reject if round-trip cost > x * stop_dist
    exit_mode: str = "fixed"        # fixed | trail
    breakeven_r: float = 1.0
    trail_atr: float = 2.5
    shortlist: int = 20
    min_quote_volume_24h: float = 10_000_000
    long_only: bool = False

    @property
    def fee(self):
        return self.fee_bps*self.cost_mult/10000

    @property
    def slip(self):
        return self.slippage_bps*self.cost_mult/10000

    @property
    def reserve(self):
        return self.funding_reserve_bps/10000


@dataclass
class Trade:
    symbol: str
    side: int
    open_ms: int
    close_ms: int
    reason: str
    entry: float
    exit: float
    qty: float
    gross: float
    costs: float
    net: float
    risk: float
    bars: int

    @property
    def r(self):
        return self.net/self.risk


@dataclass
class Result:
    trades: list = field(default_factory=list)
    rejects: dict = field(default_factory=dict)
    lock_triggers: dict = field(default_factory=lambda: {"daily_2pct": 0, "drawdown_8pct": 0, "loss_streak_3": 0})
    final_equity: float = 0.0
    max_drawdown: float = 0.0


def _rolling_qv(s: Series, bars):
    out, run = [], 0.0
    for i, x in enumerate(s.qv):
        run += x
        if i >= bars:
            run -= s.qv[i-bars]
        out.append(run if i >= bars-1 else 0.0)
    return out


def simulate(data: dict, signals: dict, eng: Engine, funding: dict | None = None) -> Result:
    """data: {sym: Series}; signals: {sym: {bar_index: Sig}} on the same timeframe."""
    funding = funding or {}
    idx = {sym: {t: i for i, t in enumerate(s.t)} for sym, s in data.items()}
    qv = {sym: _rolling_qv(s, 86_400_000//eng.bar_ms) for sym, s in data.items()}
    by_time = {}
    for sym, sigs in signals.items():
        for i, sig in sigs.items():
            by_time.setdefault(data[sym].t[i], []).append((sym, sig))
    timeline = sorted({t for s in data.values() for t in s.t})
    res = Result()
    balance = high = day_start = eng.initial_equity
    day, streak, pos, pending = None, 0, None, None
    daily_hit = dd_hit = False

    def reject(reason):
        res.rejects[reason] = res.rejects.get(reason, 0)+1

    def close(ref, reason, t_close):
        nonlocal balance, pos, streak, high
        p = pos
        fill = ref*(1-p["side"]*eng.slip)
        gross = p["side"]*(fill-p["entry"])*p["qty"]
        exit_fee = fill*p["qty"]*eng.fee
        fund = p["reserve"]
        if funding.get(p["sym"]):
            fund = sum(rate*p["side"]*p["entry"]*p["qty"] for ft, rate in funding[p["sym"]]
                       if p["open_ms"] < ft <= t_close)
        costs = p["entry_fee"]+exit_fee+fund
        net = gross-costs
        balance += net
        res.trades.append(Trade(p["sym"], p["side"], p["open_ms"], t_close, reason, p["entry"], fill,
                                p["qty"], gross, costs, net, p["risk"], p["bars"]))
        streak = streak+1 if net < 0 else 0
        if streak == 3:
            res.lock_triggers["loss_streak_3"] += 1
        high = max(high, balance)
        res.max_drawdown = max(res.max_drawdown, 1-balance/high)
        pos = None

    for t in timeline:
        d = datetime.fromtimestamp(t/1000, timezone.utc).astimezone(BANGKOK).date()
        if d != day:
            day, day_start, daily_hit = d, balance, False
        # 1) pending intent executes at this bar's open (strictly after the signal bar)
        if pending and pos is None:
            sym, sig, due = pending
            if t >= due:
                pending = None
                i = idx[sym].get(due)
                if t == due and i is not None:
                    s = data[sym]
                    pos = _open(sym, sig, s, i, balance, eng, reject)
        # 2) manage the open position on this bar (entry bar included, like paper.py)
        if pos is not None:
            s = data[pos["sym"]]
            i = idx[pos["sym"]].get(t)
            if i is not None:
                side = pos["side"]
                pos["bars"] += 1
                o, h, l, c = s.o[i], s.h[i], s.l[i], s.c[i]
                t_close = t+eng.bar_ms-1
                stop_hit = l <= pos["stop"] if side == 1 else h >= pos["stop"]
                tgt = pos["target"]
                target_hit = tgt is not None and (h >= tgt if side == 1 else l <= tgt)
                if stop_hit:
                    ref = min(pos["stop"], o) if side == 1 else max(pos["stop"], o)
                    close(ref, "STOP", t_close)
                elif target_hit:
                    close(tgt, "TARGET", t_close)
                elif pos["bars"] >= eng.max_hold_bars:
                    close(c, "TIME", t_close)
                elif eng.exit_mode == "trail":
                    pos["best"] = max(pos["best"], h) if side == 1 else min(pos["best"], l)
                    if side*(c-pos["entry"]) >= eng.breakeven_r*pos["dist"]:
                        be = pos["entry"]*(1+side*(2*eng.fee+2*eng.slip+eng.reserve))
                        pos["stop"] = max(pos["stop"], be) if side == 1 else min(pos["stop"], be)
                    trail = pos["best"]-side*eng.trail_atr*pos["atr"]
                    if side*(trail-pos["entry"]) > 0:  # only trail once in profit
                        pos["stop"] = max(pos["stop"], trail) if side == 1 else min(pos["stop"], trail)
                if balance <= day_start*0.98 and not daily_hit:
                    daily_hit = True
                    res.lock_triggers["daily_2pct"] += 1
                if balance <= high*0.92 and not dd_hit:
                    dd_hit = True
                    res.lock_triggers["drawdown_8pct"] += 1
                elif balance > high*0.92:
                    dd_hit = False
        # 3) new intent only when flat, from signals on the bar that just closed
        if pos is None and pending is None and t in by_time:
            ranked = _eligible(by_time[t], t, data, idx, qv, eng)
            for sym, sig in ranked:
                if eng.long_only and sig.side != 1:
                    continue
                pending = (sym, sig, t+eng.bar_ms)
                break
    res.final_equity = balance
    return res


def _eligible(cands, t, data, idx, qv, eng):
    """Point-in-time liquidity rank among loaded symbols (not the full exchange)."""
    live = sorted(((qv[s][idx[s][t]], s) for s in data if t in idx[s]), reverse=True)
    top = {s for v, s in live[:eng.shortlist] if v >= eng.min_quote_volume_24h}
    return sorted(((s, g) for s, g in cands if s in top), key=lambda x: (-x[1].score, x[0]))


def _open(sym, sig, s, i, balance, eng: Engine, reject):
    side, o = sig.side, s.o[i]
    entry = o*(1+side*eng.slip)
    if eng.reanchor:
        drift = side*(o-sig.reference)
        if eng.max_adverse_drift_r is not None and drift > eng.max_adverse_drift_r*sig.stop_dist:
            reject("ADVERSE_DRIFT"); return None
        stop = entry-side*sig.stop_dist
        r_mult = sig.target_r if sig.target_r is not None else (
            abs(sig.target-sig.reference)/sig.stop_dist if sig.target is not None else None)
        target = None if (r_mult is None or eng.exit_mode == "trail") else entry+side*r_mult*sig.stop_dist
    else:
        if abs(o/sig.reference-1) > eng.max_entry_drift:
            reject("ENTRY_DRIFT"); return None
        stop, target = sig.stop, sig.target
        if stop is None:
            stop = sig.reference-side*sig.stop_dist
            target = sig.reference+side*sig.target_r*sig.stop_dist if sig.target_r else None
    if side*(entry-stop) <= 0 or (target is not None and side*(target-entry) <= 0):
        reject("INVALID_STOP_TARGET"); return None
    dist = abs(entry-stop)
    if eng.cost_gate is not None and entry*(2*eng.fee+2*eng.slip+eng.reserve) > eng.cost_gate*dist:
        reject("COST_GATE"); return None
    stop_fill = stop*(1-side*eng.slip)
    unit_risk = abs(entry-stop_fill)+eng.fee*(entry+stop_fill)+entry*eng.reserve
    unit_cash = entry/eng.leverage+entry*(2*eng.fee+eng.reserve+2*eng.slip)
    qty = min(balance*eng.risk_fraction/unit_risk, balance*eng.max_notional_fraction/entry, balance/unit_cash)
    if qty <= 0:
        reject("NO_SIZE"); return None
    return {"sym": sym, "side": side, "entry": entry, "stop": stop, "target": target, "qty": qty,
            "risk": qty*unit_risk, "entry_fee": entry*qty*eng.fee, "reserve": entry*qty*eng.reserve,
            "open_ms": s.t[i], "bars": 0, "dist": dist, "atr": sig.atr,
            "best": s.o[i]}


# ----------------------------------------------------------------- metrics

def metrics(trades, seed=7, boot=2000):
    if not trades:
        return {"trades": 0}
    rs = [t.r for t in trades]
    wins = [t.net for t in trades if t.net > 0]
    losses = [-t.net for t in trades if t.net < 0]
    gross_abs = sum(abs(t.gross) for t in trades)
    rnd = random.Random(seed)
    means = sorted(statistics.fmean(rnd.choices(rs, k=len(rs))) for _ in range(boot))
    span_w = max(1, (trades[-1].close_ms-trades[0].open_ms)/604_800_000)
    return {
        "trades": len(trades),
        "trades_per_week": round(len(trades)/span_w, 2),
        "win_rate": round(len(wins)/len(trades), 3),
        "avg_r": round(statistics.fmean(rs), 3),
        "avg_r_ci95": [round(means[int(boot*0.025)], 3), round(means[int(boot*0.975)], 3)],
        "median_r": round(statistics.median(rs), 3),
        "profit_factor": round(sum(wins)/sum(losses), 2) if losses else None,
        "net_pnl": round(sum(t.net for t in trades), 3),
        "gross_pnl": round(sum(t.gross for t in trades), 3),
        "costs": round(sum(t.costs for t in trades), 3),
        "costs_per_abs_gross": round(sum(t.costs for t in trades)/gross_abs, 3) if gross_abs else None,
        "long": _side(trades, 1), "short": _side(trades, -1),
        "exits": {k: sum(1 for t in trades if t.reason == k) for k in sorted({t.reason for t in trades})},
    }


def _side(trades, side):
    sub = [t.r for t in trades if t.side == side]
    return {"n": len(sub), "avg_r": round(statistics.fmean(sub), 3) if sub else None}


# ----------------------------------------------------------------- variants

V1 = Engine()
VARIANTS = {
    # name: (timeframe factor over 15m, signal kind, engine)
    "v1_baseline":      (1, "v1", V1),
    "v1_reanchor":      (1, "v1", replace(V1, reanchor=True, max_adverse_drift_r=0.25)),
    "v1_reanchor_cost": (1, "v1", replace(V1, reanchor=True, max_adverse_drift_r=0.25, cost_gate=0.25)),
    "v2_1h_fixed2R":    (4, "v2", replace(V1, bar_ms=HOUR_MS, max_hold_bars=24, reanchor=True,
                                          max_adverse_drift_r=0.25, cost_gate=0.25)),
    "v2_1h_trail":      (4, "v2", replace(V1, bar_ms=HOUR_MS, max_hold_bars=48, reanchor=True,
                                          max_adverse_drift_r=0.25, cost_gate=0.25, exit_mode="trail")),
}


def run_variant(name, data15: dict, funding=None, cost_mult=1.0, v2_params=V2Params()):
    factor, kind, eng = VARIANTS[name]
    eng = replace(eng, cost_mult=cost_mult)
    data = data15 if factor == 1 else {s: resample(x, factor, BAR_MS) for s, x in data15.items()}
    if kind == "v1":
        sigs = {s: baseline_signals(s, x) for s, x in data.items()}
    else:
        btc = data.get("BTCUSDT")
        sigs = {s: v2_signals(x, v2_params, btc) for s, x in data.items()}
    return simulate(data, sigs, eng, funding)


def split_report(res: Result, split_ms=None):
    out = {"all": metrics(res.trades), "final_equity": round(res.final_equity, 3),
           "max_drawdown": round(res.max_drawdown, 4), "rejects": res.rejects,
           "lock_triggers_counted_not_enforced": res.lock_triggers}
    if split_ms:
        out["in_sample"] = metrics([t for t in res.trades if t.open_ms < split_ms])
        out["out_of_sample"] = metrics([t for t in res.trades if t.open_ms >= split_ms])
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--dir", required=True)
    d.add_argument("--start", required=True, help="YYYY-MM")
    d.add_argument("--end", required=True, help="YYYY-MM (last COMPLETE month)")
    d.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    d.add_argument("--no-funding", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--dir", help="downloaded data root; omit with --synthetic")
    r.add_argument("--synthetic", action="store_true", help="random-walk sanity check (no edge expected)")
    r.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    r.add_argument("--variants", nargs="*", default=list(VARIANTS))
    r.add_argument("--split", help="YYYY-MM-DD: report in-sample vs out-of-sample separately")
    r.add_argument("--cost-mult", type=float, default=1.0)
    r.add_argument("--flat-funding", action="store_true", help="ignore funding files; use fixed reserve")
    r.add_argument("--out", help="write JSON report here")
    a = p.parse_args(argv)
    if a.cmd == "download":
        download(a.dir, a.symbols, a.start, a.end, not a.no_funding)
        return
    if a.synthetic:
        data = {f"SYN{k}USDT": synthetic(seed=k) for k in range(6)}
        funding = {}
    else:
        if not a.dir:
            p.error("--dir required unless --synthetic")
        data = {s: x for s in a.symbols if (x := load_klines(a.dir, s))}
        funding = {} if a.flat_funding else {s: load_funding(a.dir, s) for s in data}
        if not data:
            p.error("no kline files found; run download first")
    split_ms = None
    if a.split:
        split_ms = int(datetime.fromisoformat(a.split).replace(tzinfo=timezone.utc).timestamp()*1000)
    report = {"symbols": sorted(data), "cost_mult": a.cost_mult,
              "funding": "flat_reserve" if not any(funding.values()) else "binance_archive",
              "variants": {}}
    for name in a.variants:
        res = run_variant(name, data, funding, a.cost_mult)
        report["variants"][name] = split_report(res, split_ms)
        m = report["variants"][name]["all"]
        print(f"{name:18s} n={m.get('trades', 0):4d} win={m.get('win_rate')} avgR={m.get('avg_r')} "
              f"CI={m.get('avg_r_ci95')} PF={m.get('profit_factor')} eq={res.final_equity:.2f} "
              f"maxDD={res.max_drawdown:.1%}", flush=True)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if a.out:
        Path(a.out).write_text(text)
    else:
        print(text)


if __name__ == "__main__":
    main()
