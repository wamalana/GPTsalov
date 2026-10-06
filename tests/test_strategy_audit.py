import unittest
from gptsalov.strategy_audit import paired,summary

class AuditTests(unittest.TestCase):
    def test_retest_uses_next_bar_and_original_deadline(self):
        sig=dict(reference=100,stop=90,target=120,side=1)
        bars=[[i*60000,100,101,99,100.5,0,(i+1)*60000-1] for i in range(240)]
        r=paired(sig,bars)
        self.assertEqual(r['retest_entry_offset'],1)
        self.assertEqual(r['retest']['reason'],'ORIGINAL_DEADLINE')
        self.assertEqual(r['fixed']['reason'],'TIME')

    def test_retest_no_touch_no_entry(self):
        sig=dict(reference=100,stop=90,target=120,side=1)
        bars=[[i*60000,105,106,104,105,0,(i+1)*60000-1] for i in range(240)]
        self.assertEqual(paired(sig,bars)['retest']['status'],'NO_ENTRY')

    def test_summary_cost_identity(self):
        s=summary([dict(gross=1,fees=.1,funding_reserve=.2,net=.7)])
        self.assertAlmostEqual(s['gross']-s['fees']-s['funding_reserve'],s['net'])
