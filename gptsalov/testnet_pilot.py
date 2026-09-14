"""Bounded ETHUSDT Testnet strategy pilot. Fixed demo host, no production orders."""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import argparse
import fcntl
import json
import os
import sqlite3
import time

from .core import Config, Rules, dec, encode, size, closed_bars, strategy, BAR_MS
from .market import BinancePublic
from .research import reward_risk
from .testnet import DemoClient, Coordinator, Journal, Rejected, Uncertain, ORDER, ALGO, TERMINAL
from .testnet_smoke import setup

SYMBOL='ETHUSDT'
CFG=Config(initial_equity=dec(50),max_notional_fraction=dec('.5'))
POLICY={'schema':1,'environment':'testnet','symbol':SYMBOL,'budget':'50',
        'risk_cap':'0.25','notional_cap':'25','min_rr':'1','max_trades':3,
        'duration_ms':86400000,'hold_ms':14400000,'config_hash':CFG.fingerprint}

def now_ms(): return int(time.time()*1000)
def day(t): return datetime.fromtimestamp(t/1000,ZoneInfo('Asia/Bangkok')).date().isoformat()

class Book:
    def __init__(self,root,identity):
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.lock=(self.root/'writer.lock').open('a')
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.db=sqlite3.connect(self.root/'pilot.db')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,data TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,time_ms INTEGER,data TEXT)')
        row=self.db.execute('SELECT data FROM state WHERE id=1').fetchone()
        if row:
            self.s=json.loads(row[0])
            if self.s['identity']!=identity or self.s['policy']!=POLICY:
                self.close(); raise ValueError('Pilot identity/policy mismatch')
        else:
            t=now_ms()
            self.s=dict(identity=identity,policy=POLICY,created_ms=t,active=None,
                seen_ms=None,closed_trades=0,loss_streak=0,balance='50',equity='50',
                high_water='50',day=day(t),day_start='50',lock=None,error=None,
                last_check_ms=None,last_signal_ms=None,phase='STARTING')
            self.save('CREATED')
    def save(self,event=None):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES(1,?)',(encode(self.s),))
            if event is not None:
                self.db.execute('INSERT INTO events(time_ms,data) VALUES(?,?)',(now_ms(),encode(event)))
    def close(self):
        self.db.close();self.lock.close()

def position(api):
    rows=api.call('GET','/fapi/v3/positionRisk',symbol=SYMBOL)
    rows=[x for x in rows if x['symbol']==SYMBOL and dec(x['positionAmt'])!=0]
    if len(rows)>1: raise ValueError('Unexpected hedge/multiple positions')
    return rows[0] if rows else None

def owned_exit_orders(c):
    """Require exchange order evidence, not a briefly empty position response."""
    result={}
    if c.j.get('exit'):
        try:
            o=c.api.call('GET',ORDER,symbol=SYMBOL,origClientOrderId=c.cid('exit'))
            result[str(o['orderId'])]=o
        except Rejected:
            pass
    for name in ('stop','target'):
        if c.j.get(name) is None: continue
        try:
            a=c.api.call('GET',ALGO,clientAlgoId=c.cid(name))
        except Rejected:
            continue
        oid=a.get('actualOrderId')
        if oid and str(oid)!='0':
            o=c.api.call('GET',ORDER,symbol=SYMBOL,orderId=oid)
            result[str(o['orderId'])]=o
    return list(result.values())

def close_evidence(c,entry):
    orders=owned_exit_orders(c)
    opposite='SELL' if c.plan()['side']=='BUY' else 'BUY'
    if any(o.get('symbol')!=SYMBOL or o.get('side')!=opposite for o in orders):
        raise ValueError('Closing order identity mismatch')
    quantity=sum((dec(x['executedQty']) for x in orders),dec(0))
    if quantity!=dec(entry['executedQty']):
        return None
    if not all(o['status'] in TERMINAL for o in orders):
        return None
    return orders

def cancel_owned(c):
    orders=c.api.call('GET','/fapi/v1/openAlgoOrders',symbol=SYMBOL)
    owned={c.cid('stop'),c.cid('target')}
    if any(o.get('clientAlgoId') not in owned for o in orders):
        raise ValueError('Unowned conditional order')
    for o in orders:
        c.api.call('DELETE',ALGO,clientAlgoId=o['clientAlgoId'])
    return not c.api.call('GET','/fapi/v1/openAlgoOrders',symbol=SYMBOL)

