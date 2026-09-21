from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from gptsalov.core import dec
from gptsalov.testnet import Journal,Coordinator,ORDER,ALGO
from gptsalov.testnet_pilot import Book,POLICY,reconcile,settle,risk_check,realized,day
from test_testnet import Exchange,PLAN

class PilotExchange(Exchange):
    def call(self,method,path,**p):
        if path.endswith('positionRisk'):
            return [dict(symbol='ETHUSDT',positionAmt='0' if self.flat else self.amount,
                markPrice='100',entryPrice='100',unRealizedProfit='0')]
        if path.endswith('userTrades'):
            o=next(o for o in self.orders.values() if o['orderId']==p['orderId'])
            return [dict(id=o['orderId'],orderId=o['orderId'],qty=o['executedQty'],price=o['avgPrice'],
                realizedPnl='-0.01' if o['side']=='SELL' else '0',
                commission='0.001',commissionAsset='USDT',time=1700000000000)]
        if path.endswith('income'): return []
        if path.endswith('openAlgoOrders'): return list(self.algos.values())
        if path.endswith('symbolConfig'):
            return [dict(symbol='ETHUSDT',marginType='ISOLATED',leverage=2)]
        if path.endswith('exchangeInfo'):
            result=super().call(method,path,**p)
            result['symbols'][0]['symbol']='ETHUSDT'
            return result
        if path==ORDER and method=='GET' and 'orderId' in p:
            return next(o for o in self.orders.values() if str(o['orderId'])==str(p['orderId']))
        result=super().call(method,path,**p)
        if path==ORDER and method=='POST':
            result.update(orderId=len(self.orders),symbol='ETHUSDT',side=p['side'])
        if path==ALGO and method=='POST':
            result['orderType']=p['type']
        return result

class PilotTests(unittest.TestCase):
    def make(self,d,api):
        j=Journal(Path(d)/'trade.db');self.addCleanup(j.close)
        c=Coordinator(j,api)
        c.prepare({**PLAN,'symbol':'ETHUSDT','signal_id':'pilot'})
        return c
    def test_flat_api_without_exit_evidence_retains_stop(self):
        with TemporaryDirectory() as d:
            api=PilotExchange();c=self.make(d,api)
            phase,_=reconcile(c)
            self.assertEqual(phase,'PROTECTED')
            api.flat=True
            phase,_=reconcile(c)
            self.assertEqual(phase,'WAIT_POSITION_OR_CLOSE_EVIDENCE')
            self.assertEqual(len(api.algos),2)

    def test_forced_close_accounting_and_cleanup(self):
        with TemporaryDirectory() as d:
            api=PilotExchange();c=self.make(d,api)
            self.assertEqual(reconcile(c)[0],'PROTECTED')
            self.assertEqual(reconcile(c,True)[0],'EXIT_RECONCILIATION')
            phase,evidence=reconcile(c)
            self.assertEqual(phase,'CLOSED')
            self.assertEqual(api.algos,{})
            net,details=realized(api,*evidence)
            self.assertEqual(net,dec('-.012'))
            self.assertEqual(details['fees'],'0.002')

    def test_stop_trigger_close_evidence(self):
        with TemporaryDirectory() as d:
            api=PilotExchange();c=self.make(d,api)
            reconcile(c)
            api.orders['triggered']=dict(orderId=99,symbol='ETHUSDT',side='SELL',
                status='FILLED',executedQty='.01',avgPrice='99')
            api.algos[c.cid('stop')].update(algoStatus='FINISHED',actualOrderId='99')
            api.amount='0'
            self.assertEqual(reconcile(c)[0],'CLOSED')

    def test_unknown_entry_does_not_resubmit(self):
        with TemporaryDirectory() as d:
            api=PilotExchange();c=self.make(d,api)
            c.j.put('entry',{'phase':'ATTEMPTED'})
            self.assertEqual(reconcile(c)[0],'UNKNOWN_ENTRY')
            self.assertFalse(any(x[:2]==('POST',ORDER) for x in api.calls))

    def test_three_losses_persist_and_no_daily_unlock(self):
        with TemporaryDirectory() as d:
            b=Book(d,'test')
            for _ in range(3):
                settle(b,dec('-.25'),{})
            self.assertEqual(b.s['lock'],'LOSS_STREAK_REVIEW')
            t=b.s['created_ms']
            b.s['day_start']='100' # Additional daily breach must not erase hard lock.
            risk_check(b.s,t)
            self.assertEqual(b.s['lock'],'LOSS_STREAK_REVIEW')
            b.save();b.close()
            b=Book(d,'test')
            self.assertEqual(b.s['closed_trades'],3)
            self.assertEqual(b.s['balance'],'49.25')
            risk_check(b.s,t+86400000)
            self.assertEqual(b.s['lock'],'LOSS_STREAK_REVIEW')
            b.close()

    def test_batch_limit_and_identity(self):
        with TemporaryDirectory() as d:
            b=Book(d,'test')
            for _ in range(29):settle(b,dec('.1'),{})
            self.assertIsNone(b.s['lock'])
            settle(b,dec('.1'),{})
            self.assertEqual(b.s['lock'],'PILOT_BATCH_COMPLETE')
            b.close()
            with self.assertRaises(ValueError):Book(d,'different')

    def test_thirty_day_limit_preserves_existing_batch_start(self):
        with TemporaryDirectory() as d:
            b=Book(d,'test');self.addCleanup(b.close)
            start=b.s['created_ms']
            risk_check(b.s,start+30*86400000-1)
            self.assertIsNone(b.s['lock'])
            risk_check(b.s,start+30*86400000)
            self.assertEqual(b.s['lock'],'PILOT_TIME_COMPLETE')

    def test_incomplete_fills_refused(self):
        with TemporaryDirectory() as d:
            api=PilotExchange();c=self.make(d,api)
            reconcile(c);reconcile(c,True)
            _,orders=reconcile(c)
            original=api.call
            api.call=lambda method,path,**p: [] if path.endswith('userTrades') else original(method,path,**p)
            with self.assertRaisesRegex(ValueError,'incomplete'):realized(api,*orders)
