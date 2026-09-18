"""Forward paper ledger for the 4h trend-following candidate (backtest variant v3_1_4h_trail).

Research only: public GET endpoints, no credentials, no orders. Position rules
are the SAME functions the historical replay uses (backtest.manage_bar /
close_math / _open), so forward results are directly comparable with
research/backtest-2026-09-18. Entry is modeled at the open of the bar after
the signal bar, filled when that bar closes (same delayed semantics as paper.py).

    python3 -m gptsalov.trend4h run --db data/trend4h/ledger.db --cycles 0
    python3 -m gptsalov.trend4h status --db data/trend4h/ledger.db
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path

from .backtest import VARIANTS, Engine, _open, close_math, manage_bar
from .core import closed_bars, encode
from .market import BinancePublic, MarketError
from .strategy_v2 import Series, Sig, V2Params, v2_signals

VARIANT = "v3_1_4h_trail"
BAR_MS = 4*3_600_000
LOOKBACK = 120  # bars; v2 needs ~60 for EMA50 slope, ATR50 and ER20
SCHEMA = 1


def engine() -> Engine:
    return VARIANTS[VARIANT][2]


class Ledger:
    """Single-writer SQLite ledger with atomic state + events commit. No reset command."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_suffix(self.path.suffix+".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Another writer owns this ledger")
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, timestamp_ms INTEGER NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL)")
        row = self.db.execute("SELECT data FROM state WHERE id=1").fetchone()
        eng = engine()
        identity = {"schema": SCHEMA, "variant": VARIANT, "engine": asdict(eng), "params": asdict(V2Params()),
                    "universe": "top20_usdt_perp_by_24h_quote_volume_plus_btc"}
        if row:
            self.state = json.loads(row[0])
            if self.state["identity"] != identity:
                self.close()
                raise ValueError("Ledger identity mismatch: start a new ledger for a changed variant")
        else:
            self.state = {"identity": identity, "balance": eng.initial_equity, "high_water": eng.initial_equity,
                          "positions": [], "pending": [], "last_close_ms": None, "observed_at_ms": None,
                          "last_error": None, "closed_trades": 0, "wins": 0, "loss_streak": 0,
                          "rejects": {}, "last_signals": []}
            with self.db:
                self.db.execute("INSERT INTO state VALUES (1, ?)", (encode(self.state),))

    def commit(self, events=()):
        with self.db:
            self.db.execute("UPDATE state SET data=? WHERE id=1", (encode(self.state),))
            self.db.executemany("INSERT INTO events(timestamp_ms,kind,data) VALUES (?,?,?)",
                                [(t, k, encode(d)) for t, k, d in events])

    def close(self):
        self.db.close()
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def universe(client, now_ms, shortlist=20, min_age_days=30):
    """Point-in-time liquidity shortlist: top-N USDT perpetuals by 24h quote volume, plus BTC.

    Mirrors the historical replay's rolling-volume ranking so the forward ledger
    does not carry a hand-picked (survivorship-biased) symbol list.
    """
    rows = client.get("/fapi/v1/exchangeInfo")["symbols"]
    vol = {r["symbol"]: float(r["quoteVolume"]) for r in client.get("/fapi/v1/ticker/24hr")}
    ok = []
    for r in rows:
        try:
            if (r["status"] == "TRADING" and r["contractType"] == "PERPETUAL" and r["quoteAsset"] == "USDT"
                    and r["marginAsset"] == "USDT" and r["underlyingType"] == "COIN"
                    and now_ms-int(r["onboardDate"]) >= min_age_days*86_400_000 and vol.get(r["symbol"], 0) > 0):
                ok.append((vol[r["symbol"]], r["symbol"]))
        except (KeyError, TypeError, ValueError):
            continue
    ok.sort(reverse=True)
    return {s for _, s in ok[:shortlist]} | {"BTCUSDT"}


