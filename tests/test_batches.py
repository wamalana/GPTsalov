import unittest
from tempfile import TemporaryDirectory
from gptsalov.testnet_pilot import Book,settle,risk_check,POLICY
from gptsalov.core import dec
from gptsalov import testnet_limits as L
class BatchTests(unittest.TestCase):
 def test_new_batch_keeps_lifetime_count(self):
  with TemporaryDirectory() as d:
   b=Book(d,'test')
   try:
    lifetime=POLICY['max_trades']
    b.s.update(closed_trades=lifetime,batch_start_closed_trades=lifetime,batch_started_ms=b.s['created_ms'],balance='49.70849554',equity='49.70849554')
    for _ in range(POLICY['max_trades']-1):settle(b,dec('.01'),{})
    self.assertIsNone(b.s['lock'])
    settle(b,dec('.01'),{})
    self.assertEqual(b.s['closed_trades'],lifetime+POLICY['max_trades'])
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


class BatchCapsAndSafety(unittest.TestCase):
 """2026-09-21: batch caps widened for continuous data collection. The caps are
 review checkpoints, not risk controls: the risk locks below must not move."""
 def test_widened_batch_caps(self):
  self.assertEqual(POLICY['max_trades'],20)
  self.assertEqual(POLICY['duration_ms'],7*86400000)
  self.assertEqual(POLICY['hold_ms'],4*3600000)
  self.assertEqual(POLICY['max_positions'],3)

 def test_loss_streak_still_locks_after_three_losses_inside_a_long_batch(self):
  with TemporaryDirectory() as d:
   b=Book(d,'test')
   try:
    for _ in range(3):settle(b,dec('-.5'),{})
    self.assertEqual(b.s['closed_trades'],3)  # far below the batch cap
    self.assertEqual(b.s['lock'],'LOSS_STREAK_REVIEW')
   finally:b.close()

 def test_daily_loss_still_locks_inside_a_long_batch(self):
  with TemporaryDirectory() as d:
   b=Book(d,'test')
   try:
    t=b.s['created_ms']
    b.s['equity']=str(dec('50')*(1-L.DAILY_LOSS));b.s['day_start']='50'
    risk_check(b.s,t+1000)
    self.assertEqual(b.s['lock'],'DAILY_LOSS')
   finally:b.close()
