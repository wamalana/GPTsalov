"""One bounded Testnet execution experiment. Not a strategy profitability test."""
import argparse
import os
import time
from .core import dec, ceil_step, floor_step, Rules, encode
from .testnet import DemoClient, Journal, Coordinator, Rejected, Uncertain, ORDER, ALGO, TERMINAL

def small_plan(price,rules):
    price=dec(price)
    qty=ceil_step(max(rules.min_qty,rules.min_notional*dec('1.05')/price),rules.step)
    if qty*price>25 or qty>rules.max_qty:
        raise ValueError('Minimum contract size exceeds 25 USDT smoke cap')
    stop=floor_step(price*dec('.995'),rules.tick)
    target=ceil_step(price*dec('1.01'),rules.tick)
    if stop<=0: raise ValueError('Invalid stop')
    return dict(environment='testnet',symbol=rules.symbol,side='BUY',
        reference=str(price),quantity=str(qty),stop=str(stop),target=str(target),
        purpose='ORDER_LIFECYCLE_SMOKE_NOT_STRATEGY')

def flat_positions(api):
    return all(dec(x['positionAmt'])==0 for x in api.call('GET','/fapi/v3/positionRisk'))

def setup(api,symbol,leverage=2):
    if leverage is not None and (type(leverage) is not int or leverage not in (1,2)):
        raise ValueError('Invalid Testnet leverage')
    if not flat_positions(api) or api.call('GET','/fapi/v1/openOrders') or api.call('GET','/fapi/v1/openAlgoOrders'):
        raise ValueError('Testnet account must be empty before smoke test')
    if api.call('GET','/fapi/v1/positionSide/dual')['dualSidePosition'] is not False:
        raise ValueError('One-way Testnet account required')
    rows=api.call('GET','/fapi/v1/symbolConfig',symbol=symbol)
    r=next(x for x in rows if x['symbol']==symbol)
    if r['marginType'].upper()!='ISOLATED':
        try:
            api.call('POST','/fapi/v1/marginType',symbol=symbol,marginType='ISOLATED')
        except Rejected as exc:
            if exc.code!=-4046: raise
    if leverage is not None and int(r['leverage'])!=leverage:
        api.call('POST','/fapi/v1/leverage',symbol=symbol,leverage=leverage)
    for attempt in range(3):
        rows=api.call('GET','/fapi/v1/symbolConfig',symbol=symbol)
        r=next(x for x in rows if x['symbol']==symbol)
        if r['marginType'].upper()=='ISOLATED' and (leverage is None or int(r['leverage'])==leverage):
            return
        time.sleep(1)
    raise ValueError('Testnet settings not confirmed')

def flatten(c):
    p=c.plan()
    # Never exit while an entry remainder can still add exposure.
    try:
        order=c.api.call('GET',ORDER,symbol=p['symbol'],origClientOrderId=c.cid('entry'))
        if order['status'] not in TERMINAL:
            c.api.call('DELETE',ORDER,symbol=p['symbol'],origClientOrderId=c.cid('entry'))
            order=c.api.call('GET',ORDER,symbol=p['symbol'],origClientOrderId=c.cid('entry'))
        if order['status'] not in TERMINAL: return 'REVIEW_ENTRY_NOT_TERMINAL'
    except (Rejected,Uncertain):
        return 'REVIEW_ENTRY_UNKNOWN'
    rows=c.api.call('GET','/fapi/v3/positionRisk',symbol=p['symbol'])
    amount=sum((dec(x['positionAmt']) for x in rows if x['symbol']==p['symbol']),dec(0))
    if amount==0: return 'FLAT_PENDING_CLEANUP'
    if amount<0 or amount>dec(p['quantity']): return 'REVIEW_POSITION_MISMATCH'
    c.once('exit',ORDER,dict(symbol=p['symbol'],side='SELL',type='MARKET',
        positionSide='BOTH',quantity=str(amount),reduceOnly='true',newClientOrderId=c.cid('exit')))
    return 'EXIT_RECONCILE'