def exit_once(c,entry,pos):
    if entry['status'] not in TERMINAL:
        c.api.call('DELETE',ORDER,symbol=SYMBOL,origClientOrderId=c.cid('entry'))
        return 'CANCEL_ENTRY_BEFORE_EXIT'
    amount=dec(pos['positionAmt'])
    sign=1 if c.plan()['side']=='BUY' else -1
    if amount*sign<=0 or abs(amount)>dec(entry['executedQty']):
        raise ValueError('Position ownership mismatch')
    c.once('exit',ORDER,dict(symbol=SYMBOL,side='SELL' if sign==1 else 'BUY',
        positionSide='BOTH',type='MARKET',quantity=str(abs(amount)),
        reduceOnly='true',newClientOrderId=c.cid('exit')))
    return 'EXIT_RECONCILIATION'

def reconcile(c,force_exit=False):
    """Only first entry POST uses preflight; restart never retries that POST."""
    p=c.plan()
    if c.j.get('entry') is None:
        c.preflight(p)
        c.once('entry',ORDER,dict(symbol=SYMBOL,side=p['side'],positionSide='BOTH',
            type='MARKET',quantity=p['quantity'],newClientOrderId=c.cid('entry')))
    try:
        entry=c.api.call('GET',ORDER,symbol=SYMBOL,origClientOrderId=c.cid('entry'))
    except Rejected:
        return 'UNKNOWN_ENTRY',None
    if entry.get('symbol')!=SYMBOL or entry.get('side')!=p['side']:
        raise ValueError('Entry identity mismatch')
    filled=dec(entry['executedQty'])
    if filled==0:
        return ('NO_FILL' if entry['status'] in TERMINAL else 'WAIT_ENTRY'),None
    if filled<0 or filled>dec(p['quantity']):
        raise ValueError('Invalid entry fill')
    sign=1 if p['side']=='BUY' else -1
    opposite='SELL' if sign==1 else 'BUY'
    c.once('stop',ALGO,dict(symbol=SYMBOL,side=opposite,positionSide='BOTH',
        algoType='CONDITIONAL',type='STOP_MARKET',triggerPrice=p['stop'],
        closePosition='true',workingType='CONTRACT_PRICE',clientAlgoId=c.cid('stop')))
    if entry['status'] not in TERMINAL:
        c.api.call('DELETE',ORDER,symbol=SYMBOL,origClientOrderId=c.cid('entry'))
        return 'CANCEL_PARTIAL_ENTRY',None
    pos=position(c.api)
    if pos is None:
        exits=close_evidence(c,entry)
        if exits is None:
            return 'WAIT_POSITION_OR_CLOSE_EVIDENCE',None
        if not cancel_owned(c): return 'CLEANUP_PENDING',None
        if position(c.api) is not None: return 'WAIT_POSITION_SYNC',None
        if c.api.call('GET','/fapi/v1/openOrders'):
            raise ValueError('Unexpected open regular order')
        return 'CLOSED',(entry,exits)
    amount=dec(pos['positionAmt'])
    if amount*sign<=0 or abs(amount)>filled:
        raise ValueError('Position ownership mismatch')
    avg=dec(entry['avgPrice']);stop=dec(p['stop'])
    risk=abs(amount)*(abs(avg-stop)+(avg+stop)*dec('.0008')+avg*dec('.001'))
    if risk>dec('.25') or abs(avg/dec(p['reference'])-1)>dec('.005') or sign*(avg-stop)<=0:
        force_exit=True
    if force_exit or c.j.get('exit'):
        return exit_once(c,entry,pos),None
    try:
        a=c.api.call('GET',ALGO,clientAlgoId=c.cid('stop'))
        protected=(a['algoStatus']=='NEW' and a['symbol']==SYMBOL and a['side']==opposite
            and a.get('closePosition') in (True,'true') and dec(a['triggerPrice'])==stop
            and a.get('orderType',a.get('type'))=='STOP_MARKET')
    except Rejected:
        protected=False
    if not protected:
        return exit_once(c,entry,pos),None
    c.once('target',ALGO,dict(symbol=SYMBOL,side=opposite,positionSide='BOTH',
        algoType='CONDITIONAL',type='TAKE_PROFIT_MARKET',triggerPrice=p['target'],
        closePosition='true',workingType='CONTRACT_PRICE',clientAlgoId=c.cid('target')))
    # A target that cannot be confirmed cannot cause a second POST.
    try:
        t=c.api.call('GET',ALGO,clientAlgoId=c.cid('target'))
        target_ok=t['algoStatus']=='NEW' and t['symbol']==SYMBOL and t['side']==opposite and t.get('closePosition') in (True,'true') and dec(t['triggerPrice'])==dec(p['target'])
    except Rejected:
        target_ok=False
    return ('PROTECTED' if target_ok else 'PROTECTED_TARGET_REVIEW'),None

