"""Broker-specific XAUUSD bid/ask replay. Offline only, no order transport."""
import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
import json
from pathlib import Path
from zoneinfo import ZoneInfo

D = Decimal


def timestamp(value):
    t=datetime.fromisoformat(value.replace('Z','+00:00'))
    if t.tzinfo is None: raise ValueError('Timezone-aware timestamps required')
    return t.astimezone(timezone.utc)


def config(path):
    c=json.loads(Path(path).read_text())
    if c.get('environment')!='demo' or c.get('account_currency')!='USD':
        raise ValueError('Only demo USD account research supported')
    if not c.get('broker') or not c.get('symbol'): raise ValueError('Broker and exact symbol required')
    for k in ('tick_size','tick_value_loss_per_lot','volume_min','volume_step','volume_max','risk_usd','initial_equity_usd'):
        c[k]=D(str(c[k]))
        if not c[k].is_finite() or c[k]<=0: raise ValueError('Invalid '+k)
    for k in ('commission_roundtrip_usd_per_lot','slippage_price_per_side','max_spread_price'):
        c[k]=D(str(c[k]))
        if not c[k].is_finite() or c[k]<0: raise ValueError('Invalid '+k)
    if c['volume_max']<c['volume_min']: raise ValueError('Volume limits')
    ZoneInfo(c['session_timezone'])
    if not 1<=int(c['session_start_hour'])<=21: raise ValueError('Session hour must be 1..21')
    return c


def load(path):
    rows=[]
    with open(path,newline='') as f:
        for r in csv.DictReader(f):
            b={'time':timestamp(r['time'])}
            for side in ('bid','ask'):
                for field in ('open','high','low','close'):
                    key=side+'_'+field;b[key]=D(r[key])
                    if not b[key].is_finite() or b[key]<=0:raise ValueError('Invalid price')
                if not b[side+'_low']<=min(b[side+'_open'],b[side+'_close'])<=max(b[side+'_open'],b[side+'_close'])<=b[side+'_high']:
                    raise ValueError('Invalid OHLC')
            if any(b['ask_'+k]<b['bid_'+k] for k in ('open','high','low','close')):raise ValueError('Crossed quote')
            if b['time'].second or b['time'].microsecond or b['time'].minute%5:raise ValueError('Need 5-minute bar opens')
            if rows and b['time']<=rows[-1]['time']:raise ValueError('Unsorted/duplicate times')
            rows.append(b)
    return rows


def size(c, distance):
    cost=(distance+2*c['slippage_price_per_side'])/c['tick_size']*c['tick_value_loss_per_lot']+c['commission_roundtrip_usd_per_lot']
    if cost<=0:raise ValueError('Invalid risk')
    lots=min(c['volume_max'],(c['risk_usd']/cost/c['volume_step']).to_integral_value(rounding=ROUND_FLOOR)*c['volume_step'])
    if lots<c['volume_min']:return D(0)
    return lots


def ema(values,period):
    e=sum(values[:period])/period
    for v in values[period:]:e+=D(2)/(period+1)*(v-e)
    return e


def signal(history,variant,c):
    if len(history)<60:return None
    h=history[-60:]
    if any(b['time']-a['time']!=timedelta(minutes=5) for a,b in zip(h,h[1:])):return None
    closes=[b['bid_close'] for b in h]
    fast=ema(closes,20);slow=ema(closes,50)
    direction=1 if closes[-1]>fast>slow else -1 if closes[-1]<fast<slow else 0
    atr=sum(max(b['bid_high']-b['bid_low'],abs(b['bid_high']-a['bid_close']),abs(b['bid_low']-a['bid_close'])) for a,b in zip(h[-15:-1],h[-14:]))/14
    if atr<=0:return None
    last,prev=h[-1],h[-2]
    if variant=='trend_pullback':
        prior=ema(closes[:-1],20)
        valid=(direction==1 and prev['bid_low']<=prior and last['bid_close']>prev['bid_high']) or (direction==-1 and prev['bid_high']>=prior and last['bid_close']<prev['bid_low'])
        return (direction,atr*D('1.5')) if valid else None
    local=last['time'].astimezone(ZoneInfo(c['session_timezone']))
    hour=int(c['session_start_hour'])
    if local.hour!=hour:return None
    prior=[b for b in h if b['time'].astimezone(ZoneInfo(c['session_timezone'])).date()==local.date() and b['time'].astimezone(ZoneInfo(c['session_timezone'])).hour==hour-1]
    if len(prior)!=12:return None
    hi=max(b['bid_high'] for b in prior);lo=min(b['bid_low'] for b in prior)
    side=1 if prev['bid_close']<=hi<last['bid_close'] else -1 if prev['bid_close']>=lo>last['bid_close'] else 0
    return (side,atr*D('1.5')) if side else None


