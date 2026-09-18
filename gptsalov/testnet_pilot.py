"""Bounded multi-symbol USDT Testnet strategy pilot. Fixed demo host, no production orders."""
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
from .execution_sizing import buffered_size, modeled_fill_risk
from .adaptive_risk import adaptive_stop, risk_allowance, VERSION
from .research import reward_risk
from .testnet import DemoClient, Coordinator, Journal, Rejected, Uncertain, ORDER, ALGO, TERMINAL
from .testnet_smoke import setup

SYMBOL='ETHUSDT'
CFG=Config(initial_equity=dec(50),max_notional_fraction=dec('.5'))
POLICY={'schema':1,'environment':'testnet','symbol':SYMBOL,'budget':'50',
        'risk_cap':'2','stop_model':VERSION,'notional_cap':'25','min_rr':'1','max_trades':3,
        'max_positions':3,'duration_ms':86400000,'hold_ms':14400000,'config_hash':CFG.fingerprint}

def now_ms(): return int(time.time()*1000)
def day(t): return datetime.fromtimestamp(t/1000,ZoneInfo('Asia/Bangkok')).date().isoformat()

class Book:
    def __init__(self,root,identity,allow_risk_upgrade=False):
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
            if (self.s['identity']==identity and self.s['policy']!=POLICY
                    and allow_risk_upgrade):
                from .risk_upgrade import upgrade_policy
                try:
                    upgrade_policy(self.s, POLICY)
                    self.save({'event':'RISK_POLICY_UPGRADED','risk_model':POLICY['risk_model']})
                except Exception:
                    self.close(); raise
            if self.s['identity']!=identity or self.s['policy']!=POLICY:
                self.close(); raise ValueError('Pilot identity/policy mismatch')
        else:
            t=now_ms()
            self.s=dict(identity=identity,policy=POLICY,created_ms=t,active=[],next_trade_id=1,
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

def position(api,symbol=None):
    params={'symbol':symbol} if symbol else {}
    rows=api.call('GET','/fapi/v3/positionRisk',**params)
    rows=[x for x in rows if dec(x['positionAmt'])!=0 and (symbol is None or x['symbol']==symbol)]
    if len(rows)>1:raise ValueError('Unexpected multiple positions')
    return rows[0] if rows else None

def positions(api):
    rows=api.call('GET','/fapi/v3/positionRisk')
    result=[x for x in rows if dec(x['positionAmt'])!=0]
    if len({x['symbol'] for x in result})!=len(result):
        raise ValueError('Duplicate symbol positions')
    return result

def active_slots(state):
    active=state.get('active')
    if active is None:return []
    return active if isinstance(active,list) else [active]

def owned_exit_orders(c):
    """Require exchange order evidence, not a briefly empty position response."""
    symbol=c.plan()['symbol']
    result={}
    if c.j.get('exit'):
        try:
            o=c.api.call('GET',ORDER,symbol=symbol,origClientOrderId=c.cid('exit'))
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
            o=c.api.call('GET',ORDER,symbol=symbol,orderId=oid)
            result[str(o['orderId'])]=o
    return list(result.values())

def close_evidence(c,entry):
    symbol=c.plan()['symbol']
    orders=owned_exit_orders(c)
    opposite='SELL' if c.plan()['side']=='BUY' else 'BUY'
    if any(o.get('symbol')!=symbol or o.get('side')!=opposite for o in orders):
        raise ValueError('Closing order identity mismatch')
    quantity=sum((dec(x['executedQty']) for x in orders),dec(0))
    if quantity!=dec(entry['executedQty']):
        return None
    if not all(o['status'] in TERMINAL for o in orders):
        return None
    return orders

def cancel_owned(c):
    symbol=c.plan()['symbol']
    orders=c.api.call('GET','/fapi/v1/openAlgoOrders',symbol=symbol)
    owned={c.cid('stop'),c.cid('target')}
    if any(o.get('clientAlgoId') not in owned for o in orders):
        raise ValueError('Unowned conditional order')
    for o in orders:
        c.api.call('DELETE',ALGO,clientAlgoId=o['clientAlgoId'])
    return not c.api.call('GET','/fapi/v1/openAlgoOrders',symbol=symbol)

def exit_once(c,entry,pos):
    symbol=c.plan()['symbol']
    if entry['status'] not in TERMINAL:
        c.api.call('DELETE',ORDER,symbol=symbol,origClientOrderId=c.cid('entry'))
        return 'CANCEL_ENTRY_BEFORE_EXIT'
    amount=dec(pos['positionAmt'])
    sign=1 if c.plan()['side']=='BUY' else -1
    if amount*sign<=0 or abs(amount)>dec(entry['executedQty']):
        raise ValueError('Position ownership mismatch')
    c.once('exit',ORDER,dict(symbol=symbol,side='SELL' if sign==1 else 'BUY',
        positionSide='BOTH',type='MARKET',quantity=str(abs(amount)),
        reduceOnly='true',newClientOrderId=c.cid('exit')))
    return 'EXIT_RECONCILIATION'

def verify_protection(c, name, opposite, price):
    """Persist sanitized GET evidence; ACK alone is never protection proof."""
    symbol=c.plan()['symbol']
    expected = dict(clientAlgoId=c.cid(name), symbol=symbol, side=opposite,
                    positionSide='BOTH', workingType='CONTRACT_PRICE',
                    orderType='STOP_MARKET' if name=='stop' else 'TAKE_PROFIT_MARKET')
    result = {'at_ms':now_ms(), 'reason':'MISMATCH', 'retryable':False}
    try:
        row=c.api.call('GET',ALGO,clientAlgoId=c.cid(name))
        fields=tuple(expected)+('algoStatus','closePosition','triggerPrice')
        result['observed']={k:row.get(k) for k in fields}
        matches=all(row.get(k)==v for k,v in expected.items())
        matches=matches and row.get('closePosition') in (True,'true') and dec(row.get('triggerPrice','NaN'))==dec(price)
        result['reason']='CONFIRMED' if matches and row.get('algoStatus')=='NEW' else 'MISMATCH'
    except Rejected as exc:
        result.update(reason='REJECTED',code=exc.code,retryable=exc.code==-2013)
    except Uncertain:
        result.update(reason='UNAVAILABLE',retryable=True)
    except (ValueError,TypeError,AttributeError,ArithmeticError):
        result['reason']='MALFORMED'
    old=c.j.get(name+'_verification') or {}
    result['first_unconfirmed_ms']=(old.get('first_unconfirmed_ms',result['at_ms'])
        if result['reason']!='CONFIRMED' else None)
    if result['first_unconfirmed_ms'] is None and result['reason']!='CONFIRMED':
        result['first_unconfirmed_ms']=result['at_ms']
    c.j.put(name+'_verification',result)
    return result

def reconcile(c,force_exit=False):
    """Only first entry POST uses preflight; restart never retries that POST."""
    symbol=c.plan()['symbol']
    p=c.plan()
    if c.j.get('entry') is None:
        c.preflight(p)
        c.once('entry',ORDER,dict(symbol=symbol,side=p['side'],positionSide='BOTH',
            type='MARKET',quantity=p['quantity'],newClientOrderId=c.cid('entry')))
    try:
        entry=c.api.call('GET',ORDER,symbol=symbol,origClientOrderId=c.cid('entry'))
    except Rejected:
        return 'UNKNOWN_ENTRY',None
    if entry.get('symbol')!=symbol or entry.get('side')!=p['side']:
        raise ValueError('Entry identity mismatch')
    filled=dec(entry['executedQty'])
    if filled==0:
        return ('NO_FILL' if entry['status'] in TERMINAL else 'WAIT_ENTRY'),None
    if filled<0 or filled>dec(p['quantity']):
        raise ValueError('Invalid entry fill')
    sign=1 if p['side']=='BUY' else -1
    opposite='SELL' if sign==1 else 'BUY'
    c.once('stop',ALGO,dict(symbol=symbol,side=opposite,positionSide='BOTH',
        algoType='CONDITIONAL',type='STOP_MARKET',triggerPrice=p['stop'],
        closePosition='true',workingType='CONTRACT_PRICE',clientAlgoId=c.cid('stop')))
    if entry['status'] not in TERMINAL:
        c.api.call('DELETE',ORDER,symbol=symbol,origClientOrderId=c.cid('entry'))
        return 'CANCEL_PARTIAL_ENTRY',None
    pos=position(c.api,symbol)
    if pos is None:
        exits=close_evidence(c,entry)
        if exits is None:
            return 'WAIT_POSITION_OR_CLOSE_EVIDENCE',None
        if not cancel_owned(c): return 'CLEANUP_PENDING',None
        if position(c.api,symbol) is not None: return 'WAIT_POSITION_SYNC',None
        if c.api.call('GET','/fapi/v1/openOrders'):
            raise ValueError('Unexpected open regular order')
        return 'CLOSED',(entry,exits)
    amount=dec(pos['positionAmt'])
    if amount*sign<=0 or abs(amount)>filled:
        raise ValueError('Position ownership mismatch')
    avg=dec(entry['avgPrice']);stop=dec(p['stop'])
    risk=modeled_fill_risk(abs(amount),avg,stop)
    reasons=[]
    allowed_risk=min(dec(p.get('risk_cap','.25')),dec(p.get('risk_assessment',{}).get('risk_budget',p.get('risk_cap','.25'))))
    if risk>allowed_risk:reasons.append('FILL_RISK_EXCEEDED')
    if abs(amount)*avg>25:reasons.append('FILL_NOTIONAL_EXCEEDED')
    if abs(avg/dec(p['reference'])-1)>dec('.005'):reasons.append('FILL_DRIFT_EXCEEDED')
    if sign*(avg-stop)<=0:reasons.append('STOP_WRONG_SIDE')
    if reasons:
        c.j.put('risk_exit_reason',dict(reasons=reasons,at_ms=now_ms(),modeled_risk=str(risk),notional=str(abs(amount)*avg)))
        force_exit=True
    if force_exit or c.j.get('exit'):
        return exit_once(c,entry,pos),None
    if verify_protection(c,'stop',opposite,stop)['reason']!='CONFIRMED':
        return exit_once(c,entry,pos),None
    c.once('target',ALGO,dict(symbol=symbol,side=opposite,positionSide='BOTH',
        algoType='CONDITIONAL',type='TAKE_PROFIT_MARKET',triggerPrice=p['target'],
        closePosition='true',workingType='CONTRACT_PRICE',clientAlgoId=c.cid('target')))
    # A target that cannot be confirmed cannot cause a second POST.
    verification=verify_protection(c,'target',opposite,p['target'])
    if verification['reason']=='CONFIRMED':
        return 'PROTECTED',None
    # A known POST rejection is not a visibility delay. Never retry its POST.
    if (verification['retryable'] and c.j.get('target').get('phase')!='REJECTED'
            and 0<=now_ms()-verification['first_unconfirmed_ms']<30000):
        return 'PROTECTED_TARGET_PENDING',None
    return 'PROTECTED_TARGET_REVIEW',None

def realized(api,entry,exits):
    symbol=entry['symbol']
    if any(o.get('symbol')!=symbol for o in exits):raise ValueError('Cross-symbol accounting refused')
    rows={}
    for order in [entry]+exits:
        fills=api.call('GET','/fapi/v1/userTrades',symbol=symbol,orderId=order['orderId'],limit=1000)
        fills=[x for x in fills if str(x['orderId'])==str(order['orderId'])]
        if len(fills)>=1000 or sum((dec(x['qty']) for x in fills),dec(0))!=dec(order['executedQty']):
            raise ValueError('Fill history incomplete')
        for f in fills:
            if f['commissionAsset']!='USDT': raise ValueError('Non-USDT fee requires review')
            rows[str(f['id'])]=f
    start=min(int(x['time']) for x in rows.values())
    end=max(int(x['time']) for x in rows.values())
    funding=api.call('GET','/fapi/v1/income',symbol=symbol,incomeType='FUNDING_FEE',startTime=start,endTime=end,limit=1000)
    if len(funding)>=1000 or any(x['asset']!='USDT' or x.get('symbol')!=symbol for x in funding):
        raise ValueError('Funding history requires review')
    gross=sum((dec(x['realizedPnl']) for x in rows.values()),dec(0))
    fees=sum((dec(x['commission']) for x in rows.values()),dec(0))
    net=gross-fees+sum((dec(x['income']) for x in funding),dec(0))
    return net,dict(gross=str(gross),fees=str(fees),net=str(net),fills=list(rows.values()),funding=funding)

def settle(book,net,details,slot=None):
    s=book.s
    s['balance']=str(dec(s['balance'])+net);s['equity']=s['balance']
    s['closed_trades']+=1
    s['loss_streak']=s['loss_streak']+1 if net<0 else 0
    if slot is None:
        s['active']=[]
    else:
        s['active']=[x for x in active_slots(s) if x.get('file')!=slot.get('file')]
    if s['loss_streak']>=3: s['lock']='LOSS_STREAK_REVIEW'
    if s['closed_trades']-s.get('batch_start_closed_trades',0)>=POLICY['max_trades'] and not s['lock']: s['lock']='PILOT_BATCH_COMPLETE'
    book.save({'event':'CLOSED','result':details})

def risk_check(s,t):
    if s['day']!=day(t):
        s['day']=day(t);s['day_start']=s['equity']
        if s['lock']=='DAILY_LOSS': s['lock']=None
    equity=dec(s['equity'])
    s['high_water']=str(max(dec(s['high_water']),equity))
    if equity<=dec(s['day_start'])*dec('.98') and not s['lock']: s['lock']='DAILY_LOSS'
    if equity<=dec(s['high_water'])*dec('.92'): s['lock']='DRAWDOWN_REVIEW'
    if t-s.get('batch_started_ms',s['created_ms'])>=POLICY['duration_ms']: s['lock']=s['lock'] or 'PILOT_TIME_COMPLETE'

def apply_order_risk(api,book,signal,reference,plan,rule,atr):
    from dataclasses import replace
    from .order_risk import plan_order, available_usdt, VERSION as RISK_VERSION
    state={k:book.s[k] for k in ('equity','high_water','day_start','loss_streak')}
    assessment=plan_order(symbol=signal.symbol,side=signal.side,reference=reference,
        stop=plan.stop,target=plan.target,atr=atr,quantity_cap=plan.qty,rules=rule,
        state=state,available_balance=available_usdt(api),
        brackets=api.call('GET','/fapi/v1/leverageBracket',symbol=signal.symbol))
    details=dict(risk_model=RISK_VERSION,leverage=assessment['leverage'],
        risk_atr=str(assessment['atr']),risk_state=state,risk_assessment=assessment)
    plan=replace(plan,qty=assessment['quantity'],notional=assessment['notional'],
        risk=assessment['modeled_risk'],budget=assessment['risk_budget'])
    book.s['last_risk_review']=assessment
    book.save({'event':'RISK_LEVERAGE_REVIEW','symbol':signal.symbol,'review':assessment})
    return plan,details


def multi_candidate(api,public,book,limit=1,exclude=(),risk_reserved=dec(0)):
    from .market_scanner import read as read_scan, classify
    t=now_ms();stamp=t//BAR_MS*BAR_MS-1
    s=book.s
    if type(limit) is not int or not 1<=limit<=POLICY['max_positions']:
        raise ValueError('INVALID_SLOT_LIMIT')
    if s.get('seen_ms')==stamp:return [] if limit>1 else None
    if not 0<=t-stamp<=CFG.max_data_age_ms:return [] if limit>1 else None
    scan=read_scan(stamp=t,full=True)
    if scan.get('status')!='current' or not 0<=t-scan.get('started_ms',0)<=120000:
        s['last_selection']={'at_ms':t,'status':'WAIT_FRESH_SCAN'}
        return [] if limit>1 else None
    candidates=[r for r in scan.get('rows',[]) if r.get('market')=='USD-M'
                and r.get('quote')=='USDT' and r.get('contract')=='PERPETUAL'
                and r.get('decision')=='CANDIDATE' and r.get('candle_close_ms')==stamp
                and 0<=t-r.get('observed_ms',0)<=120000]
    s['seen_ms']=stamp
    selection={'at_ms':t,'candle_close_ms':stamp,'status':'NO_ELIGIBLE_SIGNAL',
               'candidates':len(candidates),'rejected':{}}
    s['last_selection']=selection;book.save()
    if not candidates:return [] if limit>1 else None
    candidates.sort(key=lambda r:(-r.get('score',0),-r.get('volume',0),r['symbol']))
    demo_rows=api.call('GET','/fapi/v1/exchangeInfo')['symbols']
    demo={r['symbol']:r for r in demo_rows if r.get('status')=='TRADING'
          and r.get('contractType')=='PERPETUAL' and r.get('quoteAsset')=='USDT'
          and r.get('marginAsset')=='USDT'}
    selection['supported_testnet_symbols']=len(demo)
    market={r['symbol']:r for r in public.get('/fapi/v1/exchangeInfo')['symbols']}
    tickers={r['symbol']:r for r in public.get('/fapi/v1/ticker/24hr')}
    books={r['symbol']:r for r in public.get('/fapi/v1/ticker/bookTicker')}
    from .multiagent_gate import evaluate
    selected=[];excluded=set(exclude)
    for row in candidates:
        symbol=row['symbol'];t=now_ms()
        if symbol in excluded:
            selection['rejected'][symbol]='ALREADY_ACTIVE';continue
        if not 0<=t-stamp<=CFG.max_data_age_ms:
            selection['status']='ENTRY_WINDOW_EXPIRED';break
        if symbol not in demo:
            selection['rejected'][symbol]='NOT_ON_USDT_TESTNET';continue
        liquidity=classify('USD-M',market.get(symbol,{}),tickers.get(symbol),books.get(symbol),t)
        if liquidity['status']!='pending':
            selection['rejected'][symbol]=liquidity['reason'];continue
        bars=closed_bars(public.get('/fapi/v1/klines',symbol=symbol,interval='15m',limit=200),now_ms())
        if not bars or bars[-1].close_ms!=stamp:
            selection['rejected'][symbol]='STALE_CANDLES';continue
        signal=strategy(symbol,bars)
        risk_aware=bool(s.get('policy',{}).get('risk_model'))
        allowed,review=evaluate(bars,s,now_ms(),signal,symbol=symbol,risk_aware=risk_aware)
        review['symbol']=symbol
        s['last_agent_review']=review
        book.save({'event':'MULTIAGENT_REVIEW','symbol':symbol,'review':review})
        if not allowed:
            selection['rejected'][symbol]='AGENT_GATE';continue
        try:
            risk_details={}
            rule=Rules.from_exchange(demo[symbol])
            reference=dec(api.call('GET','/fapi/v1/ticker/price',symbol=symbol)['price'])
            signal,stop_evidence=adaptive_stop(signal,bars)
            total_cap,risk_evidence=risk_allowance(s)
            remaining=max(dec(0),total_cap-risk_reserved-sum((dec(x['modeled_risk']) for x in selected),dec(0)))
            slots_left=limit-len(selected)
            cap=remaining/slots_left if slots_left else dec(0)
            if cap<=0:raise ValueError('PORTFOLIO_RISK_EXHAUSTED')
            s['risk_assessment']=risk_evidence
            plan=buffered_size(signal,reference,min(dec(s['equity']),dec(50)),rule,CFG,risk_cap=cap)
            assessment=reward_risk(dict(entry=plan.entry,qty=plan.qty,target=plan.target,
                side=signal.side,entry_fee=plan.notional*CFG.fee_bps/10000,
                funding_reserve=plan.notional*CFG.funding_reserve_bps/10000,modeled_risk=plan.risk),CFG)
            if dec(assessment['net_rr'])<1:raise ValueError('NET_RR_BELOW_1')
            if plan.risk>cap or plan.notional>25:raise ValueError('CAP_EXCEEDED')
            if risk_aware:
                plan,risk_details=apply_order_risk(api,book,signal,reference,plan,rule,
                    review['strategy_quality']['atr'])
                cap=min(cap,dec(risk_details['risk_assessment']['risk_budget']))
        except ValueError as e:
            selection['rejected'][symbol]=str(e);continue
        if not 0<=now_ms()-stamp<=CFG.max_data_age_ms:
            selection['status']='ENTRY_WINDOW_EXPIRED';break
        chosen=dict(environment='testnet',symbol=symbol,side='BUY' if signal.side==1 else 'SELL',
                    quantity=str(plan.qty),reference=str(reference),stop=str(plan.stop),target=str(plan.target),
                    purpose='MULTI_MARKET_TESTNET',sizing_version='fill-envelope-v2',stop_model=VERSION,risk_cap=str(cap),modeled_risk=str(plan.risk),stop_analysis=stop_evidence,signal_id=signal.key,signal_observed_ms=now_ms(),
                    signal_close_ms=stamp,**risk_details)
        selected.append(chosen);excluded.add(symbol)
        if len(selected)>=limit:break
    if selected:
        s['last_signal_ms']=stamp
        selection.update(status='SELECTED',selected=[dict(symbol=x['symbol'],side=x['side'],
            modeled_risk=x['modeled_risk']) for x in selected])
        if len(selected)==1:
            selection.update(symbol=selected[0]['symbol'],side=selected[0]['side'],
                             modeled_risk=selected[0]['modeled_risk'])
    book.save({'event':'MARKET_SELECTION','selection':selection})
    if limit==1:return selected[0] if selected else None
    return selected

def candidate(api,public,book):
    if book.s['policy'].get('execution_universe')=='ALL_USDT_PERPETUAL':
        return multi_candidate(api,public,book)
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
    if book.s['policy'].get('decision_engine') == 'multiagent-testnet-v1':
        from .multiagent_gate import evaluate
        if book.s['policy'].get('risk_model'):
            allowed, review = evaluate(bars, book.s, t, signal, risk_aware=True)
        else:
            allowed, review = evaluate(bars, book.s, t, signal)
        book.s['last_agent_review'] = review
        book.save({'event':'MULTIAGENT_REVIEW','review':review})
        if not allowed: return None
    if signal is None: return None
    book.s['last_signal_ms']=stamp
    rows=api.call('GET','/fapi/v1/exchangeInfo')['symbols']
    rule=Rules.from_exchange(next(x for x in rows if x['symbol']==SYMBOL))
    reference=dec(api.call('GET','/fapi/v1/ticker/price',symbol=SYMBOL)['price'])
    risk_details = {}
    try:
        plan=buffered_size(signal,reference,min(dec(book.s['equity']),dec(50)),rule,CFG)
        assessment=reward_risk(dict(entry=plan.entry,qty=plan.qty,target=plan.target,
            side=signal.side,entry_fee=plan.notional*CFG.fee_bps/10000,
            funding_reserve=plan.notional*CFG.funding_reserve_bps/10000,modeled_risk=plan.risk),CFG)
        if dec(assessment['net_rr'])<1: raise ValueError('NET_RR_BELOW_1')
        if plan.risk>dec('.25') or plan.notional>25: raise ValueError('Pilot cap exceeded')
        if book.s['policy'].get('risk_model'):
            plan,risk_details=apply_order_risk(api,book,signal,reference,plan,rule,
                book.s['last_agent_review']['strategy_quality']['atr'])
    except ValueError as exc:
        book.save({'event':'SIGNAL_REJECTED','reason':str(exc),'signal_id':signal.key})
        return None
    return dict(environment='testnet',symbol=SYMBOL,side='BUY' if signal.side==1 else 'SELL',
        quantity=str(plan.qty),reference=str(reference),stop=str(plan.stop),target=str(plan.target),
        purpose='STRATEGY_PILOT',signal_id=signal.key,signal_observed_ms=t,**risk_details)

def tick(book,api,public):
    s=book.s;t=now_ms()
    s['last_check_ms']=t
    slots=active_slots(s)
    s['active']=slots
    live_positions=positions(api)
    live_symbols={x['symbol'] for x in live_positions}
    owned_symbols={x['symbol'] for x in slots}
    if not live_symbols<=owned_symbols:
        s['lock']='UNOWNED_ACCOUNT_STATE';s['phase']='LOCKED';book.save();return
    s['equity']=str(dec(s['balance'])+sum((dec(x.get('unRealizedProfit','0'))-
        abs(dec(x['positionAmt']))*(dec(x['markPrice'])*dec('.0008')+dec(x['entryPrice'])*dec('.0015'))
        for x in live_positions),dec(0)))
    risk_check(s,t)
    for slot in list(slots):
        path=book.root/slot['file']
        with Journal(path) as j:
            c=Coordinator(j,api)
            symbol=c.plan()['symbol']
            if slot.get('symbol',symbol)!=symbol:raise ValueError('Active symbol mismatch')
            force=bool(s['lock']) or t-slot['started_ms']>=POLICY['hold_ms']
            phase,data=reconcile(c,force_exit=force)
            slot['phase']=phase
            if phase=='CLOSED':
                net,details=realized(api,*data)
                plan=c.plan()
                if plan.get('stop_model')==VERSION:
                    details['stop_model']=VERSION
                    details['modeled_risk']=plan['modeled_risk']
                    s.setdefault('adaptive_results',[]).append({'net':str(net),'risk':plan['modeled_risk']})
                settle(book,net,details,slot)
                risk_check(s,t)
            elif phase=='NO_FILL':
                s['active']=[x for x in active_slots(s) if x.get('file')!=slot.get('file')]
                s['lock']='NO_FILL_REVIEW'
            elif phase.startswith('UNKNOWN') or phase in ('PROTECTED_TARGET_REVIEW',):
                s['lock']='ORDER_REVIEW'
            s['error']=None
    slots=active_slots(s)
    s['phase']='ACTIVE_'+str(len(slots)) if slots else 'WAIT_SIGNAL'
    book.save()
    s['risk_assessment']=risk_allowance(s)[1]
    if s['lock']:
        s['phase']='LOCKED';book.save();return
    batch_used=s['closed_trades']-s.get('batch_start_closed_trades',0)+len(slots)
    capacity=min(POLICY['max_positions']-len(slots),POLICY['max_trades']-batch_used)
    if capacity<=0:
        book.save();return
    # Scan once a minute, never submit merely to increase trade count.
    if t-s.get('last_scan_ms',0)<60000:
        book.save();return
    s['last_scan_ms']=t
    if api.call('GET','/fapi/v1/openOrders'):
        s['lock']='UNOWNED_ACCOUNT_STATE';book.save();return
    expected_algo=set()
    for slot in slots:
        with Journal(book.root/slot['file']) as j:
            c=Coordinator(j,api)
            for name in ('stop','target'):
                if j.get(name):expected_algo.add(c.cid(name))
    open_algos=api.call('GET','/fapi/v1/openAlgoOrders')
    if any(x.get('clientAlgoId') not in expected_algo for x in open_algos):
        s['lock']='UNOWNED_ACCOUNT_STATE';book.save();return
    reserved=sum((dec(x.get('modeled_risk','0')) for x in slots),dec(0))
    plans=multi_candidate(api,public,book,limit=capacity,exclude=owned_symbols,risk_reserved=reserved)
    for p in plans:
        if now_ms()-p['signal_observed_ms']>60000:
            book.save('STALE_PLAN_SKIPPED');break
        setup(api,p['symbol'],leverage=None if p.get('risk_model') else 2,require_empty=False)
        if p.get('purpose')=='MULTI_MARKET_TESTNET' and not 0<=now_ms()-p['signal_close_ms']<=CFG.max_data_age_ms:
            book.save('STALE_AFTER_SETUP');break
        p['portfolio_slots']=POLICY['max_positions']
        p['portfolio_symbols']=[x['symbol'] for x in active_slots(s)]
        trade_id=s.get('next_trade_id',s['closed_trades']+len(active_slots(s))+1)
        filename='trade-'+str(trade_id)+'.db';s['next_trade_id']=trade_id+1
        # Persist ownership before any order; partial init on crash locks for review.
        slot={'file':filename,'started_ms':now_ms(),'symbol':p['symbol'],'modeled_risk':p['modeled_risk'],'phase':'PREPARED'}
        s['active']=active_slots(s)+[slot]
        book.save({'event':'SIGNAL_SELECTED','plan':p})
        with Journal(book.root/filename) as j:
            c=Coordinator(j,api);c.prepare(p)
            slot['phase']=reconcile(c)[0]
        book.save()
    s['phase']='ACTIVE_'+str(len(active_slots(s))) if active_slots(s) else 'WAIT_SIGNAL'
    s['error']=None;book.save()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('run','status'))
    parser.add_argument('--directory',required=True)
    parser.add_argument('--credentials')
    parser.add_argument('--multi-agent', action='store_true')
    parser.add_argument('--multi-market', action='store_true')
    parser.add_argument('--risk-aware', action='store_true',
        help='Opt-in tested risk/quality candidate; requires --multi-agent and a matching ledger policy')
    args=parser.parse_args()
    if args.risk_aware and not args.multi_agent:
        parser.error('--risk-aware requires --multi-agent')
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
    if args.multi_agent:
        POLICY['decision_engine']='multiagent-testnet-v1'
    if args.multi_market:
        POLICY.update(symbol='ALL_USDT_PERPETUAL',execution_universe='ALL_USDT_PERPETUAL',
                      execution_version='multimarket-testnet-v1',decision_engine='multiagent-testnet-v1')
    if args.risk_aware:
        from .order_risk import VERSION
        POLICY['risk_model']=VERSION
    book=Book(args.directory,api.identity,allow_risk_upgrade=args.risk_aware)
    try:
        if not args.multi_market and not book.s['active'] and not book.s['lock']: setup(api,SYMBOL)
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
