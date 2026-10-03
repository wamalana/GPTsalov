"""Prospective entry experiment. Public prices only; never sends orders.

This measures fixed-risk SIGNAL outcomes, not a tradable portfolio. All trials,
including waits and rejected scanner rows, are retained. No ML probability is
reported before an independently evaluated model exists.
"""
import argparse
from contextlib import closing
from dataclasses import asdict
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .core import BAR_MS, D, Signal, closed_bars, encode, hourly, strategy
from .exit_shadow import MINUTE, POLICY as EXIT_POLICY, public_bars, simulate

VARIANTS = ('baseline_v1', 'trend_pullback_v1', 'compression_breakout_v1')
POLICY = dict(version=1, variants=VARIANTS, mode='SIGNAL_STUDY_NO_ORDERS',
              risk_usdt=0.25, risk_definition='price_stop_risk_before_costs',
              horizon_minutes=240, retention='no automatic deletion',
              entry='first_full_minute_after_observation', fee_bps=5,
              slippage_bps=3, funding_reserve_bps=15, ml_status='NOT_TRAINED',
              trend_ema=20, breakout_lookback=20, volume_ratio=1.2,
              compression_atr_ratio=0.8, stop_atr=1.5, target_r=2,
              max_database_mib=512)
FINGERPRINT = hashlib.sha256(encode(POLICY).encode()).hexdigest()


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=2)
    db.execute('PRAGMA busy_timeout=2000')
    page = db.execute('PRAGMA page_size').fetchone()[0]
    db.execute(f'PRAGMA max_page_count={512*1024*1024//page}')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS policy(id INTEGER PRIMARY KEY, fingerprint TEXT, data TEXT);
      CREATE TABLE IF NOT EXISTS scans(scan_id INTEGER PRIMARY KEY, finished_ms INTEGER, data TEXT);
      CREATE TABLE IF NOT EXISTS observations(
        scan_id INTEGER, market TEXT, symbol TEXT, observed_ms INTEGER, data TEXT,
        PRIMARY KEY(scan_id,market,symbol));
      CREATE TABLE IF NOT EXISTS bars(
        market TEXT, symbol TEXT, close_ms INTEGER, first_observed_ms INTEGER, data TEXT,
        PRIMARY KEY(market,symbol,close_ms));
      CREATE TABLE IF NOT EXISTS signals(
        id TEXT PRIMARY KEY, variant TEXT, symbol TEXT, signal_close_ms INTEGER,
        observed_ms INTEGER, entry_ms INTEGER, data TEXT);
      CREATE TABLE IF NOT EXISTS outcomes(
        id TEXT PRIMARY KEY, observed_ms INTEGER, status TEXT, data TEXT);
      CREATE TABLE IF NOT EXISTS health(id INTEGER PRIMARY KEY, data TEXT);
    ''')
    old = db.execute('SELECT fingerprint FROM policy WHERE id=1').fetchone()
    if old and old[0] != FINGERPRINT:
        db.close()
        raise ValueError('Research policy changed: use a new database')
    with db:
        db.execute('INSERT OR IGNORE INTO policy VALUES(1,?,?)', (FINGERPRINT, encode(POLICY)))
    return db


def ema(values, period=20):
    value = sum(values[:period], D(0))/period
    previous = value
    for x in values[period:]:
        previous = value
        value += D(2)/(period+1)*(x-value)
    return value, previous


def assess(symbol, bars, observed_ms):
    """Only contiguous closed 15m bars available at observation time are used."""
    result = {v: {'reason': 'INVALID_OR_STALE_DATA', 'signal': None} for v in VARIANTS}
    if len(bars) < 100 or bars[-1].close_ms >= observed_ms or observed_ms-bars[-1].close_ms > BAR_MS:
        return result, {}
    if any(b.open_ms % BAR_MS or b.close_ms != b.open_ms+BAR_MS-1 for b in bars):
        return result, {}
    if any(b.open_ms != a.open_ms+BAR_MS for a, b in zip(bars, bars[1:])):
        return result, {}
    hours = hourly(bars)
    if len(hours) < 21:
        return result, {}
    current, previous = bars[-1], bars[-2]
    e, old_e = ema([b.close for b in hours])
    local_e, _ = ema([b.close for b in bars[:-1]])
    tr = [max(b.high-b.low, abs(b.high-a.close), abs(b.low-a.close))
          for a,b in zip(bars, bars[1:])]
    atr = sum(tr[-14:], D(0))/14
    prior_atr = sum(tr[-15:-1], D(0))/14
    slow_atr = sum(tr[-51:-1], D(0))/50
    volume = sum((b.volume for b in bars[-21:-1]), D(0))/20
    if min(atr, slow_atr, volume) <= 0:
        return result, {}
    direction = 1 if hours[-1].close > e > old_e else -1 if hours[-1].close < e < old_e else 0
    features = dict(atr_fraction=float(atr/current.close), volume_ratio=float(current.volume/volume),
                    compression_ratio=float(prior_atr/slow_atr), hourly_direction=direction,
                    ema_distance_atr=float((current.close-e)/atr),
                    return_4bars=float(current.close/bars[-5].close-1),
                    latest_close_ms=current.close_ms)
    baseline = strategy(symbol, bars)
    result['baseline_v1'] = dict(reason='SIGNAL' if baseline else 'NO_SIGNAL',
                                 signal=asdict(baseline) if baseline else None)
    liquid_vol = D('.002') <= atr/current.close <= D('.05')
    participation = current.volume >= volume*D('1.2')
    pullback = ((direction == 1 and previous.low <= local_e and previous.close >= local_e-atr
                 and current.close > previous.high and current.close > local_e)
                or (direction == -1 and previous.high >= local_e and previous.close <= local_e+atr
                    and current.close < previous.low and current.close < local_e))
    hi = max(b.high for b in bars[-21:-1]); lo = min(b.low for b in bars[-21:-1])
    breakout = (direction == 1 and current.close > hi) or (direction == -1 and current.close < lo)
    conditions = {'trend_pullback_v1': pullback,
                  'compression_breakout_v1': breakout and prior_atr/slow_atr <= D('.8')}
    for variant, trigger in conditions.items():
        reason = ('VOLATILITY_FILTER' if not liquid_vol else 'VOLUME_FILTER' if not participation
                  else 'NO_TREND' if not direction else 'NO_SETUP' if not trigger else 'SIGNAL')
        sig = None
        if reason == 'SIGNAL':
            distance = atr*D('1.5')
            stop, target = current.close-direction*distance, current.close+direction*2*distance
            if min(stop, target) <= 0:
                reason = 'INVALID_BARRIERS'
            else:
                sig = asdict(Signal(symbol, direction, current.close_ms, current.close, stop,
                                    target, abs(current.close-e)/atr, variant))
        result[variant] = dict(reason=reason, signal=sig)
    return result, features


class Recorder:
    def __init__(self, path):
        self.path = path
        self.error = None

    def observe(self, scan_id, row, bars, observed_ms):
        """Fail open for the existing scanner, fail visibly for research."""
        if self.error:
            return
        try:
            with closing(connect(self.path)) as db, db:
                eligible = row['market'] == 'USD-M' and row.get('quote') == 'USDT' and row.get('contract') == 'PERPETUAL'
                results, features = assess(row['symbol'], bars, observed_ms) if eligible else ({}, {})
                payload = dict(scanner=row, features=features, variants=results,
                               universe_eligible=eligible, ml_status='NOT_TRAINED')
                db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)',
                           (scan_id,row['market'],row['symbol'],observed_ms,encode(payload)))
                db.executemany('INSERT OR IGNORE INTO bars VALUES(?,?,?,?,?)',
                               [(row['market'],row['symbol'],b.close_ms,observed_ms,encode(asdict(b))) for b in bars])
                for variant, result in results.items():
                    sig = result['signal']
                    if not sig:
                        continue
                    identity = f"{variant}:{row['symbol']}:{sig['bar_close_ms']}:{sig['side']}"
                    # A repeat scan never moves a previously registered entry time.
                    db.execute('INSERT OR IGNORE INTO signals VALUES(?,?,?,?,?,?,?)',
                               (identity,variant,row['symbol'],sig['bar_close_ms'],observed_ms,
                                (observed_ms//MINUTE+1)*MINUTE,encode(dict(signal=sig,features=features,
                                  scanner=row,scan_id=scan_id,policy=FINGERPRINT))))
        except Exception as exc:
            self.error = type(exc).__name__

    def finish(self, data):
        if self.error:
            return
        try:
            with closing(connect(self.path)) as db, db:
                db.execute('INSERT OR IGNORE INTO scans VALUES(?,?,?)',
                           (data['started_ms'],data['finished_ms'],encode(data)))
        except Exception as exc:
            self.error = type(exc).__name__


def evaluate(path, fetch=public_bars, now_ms=None, limit=60, pause=0.6):
    """Resolve next-minute entries and four-hour outcomes, never historical signals.

    Fetches only public candles. Funding is a conservative reserve, not realized
    funding. A 24h observation delay expires a signal visibly, never makes up data.
    """
    if any(POLICY[k] != EXIT_POLICY[k] for k in ('fee_bps','slippage_bps','funding_reserve_bps')) or EXIT_POLICY['horizon_bars'] != POLICY['horizon_minutes']:
        raise ValueError('Exit simulator policy drift; register a new experiment')
    stamp = int(time.time()*1000) if now_ms is None else now_ms
    lock_path = Path(str(path)+'.outcomes.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status':'BUSY'}
        with closing(connect(path)) as db:
            health = db.execute('SELECT data FROM health WHERE id=1').fetchone()
            if health and json.loads(health[0]).get('cooldown_until_ms',0) > stamp:
                return {'status':'COOLDOWN'}
            # Fair rotation: an open trade cannot starve later trades.
            rows = db.execute('''SELECT s.id,s.symbol,s.entry_ms,s.data FROM signals s
              LEFT JOIN outcomes o ON s.id=o.id
              WHERE COALESCE(o.status,'') NOT IN ('CLOSED','EXPIRED','INVALID') AND s.entry_ms+60000<=?
              ORDER BY COALESCE(o.observed_ms,0),s.entry_ms LIMIT ?''', (stamp,limit)).fetchall()
            count = 0
            cache = {}
            for identity,symbol,start,encoded in rows:
                try:
                    if stamp-start > 86400000:
                        out = dict(status='EXPIRED',reason='DATA_NOT_RESOLVED_WITHIN_24H')
                    else:
                        end = min(start+POLICY['horizon_minutes']*MINUTE-1,stamp-1)
                        key = (symbol,start,end)
                        if key not in cache:
                            cache[key] = fetch(*key)
                        raw = cache[key]
                        bars = closed_bars(raw,stamp,MINUTE)
                        if any(b.open_ms < start or b.close_ms > end for b in bars):
                            raise ValueError('OUT_OF_RANGE_BARS')
                        if not bars or bars[0].open_ms != start:
                            out = dict(status='WAIT_DATA',reason='ENTRY_BAR_MISSING')
                        else:
                            sig = json.loads(encoded)['signal']
                            ref = float(sig['reference']); side = int(sig['side'])
                            # Normalize price risk at the actual modeled entry, not the old signal close.
                            distance = float(bars[0].open)*abs(ref-float(sig['stop']))/ref
                            if distance <= 0:
                                raise ValueError('INVALID_DISTANCE')
                            plan = dict(side='BUY' if side==1 else 'SELL',reference=ref,
                                        stop=sig['stop'],target=sig['target'],quantity=POLICY['risk_usdt']/distance)
                            usable = [r for r in raw if int(r[6]) < stamp]
                            out = simulate(plan,usable,'fixed_4h')
                            out.update(quantity=plan['quantity'],mode='SIGNAL_STUDY_NO_ORDERS',
                                       exchange_sizing=False,realized_funding=False)
                    with db:
                        db.execute('INSERT OR REPLACE INTO outcomes VALUES(?,?,?,?)',
                                   (identity,stamp,out['status'],encode(out)))
                    count += 1
                    if pause:
                        time.sleep(pause)
                except Exception as exc:
                    # Stop on network/rate/storage/data failure. Do not loop through the universe.
                    with db:
                        db.execute('INSERT OR REPLACE INTO health VALUES(1,?)',
                                   (encode(dict(status='ERROR',error_type=type(exc).__name__,
                                                observed_ms=stamp,cooldown_until_ms=stamp+30*MINUTE)),))
                    return dict(status='ERROR',error_type=type(exc).__name__,processed=count)
            with db:
                db.execute('INSERT OR REPLACE INTO health VALUES(1,?)',
                           (encode(dict(status='OK',observed_ms=stamp,processed=count)),))
            return dict(status='OK',processed=count)


def report(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        policy = json.loads(db.execute('SELECT data FROM policy WHERE id=1').fetchone()[0])
        rows = db.execute('SELECT s.variant,o.status,o.data FROM signals s LEFT JOIN outcomes o ON s.id=o.id').fetchall()
        scans = db.execute('SELECT COUNT(*),MAX(finished_ms) FROM scans').fetchone()
        observations = db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
        health = db.execute('SELECT data FROM health WHERE id=1').fetchone()
    results = []
    for variant in VARIANTS:
        selected = [(status,json.loads(data) if data else {}) for v,status,data in rows if v==variant]
        closed = [o for status,o in selected if status=='CLOSED']
        wins = sum(max(0,o['net']) for o in closed); losses = -sum(min(0,o['net']) for o in closed)
        results.append(dict(variant=variant,signals=len(selected),closed=len(closed),
                            unresolved=sum(status not in ('CLOSED','EXPIRED','INVALID') for status,_ in selected),
                            expired=sum(status=='EXPIRED' for status,_ in selected),
                            net_usdt=sum(o['net'] for o in closed),
                            mean_net_r=sum(o['net_r'] for o in closed)/len(closed) if closed else None,
                            profit_factor=wins/losses if losses else None))
    return dict(policy=policy,scan_count=scans[0],last_scan_ms=scans[1],observations=observations,
                health=json.loads(health[0]) if health else None,results=results,
                warning='Overlapping fixed-risk signal outcomes; NOT portfolio returns or evidence of 5% daily. '
                'No slot, margin, exchange lot-size or actual funding simulation. ML not trained. '
                'Baseline is core.strategy, not the complete adaptive Testnet execution policy.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('evaluate','report','export'))
    p.add_argument('--db', required=True)
    args = p.parse_args()
    if args.command == 'evaluate':
        result = evaluate(args.db)
    elif args.command == 'report':
        result = report(args.db)
    else:
        with closing(sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True)) as db:
            for identity,observed,data,outcome in db.execute('''SELECT s.id,s.observed_ms,s.data,o.data
              FROM signals s JOIN outcomes o ON s.id=o.id WHERE o.status='CLOSED' ORDER BY s.observed_ms'''):
                print(encode(dict(id=identity,observed_ms=observed,**json.loads(data),outcome=json.loads(outcome))))
        return
    print(encode(result))
    if result.get('status') == 'ERROR':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
