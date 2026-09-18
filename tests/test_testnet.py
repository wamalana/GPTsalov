import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
from gptsalov.testnet import Coordinator, Journal, DemoClient, Uncertain, Rejected, ORDER, ALGO

PLAN=dict(environment='testnet',symbol='BTCUSDT',side='BUY',
          quantity='0.01',reference='100',stop='99',target='102')

class Exchange:
    identity='test-account'
    def __init__(self):
        self.calls=[]; self.orders={}; self.algos={}; self.amount='0'
        self.entry_status='FILLED'; self.entry_timeout=False
        self.stop_rejected=False; self.stop_timeout=False; self.cancel_timeout=False
        self.flat=False; self.hedge=False; self.leverage=2; self.margin='ISOLATED'
    def call(self, method,path,**p):
        self.calls.append((method,path,p))
        if path.endswith('ticker/price'): return {'price':'100'}
        if path.endswith('positionSide/dual'): return {'dualSidePosition':self.hedge}
        if path.endswith('symbolConfig'):
            return [dict(symbol='BTCUSDT',marginType=self.margin,leverage=self.leverage)]
        if path.endswith('positionRisk'):
            return [dict(symbol='BTCUSDT',positionAmt='0' if self.flat else self.amount)]
        if path.endswith('openOrders') or path.endswith('openAlgoOrders'): return []
        if path.endswith('exchangeInfo'):
            return {'symbols':[dict(symbol='BTCUSDT',status='TRADING',contractType='PERPETUAL',quoteAsset='USDT',
              filters=[dict(filterType='PRICE_FILTER',tickSize='0.01',minPrice='0.01',maxPrice='1000000'),
              dict(filterType='LOT_SIZE',stepSize='0.001',minQty='0.001',maxQty='1000'),
              dict(filterType='MIN_NOTIONAL',notional='1')])]}
        if path==ORDER:
            if method=='POST':
                cid=p['newClientOrderId']
                self.orders[cid]=dict(executedQty=p['quantity'],status=self.entry_status,avgPrice='100')
                self.amount='0' if p.get('reduceOnly') else p['quantity']
                if self.entry_timeout: raise Uncertain('timeout')
                return self.orders[cid]
            if method=='GET':
                if p['origClientOrderId'] not in self.orders: raise Rejected(-2013)
                return self.orders[p['origClientOrderId']]
            if method=='DELETE':
                if self.cancel_timeout: raise Uncertain('cancel timeout')
                self.orders[p['origClientOrderId']]['status']='CANCELED'
                return {}
        if path==ALGO:
            cid=p['clientAlgoId']
            if method=='POST':
                if self.stop_rejected and p['type']=='STOP_MARKET': raise Rejected(-2021)
                self.algos[cid]={**p,'algoStatus':'NEW'}
                if self.stop_timeout and p['type']=='STOP_MARKET': raise Uncertain('timeout')
                return self.algos[cid]
            if method=='GET':
                if cid not in self.algos: raise Rejected(-2013)
                return self.algos[cid]
            if method=='DELETE':
                self.algos.pop(cid,None)
                return {}
        raise AssertionError((method,path,p))

