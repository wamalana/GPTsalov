"""Testnet-only transport and durable single-trade coordinator.

Development component, deliberately not wired to paper services or a trading CLI.
Use a dedicated empty one-way Testnet account. Never use production credentials.
"""
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import time
import fcntl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener
from .core import dec, encode
from .market import NoRedirect

BASE = 'https://demo-fapi.binance.com'
ORDER = '/fapi/v1/order'
ALGO = '/fapi/v1/algoOrder'
TERMINAL = {'FILLED','CANCELED','EXPIRED','EXPIRED_IN_MATCH','REJECTED'}


class Uncertain(RuntimeError):
    """Request may have executed; reconcile, never blindly resubmit."""


class Rejected(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__('Testnet rejected request: '+str(code))


class DemoClient:
    """Fixed host, no redirects, no retries, sanitized errors."""
    ALLOWED = {
        ('GET','/fapi/v1/time'), ('GET','/fapi/v1/exchangeInfo'), ('GET','/fapi/v1/ticker/price'),
        ('GET','/fapi/v1/positionSide/dual'), ('GET','/fapi/v1/symbolConfig'),
        ('GET','/fapi/v3/positionRisk'), ('GET','/fapi/v1/openOrders'),
        ('GET','/fapi/v1/openAlgoOrders'),
        ('GET',ORDER), ('POST',ORDER), ('DELETE',ORDER),
        ('GET',ALGO), ('POST',ALGO), ('DELETE',ALGO)}
    def __init__(self, key, secret):
        if not key or not secret:
            raise ValueError('Dedicated Testnet credentials required')
        self.key, self.secret = key, secret
        self.identity = hashlib.sha256(key.encode()).hexdigest()
        self.opener = build_opener(NoRedirect())

    def call(self, method, path, **params):
        if (method,path) not in self.ALLOWED:
            raise ValueError('Endpoint refused')
        if any(k in params for k in ('signature','timestamp','recvWindow')):
            raise ValueError('Signing parameters reserved')
        public = path in ('/fapi/v1/time','/fapi/v1/exchangeInfo','/fapi/v1/ticker/price')
        headers = {}
        if not public:
            params.update(timestamp=int(time.time()*1000),recvWindow=5000)
            headers['X-MBX-APIKEY'] = self.key
        query = urlencode(params)
        if not public:
            query += '&signature='+hmac.new(self.secret.encode(),query.encode(),hashlib.sha256).hexdigest()
        url = BASE+path
        body = None
        if method == 'POST':
            body=query.encode()
            headers['Content-Type']='application/x-www-form-urlencoded'
        elif query:
            url += '?'+query
        try:
            with self.opener.open(Request(url,data=body,headers=headers,method=method),timeout=10) as response:
                raw=response.read(1000001)
                if len(raw)>1000000:
                    raise Uncertain('Oversized response; reconcile')
                data=json.loads(raw)
                if isinstance(data,dict) and data.get('code',0)<0:
                    if data['code'] in (-1000,-1006,-1007):
                        raise Uncertain('Execution status unknown')
                    raise Rejected(data['code'])
                return data
        except HTTPError as exc:
            # Do not expose signed URL, headers, secrets or arbitrary response text.
            if exc.code>=500 or exc.code in (408,418,429):
                raise Uncertain('Testnet unavailable; reconcile before next write') from None
            try:
                code=json.loads(exc.read(4096)).get('code',exc.code)
            except Exception:
                code=exc.code
            if code in (-1000,-1006,-1007):
                raise Uncertain('Execution status unknown') from None
            raise Rejected(code) from None
        except (URLError,OSError,ValueError,RuntimeError) as exc:
            if isinstance(exc,(Rejected,Uncertain)):
                raise
            raise Uncertain('Testnet response unavailable; reconcile') from None


class Journal:
    """Exclusive writer, durable actions before network writes; no reset."""
    def __init__(self,path):
        self.path=Path(path)
        if self.path.is_symlink() or (self.path.exists() and self.path.stat().st_nlink!=1):
            raise ValueError('Journal alias refused')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=Path(str(path)+'.lock').open('a')
        try:
            fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.db=sqlite3.connect(path)
            existing={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if existing and existing!={'experiment','actions'}:
                raise ValueError('Not a Testnet journal')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS experiment(id INTEGER PRIMARY KEY, data TEXT)')
            self.db.execute('CREATE TABLE IF NOT EXISTS actions(name TEXT PRIMARY KEY, data TEXT)')
            self.db.commit()
        except Exception:
            if hasattr(self,'db'): self.db.close()
            self.lock.close()
            raise

    def get(self,name):
        row=self.db.execute('SELECT data FROM actions WHERE name=?',(name,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self,name,data):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO actions VALUES (?,?)',(name,encode(data)))

    def close(self):
        self.db.close()
        self.lock.close()

    def __enter__(self): return self
    def __exit__(self,*args): self.close()


class Coordinator:
    """One persisted Testnet plan per journal; advance() is restartable.

    The caller must stop on REVIEW/UNCERTAIN and keep reconciling. No automatic
    new trades. Entry and emergency exit POSTs are each attempted at most once.
    """
    def __init__(self,journal,client):
        self.j,self.api=journal,client

    def prepare(self, plan):
        # Plan supplied by a reviewed sizing adapter, not arbitrary bot signals.
        p=dict(plan)
        p['account_identity']=self.api.identity
        if p.get('environment')!='testnet' or p.get('side') not in ('BUY','SELL'):
            raise ValueError('Testnet plan required')
        for k in ('quantity','reference','stop','target'):
            p[k]=str(dec(p[k]))
            if dec(p[k])<=0: raise ValueError('Positive plan values required')
        sign=1 if p['side']=='BUY' else -1
        if not sign*(dec(p['reference'])-dec(p['stop']))>0 or not sign*(dec(p['target'])-dec(p['reference']))>0:
            raise ValueError('Invalid stop/target direction')
        if dec(p['quantity'])*dec(p['reference'])>100:
            raise ValueError('Testnotional capped at 100 USDT')
        # Fee/slippage/funding allowance uses existing conservative defaults.
        q,e,s=map(dec,(p['quantity'],p['reference'],p['stop']))
        modeled=q*(abs(e-s)+(e+s)*dec('0.0008')+e*dec('0.001'))
        if modeled>dec('0.5'):
            raise ValueError('Modeled risk exceeds 0.50 USDT')
        old=self.j.db.execute('SELECT data FROM experiment WHERE id=1').fetchone()
        if old:
            if json.loads(old[0])!=p: raise ValueError('Plan immutable')
            return
        # No credentials or signatures are persisted.
        with self.j.db:
            self.j.db.execute('INSERT INTO experiment VALUES (1,?)',(encode(p),))
        self.j.put('prepared',{'at_ms':int(time.time()*1000)})

    def plan(self):
        row=self.j.db.execute('SELECT data FROM experiment WHERE id=1').fetchone()
        if not row: raise ValueError('No prepared plan')
        p=json.loads(row[0])
        if p['account_identity']!=self.api.identity:
            raise ValueError('Testnet account identity mismatch')
        return p

    def cid(self,name):
        return 'gpts-'+hashlib.sha256((encode(self.plan())+name).encode()).hexdigest()[:28]

    def once(self,name,path,params):
        if self.j.get(name) is not None:
            return  # A crash before receipt is also uncertain, never resubmit.
        self.j.put(name,{'phase':'ATTEMPTED','path':path,'params':params})
        try:
            response=self.api.call('POST',path,**params)
        except Rejected as exc:
            self.j.put(name,{'phase':'REJECTED','code':exc.code})
            return
        except Uncertain:
            return
        self.j.put(name,{'phase':'ACK','response':response})

    def preflight(self,p):
        if not 0<=int(time.time()*1000)-self.j.get('prepared')['at_ms']<=60000:
            raise ValueError('Prepared plan expired; never enter retrospectively')
        current=dec(self.api.call('GET','/fapi/v1/ticker/price',symbol=p['symbol'])['price'])
        if abs(current/dec(p['reference'])-1)>dec('0.005'):
            raise ValueError('Entry price drift exceeded')
        if self.api.call('GET','/fapi/v1/positionSide/dual')['dualSidePosition'] is not False:
            raise ValueError('One-way mode required')
        rows=self.api.call('GET','/fapi/v1/symbolConfig',symbol=p['symbol'])
        row=next(x for x in rows if x['symbol']==p['symbol'])
        if row['marginType'].upper()!='ISOLATED' or int(row['leverage'])!=2:
            raise ValueError('Isolated margin and 2x required')
        if any(dec(x['positionAmt'])!=0 for x in self.api.call('GET','/fapi/v3/positionRisk')):
            raise ValueError('Dedicated account must be flat')
        if self.api.call('GET','/fapi/v1/openOrders') or self.api.call('GET','/fapi/v1/openAlgoOrders'):
            raise ValueError('Dedicated account must have no open orders')
        from .core import Rules
        rows=self.api.call('GET','/fapi/v1/exchangeInfo')['symbols']
        row=next(x for x in rows if x['symbol']==p['symbol'])
        if row['status']!='TRADING' or row['contractType']!='PERPETUAL' or row['quoteAsset']!='USDT':
            raise ValueError('Unsupported contract')
        r=Rules.from_exchange(row)
        q,e=dec(p['quantity']),dec(p['reference'])
        if q%r.step or not r.min_qty<=q<=r.max_qty or not r.min_notional<=q*e<=r.max_notional:
            raise ValueError('Quantity/notional filter failure')
        for price in (p['stop'],p['target']):
            price=dec(price)
            if price%r.tick or not r.min_price<=price<=r.max_price:
                raise ValueError('Price filter failure')

    def advance(self):
        p=self.plan(); symbol=p['symbol']; opposite='SELL' if p['side']=='BUY' else 'BUY'
        entry=self.j.get('entry')
        if entry is None:
            self.preflight(p)
            self.once('entry',ORDER,dict(symbol=symbol,side=p['side'],type='MARKET',
                quantity=p['quantity'],newClientOrderId=self.cid('entry'),positionSide='BOTH'))
        entry=self.j.get('entry')
        try:
            o=self.api.call('GET',ORDER,symbol=symbol,origClientOrderId=self.cid('entry'))
        except (Rejected,Uncertain):
            return 'UNCERTAIN_ENTRY_NO_RESUBMIT'
        filled=dec(o['executedQty'])
        terminal=o['status'] in TERMINAL
        if filled<=0:
            return 'DONE_NO_FILL' if terminal else 'WAIT_ENTRY'
        # Close-All protects partial fills, including further fills before cancel.
        self.once('stop',ALGO,dict(symbol=symbol,side=opposite,positionSide='BOTH',
            algoType='CONDITIONAL',type='STOP_MARKET',triggerPrice=p['stop'],
            closePosition='true',workingType='CONTRACT_PRICE',clientAlgoId=self.cid('stop')))
        if not terminal:
            try:
                self.api.call('DELETE',ORDER,symbol=symbol,origClientOrderId=self.cid('entry'))
                o=self.api.call('GET',ORDER,symbol=symbol,origClientOrderId=self.cid('entry'))
            except (Rejected,Uncertain):
                return 'REVIEW_PARTIAL_ENTRY'
            if o['status'] not in TERMINAL: return 'REVIEW_PARTIAL_ENTRY'
        positions=self.api.call('GET','/fapi/v3/positionRisk',symbol=symbol)
        amt=sum((dec(x['positionAmt']) for x in positions if x['symbol']==symbol),dec(0))
        if amt==0:
            # Cancel only this journal's conditional orders.
            for name in ('stop','target'):
                if self.j.get(name) is None: continue
                try:
                    self.api.call('DELETE',ALGO,clientAlgoId=self.cid(name))
                except (Rejected,Uncertain):
                    return 'REVIEW_STOP_CLEANUP'
            return 'DONE_FLAT'
        if amt*(1 if p['side']=='BUY' else -1)<=0 or abs(amt)>dec(p['quantity']):
            return 'REVIEW_POSITION_MISMATCH'
        try:
            stop=self.api.call('GET',ALGO,clientAlgoId=self.cid('stop'))
            protected=(stop['algoStatus']=='NEW' and stop['symbol']==symbol
                and stop['side']==opposite and stop.get('closePosition') in (True,'true')
                and dec(stop['triggerPrice'])==dec(p['stop']))
        except (Rejected,Uncertain,KeyError,ValueError):
            protected=False
        avg=dec(o.get('avgPrice','0'))
        q=abs(amt); stop_price=dec(p['stop'])
        fill_risk=q*(abs(avg-stop_price)+(avg+stop_price)*dec('0.0008')+avg*dec('0.001'))
        fill_ok=(avg>0 and fill_risk<=dec('0.5')
            and abs(avg/dec(p['reference'])-1)<=dec('0.005')
            and (avg-stop_price)*(1 if p['side']=='BUY' else -1)>0)
        if protected and fill_ok and self.j.get('exit') is None:
            self.once('target',ALGO,dict(symbol=symbol,side=opposite,positionSide='BOTH',
                algoType='CONDITIONAL',type='TAKE_PROFIT_MARKET',triggerPrice=p['target'],
                closePosition='true',workingType='CONTRACT_PRICE',clientAlgoId=self.cid('target')))
            try:
                target=self.api.call('GET',ALGO,clientAlgoId=self.cid('target'))
                if not (target['algoStatus']=='NEW' and target['symbol']==symbol
                    and target['side']==opposite and target.get('closePosition') in (True,'true')
                    and dec(target['triggerPrice'])==dec(p['target'])):
                    return 'PROTECTED_TARGET_REVIEW'
            except (Rejected,Uncertain,KeyError,ValueError):
                return 'PROTECTED_TARGET_REVIEW'
            return 'PROTECTED'
        # An unconfirmed stop is not protection. Flatten only terminal entry fills.
        self.once('exit',ORDER,dict(symbol=symbol,side=opposite,type='MARKET',
            quantity=str(abs(amt)),reduceOnly='true',positionSide='BOTH',
            newClientOrderId=self.cid('exit')))
        return 'REVIEW_EXIT_RECONCILIATION'