def replay(bars,c,variant):
    trades=[];skips={};equity=c['initial_equity_usd'];i=60
    while i+12<=len(bars):
        sig=signal(bars[:i],variant,c)
        if not sig:i+=1;continue
        side,distance=sig;window=bars[i:i+12]
        # Only intraday, complete one-hour windows. No rollover swap model yet.
        zone=ZoneInfo(c['session_timezone'])
        start=window[0]['time'].astimezone(zone)
        end=(window[-1]['time']+timedelta(minutes=5)).astimezone(zone)
        hour=int(c['session_start_hour'])
        valid=(window[0]['time']-bars[i-1]['time']==timedelta(minutes=5) and all(b['time']-a['time']==timedelta(minutes=5) for a,b in zip(window,window[1:])) and start.date()==end.date() and hour<=start.hour and end.hour<hour+3)
        if not valid:i+=1;continue
        spread=window[0]['ask_open']-window[0]['bid_open']
        lots=size(c,distance)
        if spread>c['max_spread_price'] or not lots or c['risk_usd']>equity:
            key='spread_or_size_or_equity';skips[key]=skips.get(key,0)+1;i+=1;continue
        entry=window[0]['ask_open'] if side==1 else window[0]['bid_open']
        stop=entry-side*distance;target=entry+side*2*distance
        exit_price=None;reason='TIME';held=12
        quote='bid' if side==1 else 'ask'
        for j,b in enumerate(window):
            hit_stop=b[quote+'_low']<=stop if side==1 else b[quote+'_high']>=stop
            hit_target=b[quote+'_high']>=target if side==1 else b[quote+'_low']<=target
            if hit_stop:
                exit_price=min(b[quote+'_open'],stop) if side==1 else max(b[quote+'_open'],stop);reason='STOP'
            elif hit_target:exit_price=target;reason='TARGET'
            if exit_price is not None:held=j+1;break
        if exit_price is None:exit_price=window[-1][quote+'_close']
        gross=(side*(exit_price-entry)-2*c['slippage_price_per_side'])/c['tick_size']*c['tick_value_loss_per_lot']*lots
        costs=c['commission_roundtrip_usd_per_lot']*lots
        profit=gross-costs;equity+=profit
        trades.append(dict(time=window[0]['time'].isoformat(),side=side,lots=str(lots),net_usd=str(profit),reason=reason))
        i+=held
    return dict(strategy=variant,trades=trades,skips=skips,final_equity_usd=str(equity),net_usd=str(equity-c['initial_equity_usd']))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True);p.add_argument('--bars',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();out=Path(a.output).resolve()
    if out in (Path(a.config).resolve(),Path(a.bars).resolve()):p.error('Separate output required')
    c=config(a.config);bars=load(a.bars)
    result=dict(execution_enabled=False,mode='OFFLINE_DEMO_RESEARCH',broker=c['broker'],symbol=c['symbol'],results=[replay(bars,c,v) for v in ('trend_pullback','session_breakout')],limitations=['Not MT5 connected; no order submission','Broker bid/ask data and verified contract specification required','Independent strategy portfolios; no margin or stop-distance restriction simulation','Intraday only: selected session must avoid broker rollover and market breaks','Constant USD tick value; verify applicable contract','Exploratory replay; no evidence of profitability'])
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))

if __name__=='__main__':main()
