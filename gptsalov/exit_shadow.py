"""Paired exit research; unsigned public GET only, read-only pilot journals."""
import argparse
from contextlib import closing
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .market import NoRedirect

MINUTE = 60000
POLICY = dict(version=1, horizon_bars=240, trail_activation_r=1,
              trail_distance_r=1, fee_bps=5, slippage_bps=3,
              funding_reserve_bps=15, entry='next_full_minute_open',
              source='production_1m', variants=['fixed_4h', 'trail_1r_4h'])


def public_bars(symbol, start, end):
    if not re.fullmatch('[A-Z0-9]{2,30}', symbol):
        raise ValueError('Invalid symbol')
    params=dict(symbol=symbol, interval='1m', startTime=start,
                endTime=end, limit=1000)
    url='https://fapi.binance.com/fapi/v1/klines?'+urlencode(params)
    with build_opener(NoRedirect()).open(Request(url, method='GET'), timeout=15) as r:
        raw=r.read(1000001)
    if len(raw)>1000000: raise ValueError('Oversized response')
    rows=json.loads(raw)
    if not isinstance(rows,list): raise ValueError('Invalid candles')
    return rows


def simulate(plan, bars, variant):
    """Closed contiguous 1m bars only. New trailing stops apply NEXT bar.

    Both variants use identical modeled entry, size and fixed target. Stop wins
    when both barriers occur in a candle. Exit candle excursion is interval
    censored: lower bound uses previously observed bars + executable exit;
    upper bound includes the whole exit candle (possibly after exit).
    """
    if variant not in POLICY['variants']: raise ValueError('Unknown variant')
    if not bars: return {'status':'WAIT_DATA'}
    side=1 if plan['side']=='BUY' else -1
    qty=float(plan['quantity']);ref=float(plan['reference'])
    raw_entry=float(bars[0][1]);slip=POLICY['slippage_bps']/10000
    entry=raw_entry*(1+side*slip)
    distance=raw_entry*side*(ref-float(plan['stop']))/ref
    reward=raw_entry*side*(float(plan['target'])-ref)/ref
    if min(qty,entry,distance,reward)<=0: raise ValueError('Invalid plan')
    stop=entry-side*distance;target=entry+side*reward
    if min(stop,target)<=0: raise ValueError('Invalid barriers')
    favorable=adverse=0.0;best=entry;trailing=False
    last=None;ambiguous=False;reason=None
    for i,b in enumerate(bars[:POLICY['horizon_bars']]):
        o,h,l,c=map(float,b[1:5])
        if not 0<l<=min(o,c)<=max(o,c)<=h: raise ValueError('Invalid OHLC')
        stop_hit=l<=stop if side==1 else h>=stop
        target_hit=h>=target if side==1 else l<=target
        ambiguous=stop_hit and target_hit
        upper_fav=max(favorable, max(0,side*((h if side==1 else l)-entry)))
        upper_adv=max(adverse, max(0,-side*((l if side==1 else h)-entry)))
        if stop_hit:
            raw_exit=min(o,stop) if side==1 else max(o,stop)
            reason='TRAIL_STOP' if trailing else 'STOP'
        elif target_hit:
            raw_exit=target;reason='TARGET'
        elif i+1==POLICY['horizon_bars']:
            raw_exit=c;reason='TIME'
        else:
            favorable,adverse=upper_fav,upper_adv
            if variant=='trail_1r_4h':
                best=max(best,h) if side==1 else min(best,l)
                if side*(best-entry)>=distance:
                    new=best-side*distance
                    stop=max(stop,new) if side==1 else min(stop,new)
                    trailing=True
            last=c
            continue
        exit_price=raw_exit*(1-side*slip)
        favorable=max(favorable,max(0,side*(raw_exit-entry)))
        adverse=max(adverse,max(0,-side*(raw_exit-entry)))
        if reason=='TIME': favorable,adverse=upper_fav,upper_adv
        gross=side*(exit_price-entry)*qty
        fees=(entry+exit_price)*qty*POLICY['fee_bps']/10000
        reserve=entry*qty*POLICY['funding_reserve_bps']/10000
        return dict(status='CLOSED',reason=reason,entry=entry,exit=exit_price,
                    entry_ms=int(bars[0][0]),exit_bar_ms=int(b[0]),
                    held_minutes=i+1,gross=gross,fees=fees,funding_reserve=reserve,
                    net=gross-fees-reserve,net_r=(gross-fees-reserve)/(qty*distance),
                    mfe_lower_usdt=favorable*qty,mfe_upper_usdt=upper_fav*qty,
                    mae_lower_usdt=adverse*qty,mae_upper_usdt=upper_adv*qty,
                    ambiguous_exit_bar=ambiguous,initial_risk_usdt=qty*distance)
    return dict(status='OPEN',entry=entry,mark=last,
                processed_bars=min(len(bars),POLICY['horizon_bars']),
                mfe_lower_usdt=favorable*qty,mae_lower_usdt=adverse*qty)