class TestnetTests(unittest.TestCase):
    def setup_coordinator(self,d,e):
        j=Journal(Path(d)/'demo.db'); self.addCleanup(j.close)
        c=Coordinator(j,e); c.prepare(PLAN)
        return c
    def posts(self,e,path):
        return [x for x in e.calls if x[:2]==('POST',path)]
    def test_entry_stop_target_and_repeated_advance(self):
        with tempfile.TemporaryDirectory() as d:
            e=Exchange(); c=self.setup_coordinator(d,e)
            self.assertEqual(c.advance(),'PROTECTED')
            self.assertEqual(c.advance(),'PROTECTED')
            self.assertEqual(len(self.posts(e,ORDER)),1)
            self.assertEqual(len(self.posts(e,ALGO)),2)
            e.flat=True
            self.assertEqual(c.advance(),'DONE_FLAT')
            self.assertEqual(e.algos,{})
    def test_restart_after_accepted_entry_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            e=Exchange(); e.entry_timeout=True
            with Journal(Path(d)/'demo.db') as j:
                c=Coordinator(j,e); c.prepare(PLAN)
                self.assertEqual(c.advance(),'PROTECTED')
            with Journal(Path(d)/'demo.db') as j:
                c=Coordinator(j,e)
                self.assertEqual(c.advance(),'PROTECTED')
            self.assertEqual(len(self.posts(e,ORDER)),1)
    def test_crash_before_request_never_resubmits(self):
        with tempfile.TemporaryDirectory() as d:
            e=Exchange(); c=self.setup_coordinator(d,e)
            c.j.put('entry',{'phase':'ATTEMPTED'})
            self.assertEqual(c.advance(),'UNCERTAIN_ENTRY_NO_RESUBMIT')
            self.assertEqual(self.posts(e,ORDER),[])
    def test_stop_timeout_queries_existing_stop(self):
        with tempfile.TemporaryDirectory() as d:
            e=Exchange(); e.stop_timeout=True; c=self.setup_coordinator(d,e)
            self.assertEqual(c.advance(),'PROTECTED')
            self.assertEqual(len(self.posts(e,ORDER)),1)
    def test_stop_rejection_reduce_only_exit_no_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            e=Exchange(); e.stop_rejected=True; c=self.setup_coordinator(d,e)
            self.assertEqual(c.advance(),'REVIEW_EXIT_RECONCILIATION')
            self.assertEqual(self.posts(e,ORDER)[-1][2]['reduceOnly'],'true')
            self.assertEqual(c.advance(),'DONE_FLAT')
            self.assertEqual(len(self.posts(e,ORDER)),2)
    def test_partial_fill_protected_before_cancel(self):
        with tempfile.TemporaryDirectory() as d:
            e=Exchange(); e.entry_status='PARTIALLY_FILLED'; c=self.setup_coordinator(d,e)
            self.assertEqual(c.advance(),'PROTECTED')
            stop=next(i for i,x in enumerate(e.calls) if x[:2]==('POST',ALGO))
            cancel=next(i for i,x in enumerate(e.calls) if x[:2]==('DELETE',ORDER))
            self.assertLess(stop,cancel)
    def test_partial_cancel_uncertain_no_exit(self):
        with tempfile.TemporaryDirectory() as d:
            e=Exchange(); e.entry_status='PARTIALLY_FILLED'; e.cancel_timeout=True
            c=self.setup_coordinator(d,e)
            self.assertEqual(c.advance(),'REVIEW_PARTIAL_ENTRY')
            self.assertEqual(len(self.posts(e,ORDER)),1)
    def test_preflight_refuses_wrong_account_settings(self):
        for field,value in [('hedge',True),('leverage',3),('margin','CROSSED')]:
            with tempfile.TemporaryDirectory() as d:
                e=Exchange(); setattr(e,field,value); c=self.setup_coordinator(d,e)
                with self.assertRaises(ValueError): c.advance()
                self.assertEqual(self.posts(e,ORDER),[])
    def test_plan_expiry_account_binding_and_risk(self):
        with tempfile.TemporaryDirectory() as d:
            e=Exchange(); c=self.setup_coordinator(d,e)
            c.j.put('prepared',{'at_ms':0})
            with self.assertRaises(ValueError): c.advance()
            e.identity='different'
            with self.assertRaises(ValueError): c.plan()
        with tempfile.TemporaryDirectory() as d, Journal(Path(d)/'demo.db') as j:
            with self.assertRaises(ValueError):
                Coordinator(j,Exchange()).prepare({**PLAN,'quantity':'2'})
    def test_existing_paper_journal_refused(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'paper.db'
            c=sqlite3.connect(p); c.execute('CREATE TABLE state(data TEXT)'); c.close()
            with self.assertRaises(ValueError): Journal(p)
    def test_fixed_host_signature_and_no_retry(self):
        c=DemoClient('test-key','test-secret')
        with patch.object(c.opener,'open',side_effect=URLError('sensitive signed url')) as op:
            with self.assertRaises(Uncertain) as err: c.call('POST',ORDER,symbol='BTCUSDT')
            self.assertNotIn('sensitive',str(err.exception))
            self.assertEqual(op.call_count,1)
            req=op.call_args.args[0]
            self.assertEqual(req.full_url,'https://demo-fapi.binance.com/fapi/v1/order')
            self.assertIn(b'signature=',req.data)
            self.assertNotIn(b'test-secret',req.data)
        with self.assertRaises(ValueError): c.call('POST','https://fapi.binance.com/fapi/v1/order')

    def test_actual_fill_drift_flattens(self):
        with tempfile.TemporaryDirectory() as d:
            e=Exchange(); c=self.setup_coordinator(d,e)
            self.assertEqual(c.advance(),'PROTECTED')
            e.orders[c.cid('entry')]['avgPrice']='101'
            self.assertEqual(c.advance(),'REVIEW_EXIT_RECONCILIATION')
            self.assertEqual(self.posts(e,ORDER)[-1][2]['reduceOnly'],'true')

    def test_ambiguous_exit_never_resends(self):
        with tempfile.TemporaryDirectory() as d:
            e=Exchange(); c=self.setup_coordinator(d,e)
            self.assertEqual(c.advance(),'PROTECTED')
            e.algos[c.cid('stop')]['algoStatus']='CANCELED'
            e.entry_timeout=True
            self.assertEqual(c.advance(),'REVIEW_EXIT_RECONCILIATION')
            self.assertEqual(c.advance(),'DONE_FLAT')
            self.assertEqual(len(self.posts(e,ORDER)),2)

    def test_second_journal_writer_refused(self):
        with tempfile.TemporaryDirectory() as d:
            e=Exchange(); c=self.setup_coordinator(d,e)
            with self.assertRaises(BlockingIOError): Journal(Path(d)/'demo.db')
