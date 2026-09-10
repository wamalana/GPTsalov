from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from io import StringIO
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from gptsalov.core import Config, D
from gptsalov.market import DemoFeed
from gptsalov.paper import Store, PaperEngine, report
from gptsalov.research import ResearchRecorder, reward_risk, research_report
from gptsalov.__main__ import main
from test_paper import snapshot


class ResearchTests(unittest.TestCase):
    def test_long_and_short_costs_and_inclusive_threshold(self):
        cfg = replace(Config(), fee_bps=D(0), slippage_bps=D(0), funding_reserve_bps=D(0))
        for side, target in [(1,'102'), (-1,'98')]:
            p = dict(entry='100', qty='1', target=target, side=side,
                     entry_fee='0', funding_reserve='0', modeled_risk='1')
            r = reward_risk(p,cfg)
            self.assertEqual(D(r['net_reward']),D(2))
            self.assertTrue(r['passes']['2'])
            p.update(entry_fee='0.1', funding_reserve='0.1')
            r = reward_risk(p,replace(cfg,fee_bps=D(10),slippage_bps=D(10)))
            self.assertLess(D(r['net_rr']),D(2))
            self.assertFalse(r['passes']['2'])

    def test_matches_known_bnb_target_close(self):
        p = dict(entry='745.3863170', target='741.900', qty='0.07', side=-1,
                 entry_fee='0.026088521095',funding_reserve='0.05217704219',modeled_risk='0.4656491716450')
        r = reward_risk(p,Config())
        self.assertEqual(D(r['net_reward']),D('0.124222436765'))
        self.assertFalse(any(r['passes'].values()))

    def test_observation_preserves_entire_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg=Config(); root=Path(temp)
            with Store(str(root/'a.db'),cfg,'synthetic-demo') as a, Store(str(root/'b.db'),cfg,'synthetic-demo') as b:
                ea,eb=PaperEngine(cfg,a),PaperEngine(cfg,b)
                recorder=ResearchRecorder(root/'research.db',root/'b.db',cfg)
                feed=DemoFeed()
                for _ in range(180):
                    snap=feed.snapshot(ea.required_symbols())
                    av=ea.step(snap); before=b.state; bv=eb.step(snap)
                    self.assertIsNone(recorder.observe(snap,before,b.state,bv))
                    self.assertEqual(a.state,b.state)
                    self.assertEqual(av,bv)
                ar=a.db.execute('SELECT * FROM events').fetchall()
                self.assertEqual(ar,b.db.execute('SELECT * FROM events').fetchall())
                r=research_report(root/'research.db')
                self.assertGreater(r['matched_closed_trades'],0)
                for c in r['comparisons']:
                    self.assertEqual(c['passed_closed_trades']+c['blocked_closed_trades'],r['matched_closed_trades'])
                with sqlite3.connect(root/'research.db') as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM outcomes').fetchone()[0],b.state['closed_trades'])

    def test_restart_deduplicates_and_skips_heartbeat(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg=Config(); root=Path(temp)
            with Store(str(root/'paper.db'),cfg,'synthetic-demo') as store:
                engine=PaperEngine(cfg,store); snap=snapshot(0,signals=True)
                before=store.state; ev=engine.step(snap)
                for _ in range(2):
                    recorder=ResearchRecorder(root/'r.db',root/'paper.db',cfg)
                    self.assertIsNone(recorder.observe(snap,before,store.state,ev))
                self.assertIsNone(recorder.observe(snap,store.state,store.state,[]))
                with sqlite3.connect(root/'r.db') as db:
                    for table in ('bars','cycles','candidates'):
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],1)

    def test_research_failure_cannot_stop_cli(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(StringIO()), redirect_stderr(StringIO()) as err:
            root=Path(temp)
            with patch.object(ResearchRecorder,'_record',side_effect=sqlite3.OperationalError('disk full')) as write:
                code=main(['run','--source','demo','--cycles','4','--db',str(root/'paper.db'),
                           '--research-db',str(root/'r.db')])
            self.assertEqual(code,0)
            self.assertEqual(write.call_count,1)
            self.assertIn('RESEARCH_DISABLED',err.getvalue())
            self.assertIsNotNone(report(root/'paper.db')['as_of_ms'])

    def test_refuses_ledger_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'p.db'; p.touch(); alias=Path(temp)/'alias'; alias.symlink_to(p)
            with self.assertRaises(ValueError): ResearchRecorder(p,p,Config())
            with self.assertRaises(ValueError): ResearchRecorder(alias,p,Config())

    def test_report_close_times_and_four_hour_window(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'p.db'
            with Store(str(p),Config(),'synthetic-demo') as store:
                engine=PaperEngine(Config(),store)
                for snap in [snapshot(0,signals=True),snapshot(1),snapshot(2),snapshot(3,high='10.3')]:
                    engine.step(snap)
                stamp=snap.latest_close
                r=report(p,stamp+1)
                self.assertEqual(r['recent_trades'][0]['close_ms'],stamp)
                self.assertEqual(r['closed_trades_last_4h'],1)
                self.assertEqual(r['closed_trade_net_pnl_last_4h'],r['recent_trades'][0]['net_pnl'])
                self.assertEqual(report(p,stamp+14400001)['closed_trades_last_4h'],0)
                self.assertEqual(report(p,stamp-1)['closed_trades_last_4h'],0)


if __name__=='__main__': unittest.main()
