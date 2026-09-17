import unittest
from types import SimpleNamespace
from gptsalov.core import Signal,Rules,Config,dec
from gptsalov.adaptive_risk import adaptive_stop,risk_allowance
from gptsalov.execution_sizing import buffered_size,modeled_fill_risk

class AdaptiveTests(unittest.TestCase):
 def test_stop_structure_both_sides(self):
  bars=[SimpleNamespace(high=dec(101),low=dec(99),close=dec(100)) for _ in range(20)]
  for side in (-1,1):
   sig=Signal('XUSDT',side,0,dec(100),dec(100)-side*3,dec(100)+side*6,dec(1))
   new,e=adaptive_stop(sig,bars)
   self.assertEqual(abs(new.reference-new.stop),4)
   self.assertEqual(abs(new.target-new.reference),8)
   self.assertEqual(e['version'],'atr-structure-v1')
 def test_extreme_structure_rejected(self):
  bars=[SimpleNamespace(high=dec(110),low=dec(90),close=dec(100)) for _ in range(20)]
  sig=Signal('XUSDT',1,0,dec(100),dec(97),dec(106),dec(1))
  with self.assertRaisesRegex(ValueError,'TOO_WIDE'): adaptive_stop(sig,bars)
 def state(self,results):
  return dict(equity='100',day_start='100',high_water='100',adaptive_results=results)
 def test_no_promotion_without_evidence(self):
  cap,_=risk_allowance(self.state([]));self.assertEqual(cap,dec('.25'))
 def test_winrate_alone_is_not_enough(self):
  old=[dict(net='1' if i<10 else '-1',risk='1') for i in range(30)]
  new=[dict(net='.1' if i<20 else '-2',risk='1') for i in range(30)]
  cap,_=risk_allowance(self.state(old+new));self.assertEqual(cap,dec('.25'))
 def test_profitable_improvement_and_daily_budget(self):
  old=[dict(net='1' if i<10 else '-1',risk='1') for i in range(30)]
  new=[dict(net='2' if i<20 else '-1',risk='1') for i in range(30)]
  s=self.state(old+new);cap,_=risk_allowance(s);self.assertEqual(cap,dec('.5'))
  s['equity']='98.1';cap,_=risk_allowance(s);self.assertEqual(cap,dec('.1'))
  s['equity']='97';cap,_=risk_allowance(s);self.assertEqual(cap,0)
 def test_sizing_cap_and_notional_all_fills(self):
  cfg=Config(initial_equity=dec(50),max_notional_fraction=dec('.5'))
  rule=Rules('XUSDT',dec('.001'),dec('.001'),dec('.001'),dec(100000),dec(1),dec(1000000),dec('.001'),dec(100000))
  for cap in ('.25','.5','1','2'):
   for side in (-1,1):
    sig=Signal('XUSDT',side,0,dec(100),dec(100)-side*4,dec(100)+side*8,dec(1))
    p=buffered_size(sig,dec(100),dec(50),rule,cfg,risk_cap=dec(cap))
    for i in range(101):
     price=dec('99.5')+dec(i)/100
     self.assertLessEqual(modeled_fill_risk(p.qty,price,p.stop),dec(cap))
     self.assertLessEqual(p.qty*price,25)
  with self.assertRaisesRegex(ValueError,'INVALID_TESTNET'):
   buffered_size(sig,dec(100),dec(50),rule,cfg,risk_cap=dec(6))
