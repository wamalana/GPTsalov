"""Exploratory paired signal experiments. No orders; public candle GET only."""
import argparse
from collections import defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from .exit_shadow import public_bars, simulate, MINUTE


def summary(xs):
    wins=sum(max(0,x['net']) for x in xs)
    losses=-sum(min(0,x['net']) for x in xs)
    return dict(count=len(xs), **{k:sum(x.get(k,0) for x in xs) for k in
                ('gross','fees','funding_reserve','net')},
                profit_factor=wins/losses if losses else None,
                losers=sum(x['net']<0 for x in xs),
                losers_reached_1r=sum(x['net']<0 and x.get('mfe_lower_usdt',0)>=x.get('initial_risk_usdt',.25) for x in xs))


def paired(signal, bars):
    """Retest reference within 15 closed minutes, enter NEXT minute; fixed risk.

    Bar touching the reference must close back on the signal side. This is a
    reference-price retest proxy, not a retest of the original Donchian level.
    All variants end at the same original 4h deadline; no future same-bar entry.
    """
    ref=float(signal['reference']); side=int(signal['side'])
    def run(offset, variant):
        raw=bars[offset:]
        distance=float(raw[0][1])*abs(ref-float(signal['stop']))/ref
        plan=dict(side='BUY' if side==1 else 'SELL', reference=ref,
                  stop=signal['stop'],target=signal['target'],quantity=.25/distance)
        result=simulate(plan,raw,variant)
        if result['status']=='OPEN':
            entry=result['entry']; price=float(raw[-1][4])*(1-side*.0003)
            gross=side*(price-entry)*plan['quantity']
            fees=(entry+price)*plan['quantity']*.0005
            reserve=entry*plan['quantity']*.0015
            result.update(status='CLOSED',reason='ORIGINAL_DEADLINE',
                          gross=gross,fees=fees,funding_reserve=reserve,
                          net=gross-fees-reserve,initial_risk_usdt=.25)
        return result
    result={'fixed':run(0,'fixed_4h'),'trailing':run(0,'trail_1r_4h')}
    offset=None
    for i,b in enumerate(bars[:15]):
        if float(b[3])<=ref<=float(b[2]) and side*(float(b[4])-ref)>0:
            offset=i+1;break
    result['retest']=dict(status='NO_ENTRY') if offset is None else run(offset,'fixed_4h')
    result['retest_entry_offset']=offset
    return result


def audit(path, output, count=60):
    stamp=int(time.time()*1000)
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        rows=db.execute('''SELECT s.id,s.variant,s.symbol,s.entry_ms,s.data,o.data
          FROM signals s JOIN outcomes o ON s.id=o.id WHERE o.status='CLOSED'
          ORDER BY s.entry_ms,s.id''').fetchall()
    groups=defaultdict(list); candidates=[]
    for identity,variant,symbol,start,d,o in rows:
        d=json.loads(d);o=json.loads(o);f=d['features'];sig=d['signal']
        atr=float(f['atr_fraction']);volume=float(f['volume_ratio'])
        tags=[variant,variant+':side='+str(sig['side']),
              variant+':atr='+('low' if atr<.005 else 'mid' if atr<.015 else 'high'),
              variant+':volume='+('high' if volume>=3 else 'normal')]
        # Fixed diagnostic screens: not tuned or promoted from this audit.
        distance=abs(float(sig['reference'])-float(sig['stop']))/float(sig['reference'])
        if .0031/distance <= .25:
            tags.append(variant+':cost_at_most_0.25R')
        if abs(float(f['ema_distance_atr']))<=2:
            tags.append(variant+':ema_distance_at_most_2ATR')
        for tag in tags:groups[tag].append(o)
        if variant=='baseline_v1' and start+240*MINUTE<stamp:
            candidates.append((identity,symbol,start,sig))
    records=[]
    for identity,symbol,start,sig in candidates[-count:]:
        try:
            bars=public_bars(symbol,start,start+240*MINUTE-1)
            if len(bars)!=240 or any(int(b[0])!=start+i*MINUTE or int(b[6])!=start+(i+1)*MINUTE-1 for i,b in enumerate(bars)):
                raise ValueError('Non-contiguous candles')
            result=paired(sig,bars)
            records.append(dict(id=identity,result=result,candle_sha256=hashlib.sha256(json.dumps(bars).encode()).hexdigest()))
            time.sleep(.2)
        except Exception as exc:
            records.append(dict(id=identity,error=type(exc).__name__))
            break
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    result=dict(generated_ms=stamp,execution_enabled=False,mode='EXPLORATORY_NOT_HOLDOUT',
                groups={k:summary(v) for k,v in groups.items()},paired_records=records,
                warning='Overlapping simulated signals, not portfolio returns. Funding reserve is not realized funding. Retest unresolved outcomes must not be excluded when deciding profitability.')
    result['paired_summary']={}
    valid=[r['result'] for r in records if 'result' in r]
    for name in ('fixed','trailing','retest'):
        xs=[r[name] for r in valid if r[name]['status']=='CLOSED']
        result['paired_summary'][name]=dict(summary(xs),
            unresolved=sum(r[name]['status']=='UNRESOLVED_AT_DEADLINE' for r in valid),
            no_entry=sum(r[name]['status']=='NO_ENTRY' for r in valid))
    paired_closed=[r for r in valid if r['retest']['status']=='CLOSED']
    result['retest_matched_fixed']=summary([r['fixed'] for r in paired_closed])
    output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='paired_records'}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',required=True);p.add_argument('--output',required=True)
    p.add_argument('--count',type=int,default=60)
    a=p.parse_args()
    if Path(a.db).resolve()==Path(a.output).resolve():p.error('Separate output required')
    if not 1<=a.count<=200:p.error('count must be 1..200')
    audit(a.db,a.output,a.count)
