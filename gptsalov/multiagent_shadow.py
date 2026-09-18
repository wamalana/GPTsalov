"""Rule-based agent foundation; observation only, no credentials or orders."""
from dataclasses import asdict
from contextlib import closing
from pathlib import Path
import argparse
import json
import sqlite3
import time

from .core import BAR_MS, Config, dec, encode, hourly, closed_bars, strategy
from .market import BinancePublic

VERSION = 'shadow-rules-v1'


def read_pilot(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
        row = db.execute('SELECT data FROM state WHERE id=1').fetchone()
    state = json.loads(row[0])
    return {k: state.get(k) for k in ('lock', 'active', 'error', 'last_check_ms', 'phase')}


def analyze(bars, state, now, symbol='ETHUSDT', scan_mode=False):
    """All rules inspect the same closed candles. Votes are not probabilities."""
    cfg = Config()
    votes = []
    def vote(agent, verdict, reason, **evidence):
        votes.append(dict(agent=agent, verdict=verdict, reason=reason, evidence=evidence))
    expected = now//BAR_MS*BAR_MS-1
    valid = (len(bars) >= 100 and bars[-1].close_ms == expected
             and 0 <= now-expected <= (BAR_MS+120000 if scan_mode else cfg.max_data_age_ms)
             and all(b.open_ms % BAR_MS == 0 and b.close_ms == b.open_ms+BAR_MS-1
                     for b in bars)
             and all(b.open_ms-a.open_ms == BAR_MS for a,b in zip(bars,bars[1:])))
    vote('data', 'PASS' if valid else 'VETO', 'CLOSED_CONTIGUOUS_FRESH' if valid else 'INVALID_OR_STALE_CANDLES')
    heartbeat = state.get('last_check_ms')
    healthy = (type(heartbeat) is int and 0 <= now-heartbeat <= 120000
               and state.get('phase') == 'WAIT_SIGNAL'
               and not any(state.get(k) for k in ('lock', 'active', 'error')))
    vote('risk', 'PASS' if healthy else 'VETO', 'PILOT_IDLE_HEALTHY' if healthy else 'PILOT_UNAVAILABLE_OR_BLOCKED',
         lock=state.get('lock'), phase=state.get('phase'))
    signal = None
    if valid:
        hours = hourly(bars)
        ema = sum((b.close for b in hours[:20]), dec(0))/20
        prev = ema
        for bar in hours[20:]:
            prev = ema
            ema += dec(2)/21*(bar.close-ema)
        trend = 1 if hours[-1].close > ema > prev else -1 if hours[-1].close < ema < prev else 0
        vote('trend', 'LONG' if trend == 1 else 'SHORT' if trend == -1 else 'ABSTAIN', 'HOURLY_EMA20', ema=str(ema))
        current = bars[-1]
        prior = bars[-21:-1]
        avg = sum((b.volume for b in prior), dec(0))/20
        momentum = 1 if current.close > max(b.high for b in prior) else -1 if current.close < min(b.low for b in prior) else 0
        if avg <= 0 or current.volume < avg*dec('1.2'):
            momentum = 0
        vote('momentum', 'LONG' if momentum == 1 else 'SHORT' if momentum == -1 else 'ABSTAIN', 'DONCHIAN20_VOLUME')
        atr = sum((max(bars[i].high-bars[i].low, abs(bars[i].high-bars[i-1].close), abs(bars[i].low-bars[i-1].close))
                   for i in range(len(bars)-14,len(bars))), dec(0))/14
        ratio = atr/current.close
        vote('volatility', 'PASS' if dec('.002') <= ratio <= dec('.05') else 'VETO', 'ATR14_RANGE', atr_ratio=str(ratio))
        signal = strategy(symbol, bars)
    else:
        for agent in ('trend','momentum','volatility'):
            vote(agent, 'ABSTAIN', 'INVALID_INPUT')
    vote('news', 'ABSTAIN', 'NOT_CONNECTED')
    blocked = any(v['verdict'] == 'VETO' for v in votes)
    return dict(version=VERSION, mode='shadow', observed_ms=now,
                candle_close_ms=bars[-1].close_ms if bars else None,
                baseline_signal=asdict(signal) if signal else None, votes=votes,
                decision='BLOCKED' if blocked else 'RESEARCH_CANDIDATE' if signal else 'WAIT',
                execution_enabled=False, llm_enabled=False,
                limitations=['news_not_connected','no_execution_sizing','not_a_profitability_evaluation'])


def record(path, result):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS observations(version TEXT, candle INTEGER, data TEXT, PRIMARY KEY(version,candle))')
        db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?)',
                   (VERSION, result['candle_close_ms'], encode(result)))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pilot-db', required=True)
    p.add_argument('--output-db', required=True)
    p.add_argument('--cycles', type=int, default=1, help='0 means continuous')
    p.add_argument('--ai-config', help='Optional owned 0600 OpenAI config; shadow review only')
    args = p.parse_args()
    if args.cycles < 0 or Path(args.pilot_db).resolve() == Path(args.output_db).resolve():
        p.error('Invalid cycles or output aliases pilot database')
    if Path(args.output_db).exists() and Path(args.pilot_db).samefile(args.output_db):
        p.error('Output aliases pilot database')
    client = BinancePublic()
    count = 0
    while args.cycles == 0 or count < args.cycles:
        # Align to closed-bar observation window; do not poll the market every minute.
        now = int(time.time()*1000)
        if args.cycles == 0 and now % BAR_MS > 90000:
            time.sleep(min(30, (BAR_MS-now % BAR_MS)/1000+3))
            continue
        server = int(client.get('/fapi/v1/time')['serverTime'])
        if abs(server-now) > 5000:
            raise ValueError('Clock skew')
        bars = closed_bars(client.get('/fapi/v1/klines',symbol='ETHUSDT',interval='15m',limit=200), server)
        result = analyze(bars, read_pilot(args.pilot_db), int(time.time()*1000))
        if args.ai_config:
            from .ai_review import review
            result['ai_review']=review(result,args.ai_config,str(Path(args.output_db).with_suffix('.ai.db')))
            result['llm_enabled']=True
        record(args.output_db, result)
        print(encode(result), flush=True)
        count += 1
        if args.cycles == 0 or count < args.cycles:
            time.sleep(120)


if __name__ == '__main__':
    main()