def fetch(client, symbols, now_ms):
    """Closed, contiguous, current 4h bars per symbol. Symbols with bad data are skipped."""
    expected = now_ms//BAR_MS*BAR_MS-1
    out, bad = {}, {}
    for sym in symbols:
        try:
            bars = closed_bars(client.get("/fapi/v1/klines", symbol=sym, interval="4h", limit=LOOKBACK),
                               now_ms, BAR_MS)
            if not bars or bars[-1].close_ms != expected:
                raise ValueError("STALE_CANDLES")
            out[sym] = Series([b.open_ms for b in bars], [float(b.open) for b in bars], [float(b.high) for b in bars],
                              [float(b.low) for b in bars], [float(b.close) for b in bars],
                              [float(b.volume) for b in bars], [float(b.volume*b.close) for b in bars])
        except (MarketError, ValueError, TypeError, IndexError, ArithmeticError) as exc:
            bad[sym] = type(exc).__name__ if not isinstance(exc, ValueError) else str(exc)
    return out, bad


def v2_params() -> V2Params:
    kind = VARIANTS[VARIANT][1]
    return V2Params(stop_atr=1.0) if kind == "v2_s1" else V2Params()


def _close(st, p, hit, stamp, eng, events):
    trade = close_math(p, hit[1], hit[0], stamp, eng)
    st["balance"] += trade.net
    st["closed_trades"] += 1
    st["wins"] += int(trade.net > 0)
    st["loss_streak"] = st["loss_streak"]+1 if trade.net < 0 else 0
    st["high_water"] = max(st["high_water"], st["balance"])
    st["positions"] = [x for x in st["positions"] if x is not p]
    events.append((stamp, "PAPER_CLOSE", {**asdict(trade), "r": trade.r}))


def step(ledger: Ledger, data: dict, bad: dict, now_ms: int, params: V2Params | None = None):
    """Process the newest closed 4h bar. Idempotent per bar; ordering matches backtest.simulate."""
    st, eng, events = ledger.state, engine(), []
    params = v2_params() if params is None else params
    stamp = now_ms//BAR_MS*BAR_MS-1
    st["observed_at_ms"], st["last_error"], st["data_rejected"] = now_ms, None, bad
    if st["last_close_ms"] == stamp:
        ledger.commit()
        return []
    for p in st["positions"]:
        if p["sym"] not in data or data[p["sym"]].t[-1] != stamp-BAR_MS+1:
            raise MarketError("MISSING_POSITION_MARKET_DATA:"+p["sym"])
    if st["last_close_ms"] is not None and stamp-st["last_close_ms"] != BAR_MS:
        st["pending"] = []
        events.append((stamp, "GAP_RESYNC", {"from": st["last_close_ms"], "to": stamp}))
    bar_open = stamp-BAR_MS+1

    def reject(reason):
        st["rejects"][reason] = st["rejects"].get(reason, 0)+1
        events.append((stamp, "REJECT", {"reason": reason}))

    # 1) pending intents fill at THIS bar's open (the bar after the signal bar)
    for pend in list(st["pending"]):
        st["pending"].remove(pend)
        s = data.get(pend["sym"])
        if pend["due_open_ms"] != bar_open or s is None or s.t[-1] != bar_open:
            reject("MISSED_ENTRY_BAR")
        elif len(st["positions"]) >= eng.max_positions:
            reject("NO_FREE_SLOT")
        else:
            pos = _open(pend["sym"], Sig(**pend["sig"]), s, len(s)-1, st["balance"], eng, reject, pend["budget"])
            if pos:
                st["positions"].append(pos)
                events.append((bar_open, "PAPER_OPEN", dict(pos)))
    # 2) manage open positions on this bar (entry bar included)
    for p in list(st["positions"]):
        s = data[p["sym"]]
        hit = manage_bar(p, s.o[-1], s.h[-1], s.l[-1], s.c[-1], eng)
        if hit:
            _close(st, p, hit, stamp, eng, events)
    # 3) new intents from signals on the bar that just closed, into free slots only
    st["last_signals"] = []
    free = eng.max_positions-len(st["positions"])-len(st["pending"])
    if free > 0:
        btc = data.get("BTCUSDT")
        found = []
        for sym, s in data.items():
            if s.t[-1] != bar_open:
                continue
            sig = v2_signals(s, params, btc).get(len(s)-1)
            if sig and (not eng.long_only or sig.side == 1):
                found.append((sym, sig))
        found.sort(key=lambda x: (-x[1].score, x[0]))
        st["last_signals"] = [{"sym": k, "side": g.side, "score": g.score} for k, g in found]
        busy = {p["sym"] for p in st["positions"]} | {x["sym"] for x in st["pending"]}
        sides = {p["side"] for p in st["positions"]} | {x["sig"]["side"] for x in st["pending"]}
        chosen = []
        for sym, sig in found:
            if sym in busy or (eng.one_per_side and sig.side in sides):
                continue
            chosen.append((sym, sig))
            busy.add(sym)
            sides.add(sig.side)
            if len(chosen) == free:
                break
        if chosen:
            open_risk = sum(p["risk"] for p in st["positions"])
            budget = (st["balance"]*eng.risk_fraction if eng.max_positions == 1
                      else max(0.0, st["balance"]*eng.risk_fraction-open_risk)/free)
            for sym, sig in chosen:
                pend = {"sym": sym, "sig": asdict(sig), "signal_close_ms": stamp, "due_open_ms": stamp+1,
                        "budget": budget}
                st["pending"].append(pend)
                events.append((stamp, "PAPER_INTENT", dict(pend)))
    st["last_close_ms"] = stamp
    ledger.commit(events)
    return events


