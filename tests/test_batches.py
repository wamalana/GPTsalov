import unittest
from tempfile import TemporaryDirectory
from gptsalov.testnet_pilot import Book,settle,risk_check,POLICY
from gptsalov.core import dec
class BatchTests(unittest.TestCase):
 def test_new_batch_keeps_lifetime_count(self):
  with TemporaryDirectory() as d:
   b=Book(d,'test')
   try:
    b.s.update(closed_trades=3,batch_start_closed_trades=3,batch_started_ms=b.s['created_ms'],balance='49.70849554',equity='49.70849554')
    for _ in range(POLICY['max_trades']-1):settle(b,dec('.01'),{})
    self.assertIsNone(b.s['lock'])
    settle(b,dec('.01'),{})
    self.assertEqual(b.s['closed_trades'],3+POLICY['max_trades'])
    self.assertEqual(b.s['lock'],'PILOT_BATCH_COMPLETE')
   finally:b.close()
 def test_batch_clock_and_daily_risk(self):
  with TemporaryDirectory() as d:
   b=Book(d,'test')
   try:
    t=b.s['created_ms'];b.s['created_ms']=t-2*86400000;b.s['batch_started_ms']=t
    risk_check(b.s,t+1000);self.assertIsNone(b.s['lock'])
    b.s['equity']='48'
    risk_check(b.s,t+2000);self.assertEqual(b.s['lock'],'DAILY_LOSS')
   finally:b.close()

from tests.conservative_limits import setUpModule, tearDownModule  # noqa: E402,F401
