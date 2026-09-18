from pathlib import Path
import tempfile
import unittest
from gptsalov.core import Rules,D
from gptsalov.testnet import Journal,Coordinator,ORDER
from gptsalov.testnet_smoke import small_plan,flatten
from test_testnet import Exchange,PLAN

class SmokeTests(unittest.TestCase):
    def test_minimum_size_and_cap(self):
        r=Rules('ETHUSDT',D('.01'),D('.001'),D('.001'),D(100),D(20),D(100000),D('.01'),D(100000))
        p=small_plan('2500',r)
        self.assertEqual(p['quantity'],'0.009')
        with tempfile.TemporaryDirectory() as d,Journal(Path(d)/'test.db') as j:
            Coordinator(j,Exchange()).prepare(p)
        with self.assertRaises(ValueError): small_plan('100000',r)

    def test_exit_is_reduce_only_and_not_repeated(self):
        with tempfile.TemporaryDirectory() as d,Journal(Path(d)/'test.db') as j:
            api=Exchange(); c=Coordinator(j,api); c.prepare(PLAN)
            self.assertEqual(c.advance(),'PROTECTED')
            flatten(c); flatten(c)
            orders=[x for x in api.calls if x[:2]==('POST',ORDER)]
            self.assertEqual(len(orders),2)
            self.assertEqual(orders[-1][2]['reduceOnly'],'true')

    def test_no_exit_if_entry_unknown(self):
        with tempfile.TemporaryDirectory() as d,Journal(Path(d)/'test.db') as j:
            api=Exchange(); c=Coordinator(j,api); c.prepare(PLAN)
            self.assertEqual(flatten(c),'REVIEW_ENTRY_UNKNOWN')
            self.assertEqual([x for x in api.calls if x[:2]==('POST',ORDER)],[])

    def test_settings_confirmation_waits_without_duplicate_writes(self):
        from unittest.mock import patch
        from gptsalov.testnet_smoke import setup
        api=Exchange()
        reads=iter([
            [dict(symbol='BTCUSDT',marginType='CROSSED',leverage=5)],
            [dict(symbol='BTCUSDT',marginType='CROSSED',leverage=5)],
            [dict(symbol='BTCUSDT',marginType='ISOLATED',leverage=2)]])
        original=api.call
        writes=[]
        def call(method,path,**params):
            if path.endswith('symbolConfig'): return next(reads)
            if method=='POST':
                writes.append(path)
                return {}
            return original(method,path,**params)
        api.call=call
        with patch('gptsalov.testnet_smoke.time.sleep'):
            setup(api,'BTCUSDT')
        self.assertEqual(writes,['/fapi/v1/marginType','/fapi/v1/leverage'])