def cleanup(c):
    try:
        entry=c.api.call('GET',ORDER,symbol=c.plan()['symbol'],origClientOrderId=c.cid('entry'))
        if entry['status'] not in TERMINAL: return False
    except (Rejected,Uncertain):
        return False
    for name in ('stop','target'):
        if c.j.get(name) is None: continue
        try:
            rows=c.api.call('GET','/fapi/v1/openAlgoOrders',symbol=c.plan()['symbol'])
            if any(x.get('clientAlgoId')==c.cid(name) for x in rows):
                c.api.call('DELETE',ALGO,clientAlgoId=c.cid(name))
        except (Rejected,Uncertain):
            return False
    return (flat_positions(c.api) and not c.api.call('GET','/fapi/v1/openOrders')
            and not c.api.call('GET','/fapi/v1/openAlgoOrders'))

def run(api,path):
    with Journal(path) as journal:
        c=Coordinator(journal,api)
        previous=journal.get('result')
        if previous and previous.get('clean'):
            return previous
        if not journal.db.execute('SELECT 1 FROM experiment').fetchone():
            setup(api,'ETHUSDT')
            rows=api.call('GET','/fapi/v1/exchangeInfo')['symbols']
            rules=Rules.from_exchange(next(x for x in rows if x['symbol']=='ETHUSDT'))
            price=api.call('GET','/fapi/v1/ticker/price',symbol='ETHUSDT')['price']
            c.prepare(small_plan(price,rules))
        p=c.plan()
        started=journal.get('started')
        if started is None:
            started={'at_ms':int(time.time()*1000)}
            journal.put('started',started)
        phases=[]
        # At most one entry. A resumed test never starts a second trade.
        for _ in range(10):
            try:
                phase=c.advance()
            except (Rejected,Uncertain):
                phase='REVIEW_API_UNCERTAIN'
            phases.append(phase)
            journal.put('progress',{'at_ms':int(time.time()*1000),'phases':phases})
            if phase.startswith('DONE'): break
            if phase=='PROTECTED' or time.time()*1000-started['at_ms']>30000:
                phases.append(flatten(c))
                break
            if phase.startswith('REVIEW') or phase.startswith('UNCERTAIN'):
                phases.append(flatten(c))
                break
            time.sleep(2)
        clean=False
        for _ in range(10):
            try:
                if flat_positions(api):
                    clean=cleanup(c)
                    if clean: break
                else:
                    phases.append(flatten(c))
            except (Rejected,Uncertain):
                pass
            time.sleep(2)
        actions={name:journal.get(name) for name in ('entry','stop','target','exit')}
        trades=[]
        for name in ('entry','exit'):
            if actions[name] is None: continue
            try:
                order=api.call('GET',ORDER,symbol=p['symbol'],origClientOrderId=c.cid(name))
                journal.put(name+'_observed',order)
                rows=api.call('GET','/fapi/v1/userTrades',symbol=p['symbol'],orderId=order['orderId'])
                trades.extend({k:x.get(k) for k in ('id','orderId','side','qty','price','realizedPnl','commission','commissionAsset','time')} for x in rows)
            except (Rejected,Uncertain,KeyError):
                phases.append('REVIEW_FILL_REPORT')
        result=dict(environment='BINANCE_TESTNET',purpose=p['purpose'],plan={k:v for k,v in p.items() if k!='account_identity'},
            phases=phases,clean=clean,actions={k:(v or {}).get('phase') for k,v in actions.items()},
            trades=trades,finished_ms=int(time.time()*1000),real_orders=0,
            automated_strategy_enabled=False)
        journal.put('result',result)
        return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--journal',required=True)
    a=p.parse_args()
    api=DemoClient(os.environ.get('APIKEYBD',''),os.environ.get('SECKEYBD',''))
    try:
        result=run(api,a.journal)
    except Exception as exc:
        result={'status':'REVIEW_REQUIRED','error_type':type(exc).__name__,'real_orders':0}
        if isinstance(exc,Rejected): result['api_code']=exc.code
        if isinstance(exc,ValueError): result['reason']=str(exc)
    print('SMOKE_JSON='+encode(result),flush=True)

if __name__=='__main__': main()
