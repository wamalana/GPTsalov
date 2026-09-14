from contextlib import redirect_stdout, closing
from dataclasses import replace
from io import StringIO
from pathlib import Path
import tempfile
import sqlite3
import unittest
from gptsalov.core import Config, D, Signal, Plan
from gptsalov.forward import FilterEngine,run,status
from gptsalov.tuning import TuningEngine,POLICIES,metrics
from gptsalov.paper import Store
from gptsalov.market import DemoFeed

class TuningTests(unittest.TestCase):
    def test_control_exact_parity(self):
        with tempfile.TemporaryDirectory() as d:
            cfg=Config()
            with Store(d+'/a',cfg,'synthetic-demo') as a, Store(d+'/b',cfg,'synthetic-demo') as b:
                ea=FilterEngine(cfg,a,'1'); eb=TuningEngine(cfg,b,POLICIES['rr1_control'])
                feed=DemoFeed()
                for _ in range(180):
                    snap=feed.snapshot()
                    self.assertEqual(ea.step(snap),eb.step(snap))
                    self.assertEqual(a.state,b.state)

    def test_cost_boundary_and_both_sides(self):
        cfg=Config(fee_bps=D('0'),slippage_bps=D('0'),funding_reserve_bps=D('0'))
        for side in (-1,1):
            signal=Signal('X',side,0,D('100'),D(100)-side,D(100)+side*2,D(1))
            plan=Plan(D(100),signal.stop,signal.target,D(1),D(100),D(1),D(1))
            m=metrics(signal,plan,cfg)
            self.assertEqual(D(m['cost_share']),0)
            self.assertEqual(D(m['stop_fraction']),1)
            policy={**POLICIES['rr1_cost25'],'max_cost_share':'0'}
            e=TuningEngine(cfg,None,policy)
            e.validate_entry(signal,plan)
            e=TuningEngine(replace(cfg,fee_bps=D(1)),None,policy)
            with self.assertRaisesRegex(ValueError,'TARGET_COST_SHARE'):
                e.validate_entry(signal,plan)

    def test_geometry_boundary_and_compression_both_sides(self):
        cfg=Config(fee_bps=D(0),slippage_bps=D(0),funding_reserve_bps=D(0))
        for side in (-1,1):
            s=Signal('X',side,0,D(100),D(100)-side*2,D(100)+side*4,D(1))
            # Exactly half original stop distance passes.
            p=Plan(D(100)-side,s.stop,s.target,D(1),D(100),D(1),D(1))
            e=TuningEngine(cfg,None,POLICIES['rr1_geometry50'])
            e.validate_entry(s,p)
            p=replace(p,entry=p.entry-side*D('.01'))
            with self.assertRaisesRegex(ValueError,'STOP_DISTANCE_COMPRESSED'):
                e.validate_entry(s,p)

    def test_resume_and_policy_immutability(self):
        with tempfile.TemporaryDirectory() as d,redirect_stdout(StringIO()):
            opts=dict(variants=POLICIES,engine_class=TuningEngine)
            run(d+'/a',Config(),'synthetic-demo',90,**opts)
            run(d+'/a',Config(),'synthetic-demo',90,**opts)
            run(d+'/b',Config(),'synthetic-demo',180,**opts)
            self.assertEqual(len(status(d+'/a')['portfolios']),3)
            for n in POLICIES:
                def read(root):
                    with closing(sqlite3.connect(Path(d)/root/(n+'.db'))) as db:
                        return db.execute('SELECT data FROM state').fetchone()[0]
                self.assertEqual(read('a'),read('b'))
            changed={**POLICIES,'rr1_cost25':{**POLICIES['rr1_cost25'],'max_cost_share':'0.3'}}
            with self.assertRaisesRegex(ValueError,'identity'):
                run(d+'/a',Config(),'synthetic-demo',1,variants=changed,engine_class=TuningEngine)
