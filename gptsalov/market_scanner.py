"""Whole Binance Futures read-only universe scan; never sends orders."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
from urllib.request import Request, build_opener
from urllib.parse import urlencode
from urllib.error import HTTPError
from .core import BAR_MS, closed_bars, dec, encode
from .market import NoRedirect
from .multiagent_shadow import analyze, read_pilot

DB = '/home/wamalana/GPTsalov/data/market-scanner-v1/scan.db'
PILOT = '/home/wamalana/GPTsalov/data/multiagent-testnet-v1/pilot.db'
MARKETS = {'USD-M':('https://fapi.binance.com','/fapi/v1'),
           'COIN-M':('https://dapi.binance.com','/dapi/v1')}
MIN_VOLUME=10_000_000
MAX_SPREAD=10
MIN_DEPTH=100

class Halt(Exception):
    pass

class Client:
    def __init__(self):
        self.opener=build_opener(NoRedirect())
        self.last=0
    def get(self, market, endpoint, **params):
        if endpoint not in ('time','exchangeInfo','ticker/24hr','ticker/bookTicker','klines'):
            raise ValueError('Endpoint not allowed')
        if set(params)-({'symbol','interval','limit'} if endpoint=='klines' else set()):
            raise ValueError('Parameters not allowed')
        base,prefix=MARKETS[market]
        time.sleep(max(0,.4-(time.monotonic()-self.last)))
        self.last=time.monotonic()
        url=base+prefix+'/'+endpoint+('?' + urlencode(params) if params else '')
        try:
            with self.opener.open(Request(url,method='GET',headers={'User-Agent':'GPTsalov-scanner/1.0'}),timeout=12) as r:
                raw=r.read(8_000_001)
                if len(raw)>8_000_000:raise ValueError('Response too large')
                # Leave room for existing pilot and other services sharing this IP.
                weight=int(r.headers.get('X-MBX-USED-WEIGHT-1M','0'))
                if weight>=1000:raise Halt('WEIGHT_BUDGET')
                data=json.loads(raw)
                if isinstance(data,dict) and data.get('code',0)!=0:raise ValueError('Exchange error')
                return data
        except HTTPError as e:
            if e.code in (403,418,429,451):raise Halt('HTTP_'+str(e.code)) from e
            raise

def clock():
    return int(time.time()*1000)

def classify(market, meta, ticker, book, stamp):
    row={'market':market,'symbol':meta.get('symbol','?'),'contract':meta.get('contractType'),
         'quote':meta.get('quoteAsset'),'status':'filtered','reason':None,
         'execution_enabled':False}
    try:
        if meta.get('status',meta.get('contractStatus'))!='TRADING':raise ValueError('NOT_TRADING')
        if ticker is None or book is None:raise ValueError('MISSING_TICKER_OR_BOOK')
        for value in (ticker['closeTime'],book['time']):
            if not -5000<=stamp-int(value)<=120000:raise ValueError('STALE_TICKER')
        bid,ask=dec(book['bidPrice']),dec(book['askPrice'])
        if not 0<bid<=ask:raise ValueError('INVALID_SPREAD')
        multiplier=dec(meta['contractSize']) if market=='COIN-M' else None
        volume=dec(ticker['volume'])*multiplier if multiplier else dec(ticker['quoteVolume'])
        depth=min(dec(book['bidQty']),dec(book['askQty']))*multiplier if multiplier else min(dec(book['bidQty'])*bid,dec(book['askQty'])*ask)
        spread=(ask-bid)/((ask+bid)/2)*10000
        row.update(volume=float(volume),spread_bps=float(spread),depth=float(depth),
                   change_pct=float(dec(ticker['priceChangePercent'])))
        if meta.get('underlyingType')!='COIN':raise ValueError('NON_CRYPTO')
        if market=='USD-M' and meta.get('quoteAsset') not in ('USDT','USDC','USD1'):raise ValueError('UNSUPPORTED_QUOTE')
        if stamp-int(meta['onboardDate'])<30*86400000:raise ValueError('NEW_CONTRACT')
        if volume<MIN_VOLUME:raise ValueError('LOW_VOLUME')
        if spread>MAX_SPREAD:raise ValueError('WIDE_SPREAD')
        if depth<MIN_DEPTH:raise ValueError('THIN_BOOK')
        row['status']='pending'
    except (ValueError,KeyError,TypeError,ArithmeticError) as e:
        row['reason']=str(e) if isinstance(e,ValueError) else 'INVALID_MARKET_DATA'
    return row

def write(path,data):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path,timeout=5)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY,data TEXT)')
        db.execute('INSERT OR REPLACE INTO snapshots VALUES(1,?)',(encode(data),))

def read(path=DB,stamp=None,full=False):
    stamp=clock() if stamp is None else stamp
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            data=json.loads(db.execute('SELECT data FROM snapshots WHERE id=1').fetchone()[0])
        if not 0<=stamp-data['started_ms']<=20*60000:data['status']='stale'
        if not full:data.pop('rows',None)
        return data
    except (sqlite3.Error,OSError,ValueError,TypeError,KeyError):
        return {'status':'unavailable','ranked':[],'markets':{},'execution_enabled':False}

def summarize(data):
    rows=data['rows']
    evaluated=[r for r in rows if r['status']=='analyzed']
    ranked=sorted(evaluated,key=lambda r:(r['decision']=='CANDIDATE',r['alignment'],r['score'],r['volume'],-r['spread_bps']),reverse=True)
    data.update(total=len(rows),active=sum(r.get('reason')!='NOT_TRADING' for r in rows),
                eligible=sum(r['status']!='filtered' for r in rows),analyzed=len(evaluated),
                candidates=sum(r['decision']=='CANDIDATE' for r in evaluated),
                reasons=dict(Counter(r['reason'] for r in rows if r.get('reason'))),
                ranked=ranked[:30])
    for market,info in data['markets'].items():
        subset=[r for r in rows if r['market']==market]
        info.update(total=len(subset),active=sum(r.get('reason')!='NOT_TRADING' for r in subset),
                    analyzed=sum(r['status']=='analyzed' for r in subset))
    data['updated_ms']=clock()

def run(path=DB,pilot=PILOT,client=None):
    client=client or Client()
    old=read(path)
    if clock()<old.get('cooldown_until_ms',0):return old
    data={'schema':1,'status':'running','started_ms':clock(),'markets':{},'rows':[],
          'scope':'All exchange-listed USD-M and COIN-M contracts; deep analysis after liquidity filters',
          'ranking':'heuristic_not_probability','execution_enabled':False,
          'execution_scope':'Existing ETHUSDT Testnet pilot only',
          'filters':{'min_volume_24h':MIN_VOLUME,'max_spread_bps':MAX_SPREAD,'min_book_depth':MIN_DEPTH,'min_age_days':30}}
    for market in MARKETS:
        data['markets'][market]={'status':'pending'}
    summarize(data);write(path,data)
    try:
        for market in MARKETS:
            try:
                server=int(client.get(market,'time')['serverTime'])
                if abs(server-clock())>5000:raise ValueError('CLOCK_SKEW')
                exchange=client.get(market,'exchangeInfo')
                ticks={x['symbol']:x for x in client.get(market,'ticker/24hr')}
                books={x['symbol']:x for x in client.get(market,'ticker/bookTicker')}
                stamp=clock()
                data['rows'].extend(classify(market,m,ticks.get(m['symbol']),books.get(m['symbol']),stamp) for m in exchange['symbols'])
                data['markets'][market]['status']='current'
            except Halt:raise
            except Exception as e:data['markets'][market].update(status='error',error=type(e).__name__)
        pending=sorted((r for r in data['rows'] if r['status']=='pending'),key=lambda r:-r['volume'])
        for index,row in enumerate(pending):
            if clock()-data['started_ms']>600000:raise Halt('SCAN_TIME_BUDGET')
            try:
                now=clock()
                raw=client.get(row['market'],'klines',symbol=row['symbol'],interval='15m',limit=200)
                bars=closed_bars(raw,now)
                try:state=read_pilot(pilot)
                except Exception:state={}
                result=analyze(bars,state,clock(),symbol=row['symbol'],scan_mode=True)
                votes={v['agent']:v['verdict'] for v in result['votes']}
                aligned=votes.get('trend') in ('LONG','SHORT') and votes.get('trend')==votes.get('momentum')
                candidate=bool(result['baseline_signal']) and aligned and all(votes.get(k)=='PASS' for k in ('data','volatility'))
                row.update(status='analyzed',decision='CANDIDATE' if candidate else 'WAIT',
                           votes=votes,alignment=int(aligned),candle_close_ms=result['candle_close_ms'],
                           score=float(result['baseline_signal']['score']) if result['baseline_signal'] else 0,
                           direction=votes.get('trend'),observed_ms=clock(),
                           portfolio_gate=votes.get('risk'),reason=None)
                if votes.get('data')!='PASS':row.update(status='error',reason='INVALID_OR_STALE_CANDLES')
            except Halt:raise
            except Exception as e:row.update(status='error',reason=type(e).__name__)
            if index%20==0:summarize(data);write(path,data)
        data['status']='current' if all(m['status']=='current' for m in data['markets'].values()) and not any(r['status'] in ('error','pending') for r in data['rows']) else 'partial'
    except Halt as e:
        data.update(status='partial',halt=str(e),cooldown_until_ms=clock()+30*60000)
    finally:
        data['finished_ms']=clock()
        summarize(data);write(path,data)
    return data

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',default=DB)
    p.add_argument('--pilot-db',default=PILOT)
    p.add_argument('--read',action='store_true')
    p.add_argument('--full',action='store_true')
    args=p.parse_args()
    data=read(args.db,full=args.full) if args.read else run(args.db,args.pilot_db)
    if not args.full:data.pop('rows',None)
    print(encode(data),flush=True)
if __name__=='__main__':main()
