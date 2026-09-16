from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest
from gptsalov.market import DemoFeed
from gptsalov.core import encode
from gptsalov.multiagent_shadow import analyze, read_pilot, record


class ShadowTests(unittest.TestCase):
    def setUp(self):
        snapshot = DemoFeed().snapshot()
        self.bars = snapshot.bars['DEMOUSDT']
        self.now = snapshot.now_ms
        self.state = dict(last_check_ms=self.now, phase='WAIT_SIGNAL',lock=None,active=None,error=None)

    def test_locked_pilot_vetoes_without_mutation(self):
        self.state['lock'] = 'ORDER_REVIEW'
        before = deepcopy(self.state)
        r = analyze(self.bars,self.state,self.now)
        self.assertEqual(r['decision'],'BLOCKED')
        self.assertEqual(self.state,before)
        self.assertFalse(r['execution_enabled'])

    def test_stale_future_missing_heartbeat_fail_closed(self):
        for stamp in (None,self.now+1,self.now-120001):
            with self.subTest(stamp=stamp):
                self.state['last_check_ms']=stamp
                self.assertEqual(analyze(self.bars,self.state,self.now)['decision'],'BLOCKED')

    def test_stale_and_missing_candles(self):
        for bars,now in ((self.bars,self.now+180000),(self.bars[:-1],self.now),
                         (self.bars[:50]+self.bars[51:],self.now),([],self.now)):
            self.assertEqual(analyze(bars,self.state,now)['decision'],'BLOCKED')

    def test_news_explicitly_unavailable(self):
        r=analyze(self.bars,self.state,self.now)
        self.assertEqual(next(v for v in r['votes'] if v['agent']=='news')['reason'],'NOT_CONNECTED')
        self.assertFalse(r['llm_enabled'])

    def test_restart_deduplicates_and_read_does_not_change_pilot(self):
        with TemporaryDirectory() as d:
            pilot=Path(d)/'pilot.db'; output=Path(d)/'shadow.db'
            with sqlite3.connect(pilot) as db:
                db.execute('CREATE TABLE state(id INTEGER PRIMARY KEY,data TEXT)')
                db.execute('INSERT INTO state VALUES(1,?)',(encode(self.state),))
            before=pilot.read_bytes()
            state=read_pilot(pilot)
            result=analyze(self.bars,state,self.now)
            record(output,result);record(output,result)
            with sqlite3.connect(output) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0],1)
            self.assertEqual(before,pilot.read_bytes())