def realized(api,entry,exits):
    rows={}
    for order in [entry]+exits:
        fills=api.call('GET','/fapi/v1/userTrades',symbol=SYMBOL,orderId=order['orderId'],limit=1000)
        fills=[x for x in fills if str(x['orderId'])==str(order['orderId'])]
        if len(fills)>=1000 or sum((dec(x['qty']) for x in fills),dec(0))!=dec(order['executedQty']):
            raise ValueError('Fill history incomplete')
        for f in fills:
            if f['commissionAsset']!='USDT': raise ValueError('Non-USDT fee requires review')
            rows[str(f['id'])]=f
    start=min(int(x['time']) for x in rows.values())
    end=max(int(x['time']) for x in rows.values())
    funding=api.call('GET','/fapi/v1/income',symbol=SYMBOL,incomeType='FUNDING_FEE',startTime=start,endTime=end,limit=1000)
    if len(funding)>=1000 or any(x['asset']!='USDT' or x.get('symbol')!=SYMBOL for x in funding):
        raise ValueError('Funding history requires review')
    gross=sum((dec(x['realizedPnl']) for x in rows.values()),dec(0))
    fees=sum((dec(x['commission']) for x in rows.values()),dec(0))
    net=gross-fees+sum((dec(x['income']) for x in funding),dec(0))
    return net,dict(gross=str(gross),fees=str(fees),net=str(net),fills=list(rows.values()),funding=funding)

def settle(book,net,details):
    s=book.s
    s['balance']=str(dec(s['balance'])+net);s['equity']=s['balance']
    s['closed_trades']+=1
    s['loss_streak']=s['loss_streak']+1 if net<0 else 0
    s['active']=None
    if s['loss_streak']>=3: s['lock']='LOSS_STREAK_REVIEW'
    if s['closed_trades']>=3 and not s['lock']: s['lock']='PILOT_BATCH_COMPLETE'
    book.save({'event':'CLOSED','result':details})

def risk_check(s,t):
    if s['day']!=day(t):
        s['day']=day(t);s['day_start']=s['equity']
        if s['lock']=='DAILY_LOSS': s['lock']=None
    equity=dec(s['equity'])
    s['high_water']=str(max(dec(s['high_water']),equity))
    if equity<=dec(s['day_start'])*dec('.98') and not s['lock']: s['lock']='DAILY_LOSS'
    if equity<=dec(s['high_water'])*dec('.92'): s['lock']='DRAWDOWN_REVIEW'
    if t-s['created_ms']>=POLICY['duration_ms']: s['lock']=s['lock'] or 'PILOT_TIME_COMPLETE'

def candidate(api,public,book):
    t=now_ms()
    bars=closed_bars(public.get('/fapi/v1/klines',symbol=SYMBOL,interval='15m',limit=200),t)
    stamp=t//BAR_MS*BAR_MS-1
    if not bars or bars[-1].close_ms!=stamp: raise ValueError('Stale signal candles')
    if book.s['seen_ms']==stamp: return None
    book.s['seen_ms']=stamp;book.save()
    if not 0<=t-stamp<=CFG.max_data_age_ms: return None
    if now_ms()//BAR_MS*BAR_MS-1!=stamp:
        book.save('CANDLE_BOUNDARY_SKIP');return None
    signal=strategy(SYMBOL,bars)
    if signal is None: return None
    book.s['last_signal_ms']=stamp
    rows=api.call('GET','/fapi/v1/exchangeInfo')['symbols']
    rule=Rules.from_exchange(next(x for x in rows if x['symbol']==SYMBOL))
    reference=dec(api.call('GET','/fapi/v1/ticker/price',symbol=SYMBOL)['price'])
    try:
        plan=size(signal,reference,min(dec(book.s['equity']),dec(50)),rule,CFG)
        assessment=reward_risk(dict(entry=plan.entry,qty=plan.qty,target=plan.target,
            side=signal.side,entry_fee=plan.notional*CFG.fee_bps/10000,
            funding_reserve=plan.notional*CFG.funding_reserve_bps/10000,modeled_risk=plan.risk),CFG)
        if dec(assessment['net_rr'])<1: raise ValueError('NET_RR_BELOW_1')
        if plan.risk>dec('.25') or plan.notional>25: raise ValueError('Pilot cap exceeded')
    except ValueError as exc:
        book.save({'event':'SIGNAL_REJECTED','reason':str(exc),'signal_id':signal.key})
        return None
    return dict(environment='testnet',symbol=SYMBOL,side='BUY' if signal.side==1 else 'SELL',
        quantity=str(plan.qty),reference=str(reference),stop=str(plan.stop),target=str(plan.target),
        purpose='STRATEGY_PILOT',signal_id=signal.key,signal_observed_ms=t)

