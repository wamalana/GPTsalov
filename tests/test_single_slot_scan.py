"""2026-09-20 NEARUSDT: with one free slot, tick called multi_candidate(limit=1),
which returned a bare dict/None. Iterating None raised TypeError, the pilot
locked, and the next tick force-exited a healthy protected trade."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from gptsalov.core import dec
from gptsalov import testnet_pilot as p


class SingleSlotReturnsAList(unittest.TestCase):
    def test_limit_one_returns_a_list_not_a_bare_plan(self):
        book=SimpleNamespace(s={'seen_ms':None,'equity':'50','day_start':'50','high_water':'50'},
                             save=lambda *a: None)
        with patch.object(p,'now_ms',return_value=0):
            for limit in (1,2,3):
                self.assertEqual(p.multi_candidate(None,None,book,limit=limit),[])

    def test_legacy_single_symbol_caller_still_gets_one_plan(self):
        plan={'symbol':'ETHUSDT'}
        book=SimpleNamespace(s={'policy':{'execution_universe':'ALL_USDT_PERPETUAL'}},save=lambda *a: None)
        with patch.object(p,'multi_candidate',return_value=[plan]):
            self.assertEqual(p.candidate(None,None,book),plan)
        with patch.object(p,'multi_candidate',return_value=[]):
            self.assertIsNone(p.candidate(None,None,book))


class ScanFaultKeepsTheOpenTrade(unittest.TestCase):
    def slot_state(self):
        return {'active':[{'file':'trade-1.db','symbol':'ETHUSDT','side':'LONG',
                           'started_ms':1000,'modeled_risk':'1.8','phase':'PROTECTED'}],
                'lock':'SELECTION_REVIEW'}

    def test_selection_review_does_not_force_exit_but_hold_expiry_still_does(self):
        s=self.slot_state(); slot=s['active'][0]
        t=slot['started_ms']+1000
        force=(bool(s['lock']) and s['lock'] not in p.NON_FORCING_LOCKS
               or t-slot['started_ms']>=p.POLICY['hold_ms'])
        self.assertFalse(force)
        expired=slot['started_ms']+p.POLICY['hold_ms']
        force=(bool(s['lock']) and s['lock'] not in p.NON_FORCING_LOCKS
               or expired-slot['started_ms']>=p.POLICY['hold_ms'])
        self.assertTrue(force)

    def test_risk_locks_still_force_exit(self):
        for lock in ('DAILY_LOSS','DRAWDOWN_REVIEW','ORDER_REVIEW','UNOWNED_ACCOUNT_STATE'):
            self.assertNotIn(lock,p.NON_FORCING_LOCKS)

    def test_scan_exception_locks_without_touching_the_position(self):
        events=[]
        s={'active':[{'file':'trade-1.db','symbol':'ETHUSDT','side':'LONG','started_ms':0,
                      'modeled_risk':'1.8','phase':'PROTECTED'}],
           'lock':None,'error':None,'phase':'ACTIVE_1','equity':'50','balance':'50','day':'x',
           'day_start':'50','high_water':'50','loss_streak':0,'closed_trades':1,'created_ms':0,
           'last_scan_ms':0,'policy':{'risk_model':True},'seen_ms':None}
        book=SimpleNamespace(s=s,root=Path('/tmp'),save=lambda e=None: events.append(e))
        api=SimpleNamespace(call=lambda m,path,**kw: [] if 'openOrders' in path or 'openAlgoOrders' in path
                            else [{'symbol':'ETHUSDT','positionAmt':'1','unRealizedProfit':'0',
                                   'markPrice':'100','entryPrice':'100'}])
        with patch.object(p,'now_ms',return_value=10**6), \
             patch.object(p,'active_slots',side_effect=lambda st: st.get('active') or []), \
             patch.object(p,'reconcile',return_value=('PROTECTED',None)), \
             patch.object(p,'Journal'),patch.object(p,'Coordinator') as coordinator, \
             patch.object(p,'risk_allowance',return_value=(dec(2),{})), \
             patch.object(p,'multi_candidate',side_effect=TypeError("'NoneType' object is not iterable")):
            coordinator.return_value.plan.return_value={'symbol':'ETHUSDT'}
            p.tick(book,api,SimpleNamespace())
        self.assertEqual(s['lock'],'SELECTION_REVIEW')
        self.assertEqual(s['error'],'TypeError')
        self.assertEqual(len(s['active']),1)  # the protected trade is untouched
        self.assertIn('SELECTION_FAILED',[e.get('event') for e in events if isinstance(e,dict)])


if __name__=='__main__':
    unittest.main()
