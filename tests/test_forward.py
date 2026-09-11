from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import sqlite3
import unittest
from gptsalov.core import Config, D
from gptsalov.forward import FilterEngine, run, status
from gptsalov.market import DemoFeed
from gptsalov.paper import Store, PaperEngine

class ForwardTests(unittest.TestCase):
    def test_baseline_parity_and_entry_filter(self):
        with tempfile.TemporaryDirectory() as d:
            cfg=Config()
            with Store(d+'/a',cfg,'synthetic-demo') as a, Store(d+'/b',cfg,'synthetic-demo') as b, Store(d+'/c',cfg,'synthetic-demo') as c:
                ea,eb,ec=PaperEngine(cfg,a),FilterEngine(cfg,b,None),FilterEngine(cfg,c,'999')
                feed=DemoFeed()
                for _ in range(180):
                    snap=feed.snapshot()
                    self.assertEqual(ea.step(snap),eb.step(snap))
                    self.assertEqual(a.state,b.state)
                    ec.step(snap)
                self.assertGreater(a.state['closed_trades'],0)
                self.assertEqual(c.state['closed_trades'],0)
                self.assertEqual(c.state['balance'],'100')
                self.assertIn('NET_RR_BELOW_THRESHOLD',str(c.db.execute('SELECT data FROM events').fetchall()))

    def test_restart_parity_identity_and_lock_preservation(self):
        with tempfile.TemporaryDirectory() as d, redirect_stdout(StringIO()):
            run(d+'/a',Config(),'synthetic-demo',90)
            run(d+'/a',Config(),'synthetic-demo',90)
            run(d+'/b',Config(),'synthetic-demo',180)
            for n in ('baseline','rr1','rr1_5','rr2'):
                def state(folder):
                    with sqlite3.connect(d+'/'+folder+'/'+n+'.db') as c:
                        return c.execute('SELECT data FROM state').fetchone()[0]
                self.assertEqual(state('a'),state('b'))
            self.assertEqual(len(status(d+'/a')['portfolios']),4)
            with self.assertRaises(ValueError):
                run(d+'/a',Config(initial_equity=D('101')),'synthetic-demo',1)

    def test_nonempty_directory_refused(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d+'/main.db').touch()
            with self.assertRaises(ValueError): run(d,Config(),'synthetic-demo',1)
