import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from gptsalov.exit_shadow import MINUTE, POLICY, simulate, run_cycle, report


def bar(i,o=100,h=101,l=99,c=100):
    return [i*MINUTE,str(o),str(h),str(l),str(c),'0',(i+1)*MINUTE-1]


PLAN=dict(symbol='BTCUSDT',side='BUY',quantity='1',reference='100',stop='90',target='120')


class ExitSimulationTests(unittest.TestCase):
    def setUp(self):
        self.costs=patch.dict(POLICY,slippage_bps=0,fee_bps=0,funding_reserve_bps=0)
        self.costs.start();self.addCleanup(self.costs.stop)

    def test_trail_activates_only_next_bar(self):
        bars=[bar(1,100,112,99,111)]
        self.assertEqual(simulate(PLAN,bars,'trail_1r_4h')['status'],'OPEN')
        bars.append(bar(2,111,111,101,102))
        trail=simulate(PLAN,bars,'trail_1r_4h')
        self.assertEqual((trail['reason'],trail['exit']),('TRAIL_STOP',102))
        self.assertEqual(simulate(PLAN,bars,'fixed_4h')['status'],'OPEN')

    def test_short_trail(self):
        plan=dict(PLAN,side='SELL',stop='110',target='80')
        result=simulate(plan,[bar(1,100,101,88,89),bar(2,89,99,89,98)],'trail_1r_4h')
        self.assertEqual((result['reason'],result['exit']),('TRAIL_STOP',98))

    def test_ambiguous_bar_stop_first_and_excursion_bounds(self):
        result=simulate(PLAN,[bar(1,100,125,85,110)],'fixed_4h')
        self.assertEqual(result['reason'],'STOP')
        self.assertTrue(result['ambiguous_exit_bar'])
        self.assertEqual((result['mfe_lower_usdt'],result['mfe_upper_usdt']),(0,25))
        self.assertEqual((result['mae_lower_usdt'],result['mae_upper_usdt']),(10,15))

    def test_gap_stop_uses_worse_open(self):
        result=simulate(PLAN,[bar(1),bar(2,85,88,83,86)],'fixed_4h')
        self.assertEqual(result['exit'],85)

    def test_both_time_out_at_240_bars_with_same_entry(self):
        bars=[bar(i) for i in range(1,241)]
        for v in POLICY['variants']:
            result=simulate(PLAN,bars,v)
            self.assertEqual((result['reason'],result['held_minutes'],result['net']),('TIME',240,0))

    def test_adverse_costs_apply_to_both_sides(self):
        with patch.dict(POLICY,slippage_bps=3,fee_bps=5,funding_reserve_bps=15):
            for plan in (PLAN,dict(PLAN,side='SELL',stop='110',target='80')):
                result=simulate(plan,[bar(i) for i in range(1,241)],'fixed_4h')
                self.assertLess(result['gross'],0)
                self.assertLess(result['net'],result['gross'])


class CollectorTests(unittest.TestCase):
    def test_read_only_source_idempotence_missing_data_and_cohorts(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'pilot';source.mkdir();target=Path(d)/'research'/'exit.db'
            p=source/'trade-1.db'
            with sqlite3.connect(p) as db:
                db.execute('create table experiment(id integer,data text)')
                db.execute('create table actions(name text,data text)')
                db.execute('insert into experiment values(1,?)',(json.dumps(dict(PLAN,environment='testnet')),))
                db.executemany('insert into actions values(?,?)',[
                    ('entry',json.dumps({'phase':'ACK','response':{'status':'NEW'}})),
                    ('prepared',json.dumps({'at_ms':1000}))])
            original=p.read_bytes()
            with patch('gptsalov.exit_shadow.time.sleep'):
                run_cycle(source,target,fetch=lambda *a:[],now=300*MINUTE)
                self.assertEqual(len(report(target)['errors']),1)
                run_cycle(source,target,fetch=lambda *a:[bar(i) for i in range(1,241)],now=301*MINUTE)
                r=report(target)
                self.assertEqual(r['groups']['historical']['paired'],1)
                self.assertEqual(r['groups']['forward']['paired'],0)
                run_cycle(source,target,fetch=lambda *a:self.fail('Completed sample refetched'),now=302*MINUTE)
            self.assertEqual(p.read_bytes(),original)
            self.assertEqual(report(target)['total'],1)

    def test_refuses_pilot_output_path(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):run_cycle(d,Path(d)/'exit.db',now=0)


if __name__=='__main__': unittest.main()
