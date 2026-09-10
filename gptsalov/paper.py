from __future__ import annotations

from dataclasses import asdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import json
import sqlite3
import time
from zoneinfo import ZoneInfo

from .core import BAR_MS, Config, D, Signal, dec, encode, size
from .market import MarketError, Snapshot

SCHEMA_VERSION = 1
THAILAND = ZoneInfo("Asia/Bangkok")


def day_at(timestamp):
    return datetime.fromtimestamp(timestamp/1000, timezone.utc).astimezone(THAILAND).date().isoformat()


class Store:
    """One writer per database + atomic state/events commit. No reset command."""
    def __init__(self, path: str, cfg: Config, source: str):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_suffix(self.path.suffix+".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Another writer owns this paper database")
        try:
            self.db = sqlite3.connect(self.path, timeout=5)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, timestamp_ms INTEGER NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL)")
            row = self.db.execute("SELECT data FROM state WHERE id=1").fetchone()
            if row:
                state = json.loads(row[0])
                if state["schema"] != SCHEMA_VERSION or state["config_hash"] != cfg.fingerprint:
                    raise ValueError("Schema/config mismatch: review before changing an existing ledger")
                if state["source"] != source:
                    raise ValueError("Cannot mix synthetic and real-market data in one ledger")
            else:
                state = {"schema": SCHEMA_VERSION, "config_hash": cfg.fingerprint,
                         "source": source, "mode": "paper", "balance": str(cfg.initial_equity),
                         "initial_equity": str(cfg.initial_equity), "equity": str(cfg.initial_equity),
                         "max_data_age_ms": cfg.max_data_age_ms,
                         "high_water": str(cfg.initial_equity), "day_start": str(cfg.initial_equity),
                         "day": None, "daily_locked": False, "hard_lock": None,
                         "position": None, "pending": None, "last_close_ms": None,
                         "as_of_ms": None, "observed_at_ms": None, "last_error": None,
                         "loss_streak": 0, "closed_trades": 0, "wins": 0,
                         "gross_realized": "0", "fees": "0", "funding_reserve_charged": "0",
                         "last_scan": {}, "strategy": "hourly_ema20_donchian20_volume_v1"}
                with self.db:
                    self.db.execute("INSERT INTO state VALUES (1, ?)", (encode(state),))
            self.state = state
        except Exception:
            if hasattr(self, "db"):
                self.db.close()
            self.lock.close()
            raise

    def commit(self, state, events=()):
        with self.db:
            self.db.execute("UPDATE state SET data=? WHERE id=1", (encode(state),))
            self.db.executemany("INSERT INTO events(timestamp_ms,kind,data) VALUES (?,?,?)",
                                [(stamp, kind, encode(data)) for stamp, kind, data in events])
        self.state = json.loads(encode(state))

    def close(self):
        self.db.close()
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class PaperEngine:
    def __init__(self, cfg: Config, store: Store):
        self.cfg, self.store = cfg, store

    def required_symbols(self):
        state = self.store.state
        if state["position"]:
            return [state["position"]["symbol"]]
        if state["pending"]:
            return [state["pending"]["signal"]["symbol"]]
        return []

    def fault(self, message, timestamp):
        state = json.loads(encode(self.store.state))
        state["last_error"] = message
        state["observed_at_ms"] = timestamp
        # A failed fetch never advances market as_of or invents a price/fill.
        self.store.commit(state, [(timestamp, "DATA_ERROR", {"message": message})])

    def step(self, snapshot: Snapshot):
        state = json.loads(encode(self.store.state))
        events = []
        cfg = self.cfg
        if state["source"] != snapshot.source:
            raise ValueError("SOURCE_MISMATCH")
        previous = state["last_close_ms"]
        stamp = snapshot.latest_close
        if previous is not None and stamp < previous:
            raise MarketError("OUT_OF_ORDER_SNAPSHOT")
        # All supplied candles must be closed, contiguous and current.
        for symbol, bars in snapshot.bars.items():
            if not bars or bars[-1].close_ms != stamp:
                raise MarketError(f"STALE_CANDLES:{symbol}")
            for i, bar in enumerate(bars):
                if (bar.close_ms >= snapshot.now_ms or bar.close_ms-bar.open_ms != BAR_MS-1
                        or bar.open_ms % BAR_MS
                        or (i and bar.open_ms != bars[i-1].open_ms+BAR_MS)):
                    raise MarketError(f"INVALID_CANDLE_SEQUENCE:{symbol}")
        state["observed_at_ms"] = snapshot.now_ms
        state["last_error"] = None
        state["last_scan"] = snapshot.audit
        if previous == stamp:
            self.store.commit(state)
            return []
        if state["position"] and state["position"]["symbol"] not in snapshot.bars:
            raise MarketError("MISSING_POSITION_MARKET_DATA")
        if previous is not None and stamp-previous != BAR_MS:
            state["pending"] = None
            if state["position"]:
                state["hard_lock"] = "DATA_GAP_REQUIRES_RECONCILIATION"
                state["last_error"] = state["hard_lock"]
                self.store.commit(state, [(snapshot.now_ms, "HALT", {"reason": state["hard_lock"]})])
                raise MarketError(state["hard_lock"])
            events.append((stamp, "FLAT_RESYNC", {"from": previous, "to": stamp}))

        balance = dec(state["balance"])
        today = day_at(stamp)
        if state["day"] != today:
            state["day"] = today
            state["day_start"] = state["equity"]
            state["daily_locked"] = False

        def close_position(reference, reason, when):
            nonlocal balance
            p = state["position"]
            fill = reference*(1-p["side"]*cfg.slippage_bps/10000)
            gross = p["side"]*(fill-dec(p["entry"]))*dec(p["qty"])
            exit_fee = fill*dec(p["qty"])*cfg.fee_bps/10000
            net = gross-exit_fee-dec(p["entry_fee"])-dec(p["funding_reserve"])
            balance += gross-exit_fee
            state["gross_realized"] = str(dec(state["gross_realized"])+gross)
            state["fees"] = str(dec(state["fees"])+exit_fee)
            state["closed_trades"] += 1
            state["wins"] += int(net > 0)
            state["loss_streak"] = state["loss_streak"]+1 if net < 0 else 0
            if state["loss_streak"] >= cfg.max_consecutive_losses:
                state["hard_lock"] = "LOSS_STREAK_REVIEW"
            events.append((when, "PAPER_CLOSE", {**p, "exit": str(fill), "reason": reason,
                                                "gross_pnl": str(gross), "net_pnl": str(net),
                                                "exit_fee": str(exit_fee)}))
            state["position"] = None

        pending = state["pending"]
        if pending and not state["position"] and not state["hard_lock"] and not state["daily_locked"]:
            signal = Signal.restore(pending["signal"])
            bars = snapshot.bars.get(signal.symbol, [])
            bar = bars[-1] if bars else None
            if bar and bar.open_ms >= pending["execute_open_ms"]:
                state["pending"] = None
                try:
                    if bar.open_ms != pending["execute_open_ms"]:
                        raise ValueError("MISSED_ENTRY_BAR")
                    plan = size(signal, bar.open, balance, snapshot.rules[signal.symbol], cfg)
                    entry_fee = plan.notional*cfg.fee_bps/10000
                    reserve = plan.notional*cfg.funding_reserve_bps/10000
                    balance -= entry_fee+reserve
                    state["fees"] = str(dec(state["fees"])+entry_fee)
                    state["funding_reserve_charged"] = str(dec(state["funding_reserve_charged"])+reserve)
                    state["position"] = {"symbol": signal.symbol, "side": signal.side,
                                         "entry": str(plan.entry), "stop": str(plan.stop),
                                         "target": str(plan.target), "qty": str(plan.qty),
                                         "entry_fee": str(entry_fee), "funding_reserve": str(reserve),
                                         "open_ms": bar.open_ms, "bars_held": 0,
                                         "signal_id": signal.key, "modeled_risk": str(plan.risk)}
                    events.append((bar.open_ms, "PAPER_OPEN", state["position"].copy()))
                except (ValueError, KeyError) as exc:
                    events.append((stamp, "REJECT", {"symbol": signal.symbol, "reason": str(exc)}))
            elif snapshot.latest_close >= pending["execute_open_ms"]:
                state["pending"] = None
                events.append((stamp, "REJECT", {"reason": "MISSING_ENTRY_DATA"}))

        p = state["position"]
        mark = None
        if p:
            bar = snapshot.bars[p["symbol"]][-1]
            p["bars_held"] += 1
            stop, target = dec(p["stop"]), dec(p["target"])
            stop_hit = bar.low <= stop if p["side"] == 1 else bar.high >= stop
            target_hit = bar.high >= target if p["side"] == 1 else bar.low <= target
            if stop_hit:
                # If both SL/TP touch, assume stop first; gaps receive worse open.
                reference = min(stop, bar.open) if p["side"] == 1 else max(stop, bar.open)
                close_position(reference, "STOP_FIRST_CONSERVATIVE", stamp)
            elif target_hit:
                close_position(target, "TARGET", stamp)
            elif p["bars_held"] >= cfg.max_hold_bars:
                close_position(bar.close, "TIME_EXIT", stamp)
            else:
                mark = bar.close
                p["mark"] = str(mark)

        def equity_now():
            p = state["position"]
            if not p:
                return balance
            # Liquidation-value estimate includes modeled adverse exit and fee.
            liquidation_value = dec(p["mark"])*(1-p["side"]*cfg.slippage_bps/10000)
            return (balance+p["side"]*(liquidation_value-dec(p["entry"]))*dec(p["qty"])
                    -liquidation_value*dec(p["qty"])*cfg.fee_bps/10000)

        equity = equity_now()
        high_water = max(dec(state["high_water"]), equity)
        state["high_water"] = str(high_water)
        if equity <= dec(state["day_start"])*(1-cfg.daily_loss_fraction):
            state["daily_locked"] = True
        if equity <= high_water*(1-cfg.drawdown_fraction):
            state["hard_lock"] = "DRAWDOWN_REVIEW"
        if state["daily_locked"] or state["hard_lock"]:
            state["pending"] = None
            if state["position"]:
                close_position(mark, state["hard_lock"] or "DAILY_LOSS", stamp)
                equity = equity_now()

        # Commit an intent only while the signal is fresh, never an earlier fill.
        if not state["position"] and not state["pending"] and not state["daily_locked"] and not state["hard_lock"]:
            for signal in snapshot.signals:
                try:
                    if signal.bar_close_ms != stamp or not 0 <= snapshot.now_ms-stamp <= cfg.max_data_age_ms:
                        raise ValueError("STALE_SIGNAL")
                    size(signal, signal.reference, equity, snapshot.rules[signal.symbol], cfg)
                    state["pending"] = {"signal": asdict(signal), "observed_ms": snapshot.now_ms,
                                        "execute_open_ms": (snapshot.now_ms//BAR_MS+1)*BAR_MS}
                    events.append((snapshot.now_ms, "PAPER_INTENT", state["pending"].copy()))
                    break
                except (ValueError, KeyError) as exc:
                    events.append((stamp, "REJECT", {"symbol": signal.symbol, "reason": str(exc)}))
        state["balance"], state["equity"] = str(balance), str(equity)
        state["as_of_ms"], state["last_close_ms"] = stamp, stamp
        self.store.commit(state, events)
        return events


def report(path: str, now_ms=None):
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    uri = Path(path).resolve().as_uri()+"?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("BEGIN")
        row = db.execute("SELECT data FROM state WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Empty ledger")
        state = json.loads(row[0])
        counts = dict(db.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind"))
        trades = [{**json.loads(data), "close_ms": stamp} for stamp, data in
                  db.execute("SELECT timestamp_ms,data FROM events WHERE kind='PAPER_CLOSE' ORDER BY id")]
    state["event_counts"] = counts
    state["recent_trades"] = trades[-10:]
    state["net_pnl_estimate"] = str(dec(state["equity"])-dec(state["initial_equity"]))
    state["drawdown_fraction"] = str(1-dec(state["equity"])/dec(state["high_water"]))
    state["win_rate"] = state["wins"]/state["closed_trades"] if state["closed_trades"] else None
    gains = sum((dec(t["net_pnl"]) for t in trades if dec(t["net_pnl"]) > 0), D(0))
    losses = -sum((dec(t["net_pnl"]) for t in trades if dec(t["net_pnl"]) < 0), D(0))
    state["profit_factor"] = str(gains/losses) if losses else None
    state["execution"] = "CANDLE_SIMULATOR_NO_REAL_ORDERS"
    state["funding_model"] = "FIXED_RESERVE_PER_TRADE_NOT_EXCHANGE_PAYMENTS"
    state["news_status"] = "NOT_CONNECTED"
    state["probability_model"] = "NOT_CALIBRATED"
    state["live_trading_supported"] = False
    state["report_generated_ms"] = now_ms
    window = [t for t in trades if now_ms-4*3600000 <= t['close_ms'] <= now_ms]
    state['closed_trades_last_4h'] = len(window)
    state['closed_trade_net_pnl_last_4h'] = str(sum((dec(t['net_pnl']) for t in window), D(0)))
    state['window_start_ms'] = now_ms-4*3600000
    state['observed_age_ms'] = now_ms-state['observed_at_ms'] if state['observed_at_ms'] is not None else None
    state["data_age_ms"] = now_ms-state["as_of_ms"] if state["as_of_ms"] is not None else None
    if state["last_error"]:
        state["health"] = "ERROR_MARKS_MAY_BE_STALE"
    elif state["hard_lock"] or state["daily_locked"]:
        state["health"] = "LOCKED"
    elif state["source"] == "synthetic-demo":
        state["health"] = "SYNTHETIC_NOT_LIVE"
    elif state["data_age_ms"] is None or not 0 <= state["data_age_ms"] <= BAR_MS+state.get("max_data_age_ms", 120000):
        state["health"] = "STALE_OR_MISSING"
    else:
        state["health"] = "PAPER_DATA_CURRENT"
    return state