def tick(book,api,public):
    s=book.s;t=now_ms()
    s['last_check_ms']=t
    if s['active']:
        path=book.root/s['active']['file']
        with Journal(path) as j:
            c=Coordinator(j,api)
            pos=position(api)
            if pos:
                s['equity']=str(dec(s['balance'])+dec(pos.get('unRealizedProfit','0'))-
                    abs(dec(pos['positionAmt']))*(dec(pos['markPrice'])*dec('.0008')+dec(pos['entryPrice'])*dec('.0015')))
            risk_check(s,t)
            force=bool(s['lock']) or t-s['active']['started_ms']>=POLICY['hold_ms']
            phase,data=reconcile(c,force_exit=force)
            s['phase']=phase
            if phase=='CLOSED':
                net,details=realized(api,*data)
                settle(book,net,details)
                risk_check(s,t)
            elif phase=='NO_FILL':
                s['active']=None;s['lock']='NO_FILL_REVIEW'
            elif phase.startswith('UNKNOWN') or phase in ('PROTECTED_TARGET_REVIEW',):
                s['lock']='ORDER_REVIEW'
            s['error']=None;book.save()
        return
    risk_check(s,t)
    if s['lock']:
        s['phase']='LOCKED';book.save();return
    # Scan once a minute, never submit merely to increase trade count.
    if t-s.get('last_scan_ms',0)<60000:
        book.save();return
    s['last_scan_ms']=t
    if position(api) is not None or api.call('GET','/fapi/v1/openOrders') or api.call('GET','/fapi/v1/openAlgoOrders'):
        s['lock']='UNOWNED_ACCOUNT_STATE';book.save();return
    p=candidate(api,public,book)
    if p:
        if now_ms()-p['signal_observed_ms']>60000:
            book.save('STALE_PLAN_SKIPPED');return
        filename='trade-'+str(s['closed_trades']+1)+'.db'
        # Persist ownership before any order; partial init on crash locks for review.
        s['active']={'file':filename,'started_ms':now_ms()}
        book.save({'event':'SIGNAL_SELECTED','plan':p})
        with Journal(book.root/filename) as j:
            Coordinator(j,api).prepare(p)
        tick(book,api,public)
    else:
        s['phase']='WAIT_SIGNAL';s['error']=None;book.save()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('run','status'))
    parser.add_argument('--directory',required=True)
    parser.add_argument('--credentials')
    args=parser.parse_args()
    if args.command=='status':
        with closing(sqlite3.connect((Path(args.directory)/'pilot.db').resolve().as_uri()+'?mode=ro',uri=True)) as db:
            s=json.loads(db.execute('SELECT data FROM state').fetchone()[0])
        s.pop('identity',None);print(encode(s));return
    if args.credentials:
        import stat
        credential_path=Path(args.credentials)
        info=credential_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
            raise ValueError('Credential file must be owned regular file mode 0600')
        credentials=json.loads(credential_path.read_text())
        if credentials.get('environment')!='testnet':
            raise ValueError('Testnet credentials required')
        api=DemoClient(credentials['api_key'],credentials['api_secret'])
    else:
        api=DemoClient(os.environ.get('APIKEYBD',''),os.environ.get('SECKEYBD',''))
    book=Book(args.directory,api.identity)
    try:
        if not book.s['active'] and not book.s['lock']: setup(api,SYMBOL)
        public=BinancePublic()
        while True:
            delay=10
            try:
                tick(book,api,public)
            except Exception as exc:
                book.s['error']=type(exc).__name__
                if isinstance(exc,Rejected):book.s['error']+=':'+str(exc.code)
                book.s['lock']=book.s['lock'] or 'API_OR_STATE_REVIEW'
                book.s['phase']='REVIEW'
                book.save({'event':'ERROR','type':book.s['error']})
                delay=60
            print(encode({k:book.s[k] for k in ('phase','equity','closed_trades','lock','error','last_check_ms')}),flush=True)
            time.sleep(delay)
    finally:
        book.close()

if __name__=='__main__': main()
