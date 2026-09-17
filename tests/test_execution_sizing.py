from dataclasses import replace
import unittest
from gptsalov.core import Config,Rules,Signal,dec
from gptsalov.execution_sizing import buffered_size,modeled_fill_risk,ENTRY_DRIFT

class SizingTests(unittest.TestCase):
    def test_all_permitted_prices_respect_budget_and_notional_both_sides(self):
        cfg=Config(initial_equity=dec(50),max_notional_fraction=dec('.5'))
        rules=Rules('XUSDT',dec('.001'),dec('.001'),dec('.001'),dec(100000),dec(5),dec(1000000),dec('.001'),dec(100000))
        for side in (-1,1):
            sig=Signal('XUSDT',side,0,dec(100),dec(100)-side*dec(2),dec(100)+side*dec(5),dec(1))
            plan=buffered_size(sig,dec(100),dec('49.70849554'),rules,cfg)
            self.assertEqual(plan.qty%rules.step,0)
            for i in range(101):
                fill=dec(100)*(1-ENTRY_DRIFT+2*ENTRY_DRIFT*dec(i)/100)
                self.assertLessEqual(modeled_fill_risk(plan.qty,fill,plan.stop),plan.budget)
                self.assertLessEqual(fill*plan.qty,25)
                self.assertGreaterEqual(fill*plan.qty,rules.min_notional)
            self.assertLess(plan.budget,dec('.25'))

    def test_minimum_does_not_round_quantity_up(self):
        cfg=Config(initial_equity=dec(50),max_notional_fraction=dec('.5'))
        rules=Rules('XUSDT',dec('.01'),dec('.001'),dec('.001'),dec(100),dec(15),dec(100000),dec('.01'),dec(100000))
        sig=Signal('XUSDT',1,0,dec(100),dec(99),dec(104),dec(1))
        with self.assertRaisesRegex(ValueError,'MINIMUM_WITH_BUFFER'):
            buffered_size(sig,dec(100),dec(50),rules,cfg)

    def test_envelope_cannot_cross_stop(self):
        cfg=Config(initial_equity=dec(50),max_notional_fraction=dec('.5'))
        rules=Rules('XUSDT',dec('.01'),dec('.001'),dec('.001'),dec(100),dec(5),dec(100000),dec('.01'),dec(100000))
        sig=Signal('XUSDT',1,0,dec(100),dec('99.8'),dec(103),dec(1))
        with self.assertRaisesRegex(ValueError,'ENVELOPE'):
            buffered_size(sig,dec(100),dec(50),rules,cfg)
