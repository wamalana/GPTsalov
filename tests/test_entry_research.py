import copy
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from gptsalov.core import BAR_MS, Candle, D, Signal
from gptsalov import entry_research as r
from gptsalov import market_scanner as scanner


def candles(n=120):
    return [Candle(i*BAR_MS,(i+1)*BAR_MS-1,100,101,99,100,100) for i in range(n)]


def minute(start,o=100,h=100.2,l=99.8,c=100):
    return [start,str(o),str(h),str(l),str(c),'10',start+59999]


class EntryResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'research.db'
        self.bars=candles()
        self.now=self.bars[-1].close_ms+1000
        self.row=dict(market='USD-M',symbol='TESTUSDT',quote='USDT',contract='PERPETUAL',status='analyzed')
        self.sig=asdict(Signal('TESTUSDT',1,self.bars[-1].close_ms,D(100),D(99),D(102),D(1)))

    def seed(self):
        assessment={v:dict(reason='SIGNAL',signal=self.sig) for v in r.VARIANTS}
        with patch.object(r,'assess',return_value=(assessment,dict(x=1))):
            recorder=r.Recorder(self.path)
            recorder.observe(1,self.row,self.bars,self.now)
            recorder.observe(2,self.row,self.bars,self.now+60000)
            self.assertIsNone(recorder.error)
        return (self.now//60000+1)*60000

    def test_both_new_strategies_long_short_and_volume_veto(self):
        for variant in ('trend_pullback_v1','compression_breakout_v1'):
            for side in (1,-1):
                bars=[]
                for i in range(120):
                    c=D(100)+D(i)/10
                    spread=D('3') if variant=='compression_breakout_v1' and i<=70 else D('.3')
                    bars.append(Candle(i*BAR_MS,(i+1)*BAR_MS-1,c,c+spread,c-spread,c,100))
                if variant=='trend_pullback_v1':
                    e,_=r.ema([b.close for b in bars[:-1]])
                    b=bars[-2]
                    bars[-2]=Candle(b.open_ms,b.close_ms,b.open,b.high,e-D('.1'),e,100)
                b=bars[-1]
                bars[-1]=Candle(b.open_ms,b.close_ms,b.open,b.close+1,b.low,b.close+D('.8'),200)
                if side==-1:
                    bars=[Candle(b.open_ms,b.close_ms,300-b.open,300-b.low,300-b.high,300-b.close,b.volume) for b in bars]
                result,_=r.assess('TESTUSDT',bars,self.now)
                self.assertEqual(result[variant]['signal']['side'],side)
                b=bars[-1]
                bars[-1]=Candle(b.open_ms,b.close_ms,b.open,b.high,b.low,b.close,1)
                result,_=r.assess('TESTUSDT',bars,self.now)
                self.assertEqual(result[variant]['reason'],'VOLUME_FILTER')

    def test_idempotence_and_prospective_entry(self):
        start=self.seed()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM signals').fetchone()[0],3)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM bars').fetchone()[0],120)
            self.assertEqual(db.execute('SELECT DISTINCT entry_ms FROM signals').fetchall(),[(start,)])
        self.assertGreater(start,self.now)

    def test_closed_future_and_gap_rejected(self):
        results,_=r.assess('TESTUSDT',self.bars,self.bars[-1].close_ms)
        self.assertTrue(all(x['reason']=='INVALID_OR_STALE_DATA' for x in results.values()))
        results,_=r.assess('TESTUSDT',self.bars[:50]+self.bars[51:],self.now)
        self.assertTrue(all(x['signal'] is None for x in results.values()))

    def test_baseline_exact_and_nonmutation(self):
        bars=self.bars
        before=copy.deepcopy(bars)
        with patch.object(r,'strategy',return_value=Signal.restore(self.sig)):
            results,features=r.assess('TESTUSDT',bars,self.now)
        self.assertEqual(results['baseline_v1']['signal'],self.sig)
        self.assertEqual(bars,before)
        self.assertEqual(features['latest_close_ms'],bars[-1].close_ms)

    def test_stop_wins_ambiguous_bar_costs_and_no_duplicate_outcomes(self):
        start=self.seed()
        fetch=lambda *args:[minute(start,h=104,l=98)]
        result=r.evaluate(self.path,fetch,start+60001,pause=0)
        self.assertEqual(result['processed'],3)
        self.assertEqual(r.evaluate(self.path,fetch,start+60002,pause=0)['processed'],0)
        with sqlite3.connect(self.path) as db:
            for (data,) in db.execute('SELECT data FROM outcomes'):
                out=json.loads(data)
                self.assertEqual(out['reason'],'STOP')
                self.assertTrue(out['ambiguous_exit_bar'])
                self.assertLess(out['net'],-.25)
                self.assertFalse(out['exchange_sizing'])

    def test_no_entry_until_full_minute_closes(self):
        start=self.seed()
        with patch.object(r,'public_bars') as fetch:
            self.assertEqual(r.evaluate(self.path,fetch,start+59999,pause=0)['processed'],0)
            fetch.assert_not_called()

    def test_missing_entry_does_not_shift_fill_forward(self):
        start=self.seed()
        r.evaluate(self.path,lambda *args:[minute(start+60000)],start+120001,pause=0)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT DISTINCT status FROM outcomes').fetchall(),[('WAIT_DATA',)])

    def test_rate_failure_stops_batch_and_cools_down(self):
        start=self.seed()
        def fail(*args):
            raise OSError('unavailable')
        self.assertEqual(r.evaluate(self.path,fail,start+120001,pause=0)['status'],'ERROR')
        self.assertEqual(r.evaluate(self.path,fail,start+120002,pause=0)['status'],'COOLDOWN')

    def test_policy_identity_and_storage_failure_visible(self):
        self.seed()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE policy SET fingerprint='different'")
        recorder=r.Recorder(self.path)
        recorder.observe(1,self.row,self.bars,self.now)
        self.assertEqual(recorder.error,'ValueError')

    def test_scanner_database_alias_rejected(self):
        with self.assertRaises(ValueError):
            scanner.run(path=self.path,research_path=self.path)

    def test_scan_preserves_rejected_and_pending_rows(self):
        rec=r.Recorder(self.path)
        data=dict(started_ms=1,finished_ms=2,status='partial',rows=[dict(reason='LOW_VOLUME'),dict(status='pending')])
        rec.finish(data)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(json.loads(db.execute('SELECT data FROM scans').fetchone()[0]),data)

    def test_expiry_is_visible_not_backfilled(self):
        start=self.seed()
        r.evaluate(self.path,lambda *a:self.fail('no fetch'),start+86400001,pause=0)
        report=r.report(self.path)
        self.assertTrue(all(x['expired']==1 and x['closed']==0 for x in report['results']))
        self.assertEqual(report['policy']['ml_status'],'NOT_TRAINED')

if __name__=='__main__':unittest.main()
