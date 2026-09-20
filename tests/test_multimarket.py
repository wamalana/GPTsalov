import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from gptsalov.core import dec,Signal,BAR_MS
from gptsalov.testnet import Journal,Coordinator,ORDER,ALGO
from gptsalov import testnet_pilot as p
from test_testnet_pilot import PilotExchange
from test_testnet import PLAN

class SymbolExchange:
    identity='test'
    def __init__(self,symbol):
        self.symbol=symbol;self.base=PilotExchange();self.identity=self.base.identity;self.requests=[]
    def call(self,method,path,**kw):
        self.requests.append((method,path,dict(kw)))
        if 'symbol' in kw:
            assert kw['symbol']==self.symbol
            kw={**kw,'symbol':'ETHUSDT'}
        r=copy.deepcopy(self.base.call(method,path,**kw))
        def replace(v):
            if isinstance(v,dict):return {k:replace(x) for k,x in v.items()}
            if isinstance(v,list):return [replace(x) for x in v]
            return self.symbol if v=='ETHUSDT' else v
        return replace(r)

class MultiMarketTests(unittest.TestCase):
    def test_non_eth_full_lifecycle_and_restart(self):
        for symbol in ('SOLUSDT','BNBUSDT'):
            with self.subTest(symbol=symbol),TemporaryDirectory() as d:
                api=SymbolExchange(symbol)
                path=Path(d)/'trade.db'
                with Journal(path) as j:
                    c=Coordinator(j,api);c.prepare({**PLAN,'symbol':symbol})
                    self.assertEqual(p.reconcile(c)[0],'PROTECTED')
                    entry_id=c.cid('entry')
                with Journal(path) as j:
                    c=Coordinator(j,api)
                    self.assertEqual(p.reconcile(c)[0],'PROTECTED')
                    self.assertEqual(c.cid('entry'),entry_id)
                    self.assertEqual(p.reconcile(c,True)[0],'EXIT_RECONCILIATION')
                    phase,evidence=p.reconcile(c)
                    self.assertEqual(phase,'CLOSED')
                    net,_=p.realized(api,*evidence)
                    self.assertEqual(net,dec('-.012'))
                entries=[kw for method,path,kw in api.requests if method=='POST' and path==ORDER and not kw.get('reduceOnly')]
                self.assertEqual(len(entries),1)
                self.assertEqual(entries[0]['symbol'],symbol)
                self.assertTrue(all(kw.get('symbol',symbol)==symbol for _,_,kw in api.requests))

    def test_any_foreign_position_detected(self):
        api=SimpleNamespace(call=lambda *a,**kw:[{'symbol':'SOLUSDT','positionAmt':'1'}])
        self.assertEqual(p.position(api)['symbol'],'SOLUSDT')

    def test_multimarket_entry_expiry(self):
        with TemporaryDirectory() as d:
            api=SymbolExchange('SOLUSDT')
            with Journal(Path(d)/'trade.db') as j:
                c=Coordinator(j,api)
                c.prepare({**PLAN,'symbol':'SOLUSDT','purpose':'MULTI_MARKET_TESTNET','signal_close_ms':1})
                phase,reason=p.reconcile(c)  # skipped trade, not a pilot lock
                self.assertEqual(phase,'ABORTED_BEFORE_ENTRY')
                self.assertIn('expired',reason)
                self.assertFalse(any(method=='POST' for method,_,_ in api.requests))

    def test_selection_skips_unsupported_and_unsizeable(self):
        t=(1789602300000//BAR_MS)*BAR_MS+50000;stamp=t//BAR_MS*BAR_MS-1
        rows=[dict(symbol=sym,market='USD-M',quote='USDT',contract='PERPETUAL',
                   decision='CANDIDATE',candle_close_ms=stamp,observed_ms=t,score=score,volume=100000000)
              for sym,score in [('MISSINGUSDT',3),('BIGUSDT',2),('SOLUSDT',1)]]
        book=SimpleNamespace(s={'equity':'49.8','day_start':'50','high_water':'50','seen_ms':None},save=lambda *a:None)
        demo=[dict(symbol=s,status='TRADING',contractType='PERPETUAL',quoteAsset='USDT',marginAsset='USDT') for s in ('BIGUSDT','SOLUSDT')]
        api=SimpleNamespace(call=lambda method,path,**kw:{'symbols':demo} if path.endswith('exchangeInfo') else {'price':'100'})
        def public_get(path,**kw):
            if path.endswith('exchangeInfo'):return {'symbols':demo}
            if path.endswith('klines'):return []
            return [{'symbol':s} for s in ('BIGUSDT','SOLUSDT')]
        public=SimpleNamespace(get=public_get)
        def signal(sym,bars):return Signal(sym,1,stamp,dec(100),dec(99),dec(103),dec(2))
        plan=SimpleNamespace(entry=dec(100),qty=dec('.1'),target=dec(103),stop=dec(99),notional=dec(10),risk=dec('.15'))
        def sizing(sig,*a,**kw):
            if sig.symbol=='BIGUSDT':raise ValueError('BELOW_EXCHANGE_MINIMUM')
            return plan
        with patch.object(p,'now_ms',return_value=t),patch('gptsalov.market_scanner.read',return_value={'status':'current','started_ms':t-10000,'rows':rows}),patch('gptsalov.market_scanner.classify',return_value={'status':'pending'}),patch.object(p,'closed_bars',return_value=[SimpleNamespace(close_ms=stamp)]),patch.object(p,'strategy',side_effect=signal),patch('gptsalov.multiagent_gate.evaluate',return_value=(True,{})),patch.object(p.Rules,'from_exchange',return_value=None),patch.object(p,'adaptive_stop',side_effect=lambda sig,bars:(sig,{'version':'atr-structure-v1'})),patch.object(p,'buffered_size',side_effect=sizing),patch.object(p,'reward_risk',return_value={'net_rr':'2'}):
            result=(p.multi_candidate(api,public,book) or [None])[0]
        self.assertEqual(result['symbol'],'SOLUSDT')
        self.assertEqual(book.s['last_selection']['rejected'],{'MISSINGUSDT':'NOT_ON_USDT_TESTNET','BIGUSDT':'BELOW_EXCHANGE_MINIMUM'})

    def test_selection_returns_ranked_three_slot_batch(self):
        t=(1789602300000//BAR_MS)*BAR_MS+50000;stamp=t//BAR_MS*BAR_MS-1
        rows=[dict(symbol=sym,market='USD-M',quote='USDT',contract='PERPETUAL',
                   decision='CANDIDATE',candle_close_ms=stamp,observed_ms=t,score=score,volume=100000000)
              for sym,score in [('SOLUSDT',3),('BNBUSDT',2),('XRPUSDT',1)]]
        book=SimpleNamespace(s={'equity':'50','day_start':'50','high_water':'50','seen_ms':None,
                                'policy':{'risk_model':False}},save=lambda *a:None)
        demo=[dict(symbol=s,status='TRADING',contractType='PERPETUAL',quoteAsset='USDT',marginAsset='USDT') for s in ('SOLUSDT','BNBUSDT','XRPUSDT')]
        api=SimpleNamespace(call=lambda method,path,**kw:{'symbols':demo} if path.endswith('exchangeInfo') else {'price':'100'})
        def public_get(path,**kw):
            if path.endswith('exchangeInfo'):return {'symbols':demo}
            if path.endswith('klines'):return []
            return [{'symbol':s} for s in ('SOLUSDT','BNBUSDT','XRPUSDT')]
        public=SimpleNamespace(get=public_get)
        def signal(sym,bars): return Signal(sym,1,stamp,dec(100),dec(99),dec(103),dec(2))
        plan=SimpleNamespace(entry=dec(100),qty=dec('.1'),target=dec(103),stop=dec(99),notional=dec(10),risk=dec('.05'))
        with patch.object(p,'now_ms',return_value=t),patch('gptsalov.market_scanner.read',return_value={'status':'current','started_ms':t-10000,'rows':rows}),patch('gptsalov.market_scanner.classify',return_value={'status':'pending'}),patch.object(p,'closed_bars',return_value=[SimpleNamespace(close_ms=stamp)]),patch.object(p,'strategy',side_effect=signal),patch('gptsalov.multiagent_gate.evaluate',return_value=(True,{})),patch.object(p.Rules,'from_exchange',return_value=None),patch.object(p,'adaptive_stop',side_effect=lambda sig,bars:(sig,{'version':'atr-structure-v1'})),patch.object(p,'buffered_size',return_value=plan),patch.object(p,'reward_risk',return_value={'net_rr':'2'}):
            result=p.multi_candidate(api,public,book,limit=3)
        self.assertEqual([x['symbol'] for x in result],['SOLUSDT','BNBUSDT','XRPUSDT'])

    def test_incomplete_scan_never_consumes_candle(self):
        t=1789602350000
        book=SimpleNamespace(s={'seen_ms':None},save=lambda *a:None)
        with patch.object(p,'now_ms',return_value=t),patch('gptsalov.market_scanner.read',return_value={'status':'running'}):
            self.assertEqual(p.multi_candidate(None,None,book),[])
        self.assertIsNone(book.s['seen_ms'])