def read_plans(source):
    """Accepted entry plans, not claims of actual Testnet fills."""
    for p in sorted(source.glob('trade-*.db')):
        with closing(sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            row=db.execute('SELECT data FROM experiment WHERE id=1').fetchone()
            actions={k:json.loads(v) for k,v in db.execute('SELECT name,data FROM actions')}
        if not row or actions.get('entry',{}).get('phase')!='ACK': continue
        plan=json.loads(row[0])
        if plan.get('environment')!='testnet': raise ValueError('Source must be Testnet')
        response=actions['entry'].get('response',{})
        if response.get('status') in ('REJECTED','EXPIRED','CANCELED') and not float(response.get('executedQty',0)):
            continue
        stamp=int(actions['prepared']['at_ms'])
        clean={k:plan[k] for k in ('symbol','side','quantity','reference','stop','target')}
        clean.update(signal_id=plan.get('signal_id'),accepted_plan_ms=stamp)
        yield p.name,clean


def run_cycle(source, target, fetch=public_bars, now=None):
    now=int(time.time()*1000) if now is None else now
    source=Path(source).resolve();target=Path(target).resolve()
    if source==target.parent or source in target.parents:
        raise ValueError('Research database must be outside pilot directory')
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with closing(sqlite3.connect(target)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS meta(id INTEGER PRIMARY KEY,data TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY,data TEXT)')
            row=db.execute('SELECT data FROM meta WHERE id=1').fetchone()
            meta=json.loads(row[0]) if row else dict(policy=POLICY,source=str(source),started_ms=now)
            if meta['policy']!=POLICY or meta['source']!=str(source):
                raise ValueError('Research identity mismatch')
            for key,plan in read_plans(source):
                prior=db.execute('SELECT data FROM samples WHERE id=?',(key,)).fetchone()
                old=json.loads(prior[0]) if prior else None
                if old and old['plan']!=plan: raise ValueError('Source plan changed')
                if old and old.get('complete'): continue
                start=(plan['accepted_plan_ms']//MINUTE+1)*MINUTE
                record=old or dict(plan=plan,cohort='historical' if plan['accepted_plan_ms']<=meta['started_ms'] else 'forward')
                record.update(error=None,observed_ms=now)
                end=min(start+POLICY['horizon_bars']*MINUTE,now//MINUTE*MINUTE)
                try:
                    if end<=start:
                        record.update(results={},complete=False)
                    else:
                        rows=fetch(plan['symbol'],start,end-1)
                        time.sleep(0.15)
                        bars=[b for b in rows if start<=int(b[0])<end and int(b[6])<now]
                        expected=list(range(start,end,MINUTE))
                        if [int(b[0]) for b in bars]!=expected:
                            raise ValueError('Missing or non-contiguous closed candles')
                        record['results']={v:simulate(plan,bars,v) for v in POLICY['variants']}
                        record['complete']=all(x['status']=='CLOSED' for x in record['results'].values())
                        record['candle_sha256']=hashlib.sha256(json.dumps(bars).encode()).hexdigest()
                        record['bars']=bars
                except Exception as exc:
                    record.update(error=type(exc).__name__+': '+str(exc)[:150],complete=False)
                with db:db.execute('INSERT OR REPLACE INTO samples VALUES(?,?)',(key,json.dumps(record)))
            meta['last_check_ms']=now
            with db:db.execute('INSERT OR REPLACE INTO meta VALUES(1,?)',(json.dumps(meta),))


def report(target):
    with closing(sqlite3.connect(Path(target).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        meta=json.loads(db.execute('SELECT data FROM meta').fetchone()[0])
        rows=[dict(id=k,**json.loads(v)) for k,v in db.execute('SELECT id,data FROM samples')]
    result=dict(meta=meta,total=len(rows),errors=[dict(id=r['id'],error=r['error']) for r in rows if r.get('error')],groups={})
    for cohort in ('historical','forward'):
        paired=[r for r in rows if r['cohort']==cohort and r.get('complete') and not r.get('error')]
        summary={'paired':len(paired),'variants':{}}
        for v in POLICY['variants']:
            xs=[r['results'][v] for r in paired];nets=[x['net'] for x in xs]
            wins=sum(max(0,n) for n in nets);losses=-sum(min(0,n) for n in nets)
            summary['variants'][v]=dict(net=sum(nets),wins=sum(n>0 for n in nets),
                profit_factor=wins/losses if losses else None,
                reasons={k:sum(x['reason']==k for x in xs) for k in ('STOP','TRAIL_STOP','TARGET','TIME')})
        summary['paired_delta_net']=sum(r['results']['trail_1r_4h']['net']-r['results']['fixed_4h']['net'] for r in paired)
        result['groups'][cohort]=summary
    result['samples']=[{k:v for k,v in r.items() if k!='bars'} for r in rows]
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('run','status'))
    parser.add_argument('--source')
    parser.add_argument('--db',required=True)
    args=parser.parse_args()
    if args.command=='run':
        if not args.source: parser.error('--source required')
        run_cycle(args.source,args.db)
    print(json.dumps(report(args.db)))


if __name__=='__main__': main()