def run(path, cycles, poll_seconds=60, client=None, clock=time.time, sleep=time.sleep):
    client = client or BinancePublic()
    count = 0
    with closing(Ledger(path)) as ledger:
        while cycles == 0 or count < cycles:
            now = int(clock()*1000)
            try:
                server = int(client.get("/fapi/v1/time")["serverTime"])
                if abs(server-now) > 5000:
                    raise MarketError("Clock skew")
                needed = universe(client, server) | {p["sym"] for p in ledger.state["positions"]} \
                    | {x["sym"] for x in ledger.state["pending"]}
                data, bad = fetch(client, sorted(needed), server)
                events = step(ledger, data, bad, server)
                for _, kind, d in events:
                    print(encode({"kind": kind, **d}), flush=True)
            except MarketError as exc:
                ledger.state["last_error"] = str(exc)
                ledger.state["observed_at_ms"] = now
                ledger.commit([(now, "DATA_ERROR", {"message": str(exc)})])
                if "HTTP" in str(exc):
                    raise  # never loop against an access/rate-limit block
            count += 1
            if cycles == 0 or count < cycles:
                sleep(poll_seconds)


def report(path, now_ms=None):
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    uri = Path(path).resolve().as_uri()+"?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        row = db.execute("SELECT data FROM state WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Empty ledger")
        st = json.loads(row[0])
        trades = [json.loads(d) for (d,) in db.execute("SELECT data FROM events WHERE kind='PAPER_CLOSE' ORDER BY id")]
    rs = [t["r"] for t in trades]
    st["recent_trades"] = trades[-10:]
    st["avg_r"] = sum(rs)/len(rs) if rs else None
    st["win_rate"] = st["wins"]/st["closed_trades"] if st["closed_trades"] else None
    gains = sum(t["net"] for t in trades if t["net"] > 0)
    losses = -sum(t["net"] for t in trades if t["net"] < 0)
    st["profit_factor"] = gains/losses if losses else None
    st["equity_estimate"] = st["balance"]
    for p in st["positions"]:
        if p.get("mark"):
            st["equity_estimate"] += p["side"]*(p["mark"]-p["entry"])*p["qty"]-p["entry_fee"]-p["reserve"]
    st["data_age_ms"] = now_ms-st["last_close_ms"] if st["last_close_ms"] else None
    st["health"] = ("ERROR" if st["last_error"] else "STALE" if st["data_age_ms"] is None
                    or st["data_age_ms"] > BAR_MS+300_000 else "PAPER_DATA_CURRENT")
    st["backtest_reference"] = ("research/backtest-2026-09-18/sweep*.json (v3_1: stop 1.0 ATR, 2 slots one-per-side; "
                                "39-symbol universe avg R +0.25, OOS +0.08 on 75 trades - not yet significant)")
    st["execution"] = "CANDLE_SIMULATOR_NO_REAL_ORDERS"
    return st


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=("run", "status"))
    p.add_argument("--db", required=True)
    p.add_argument("--cycles", type=int, default=0, help="0 = run until stopped")
    a = p.parse_args(argv)
    if a.command == "status":
        print(encode(report(a.db)))
        return
    if a.cycles < 0:
        p.error("cycles must be >= 0")
    run(a.db, a.cycles)


if __name__ == "__main__":
    main()
