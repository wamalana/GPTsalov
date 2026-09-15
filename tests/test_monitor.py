import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from gptsalov.monitor import collect, ledger_status, make_server, Snapshot, MAX_SNAPSHOT_AGE_MS
from gptsalov.watchdog import check, transition

STAMP = 1_800_000_000_000


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'paper.db'
        self.state = dict(schema=1, mode='paper', source='binance', observed_at_ms=STAMP-1000,
                          as_of_ms=STAMP-10000, equity='102', initial_equity='100', balance='101',
                          closed_trades=4, wins=2, last_error=None, hard_lock=None, daily_locked=False)
        self.write()

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY, data TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,timestamp_ms INTEGER,kind TEXT,data TEXT)')
            db.execute('INSERT OR REPLACE INTO state VALUES(1,?)',(json.dumps(self.state),))

    def test_read_only_and_allowlist(self):
        self.state.update(last_error='SECRET_API_KEY', unknown_token='SECRET_API_KEY')
        self.write()
        before = self.path.read_bytes()
        result = collect(self.path, lambda _: {'status':'active'}, STAMP)
        self.assertFalse(result['healthy'])
        self.assertNotIn('SECRET_API_KEY',json.dumps(result))
        self.assertEqual(before,self.path.read_bytes())
        self.assertEqual(result['ledger']['metrics']['pnl'],2)

    def test_missing_database_not_created(self):
        missing = self.path.with_name('missing.db')
        self.assertEqual(ledger_status(missing,STAMP)['status'],'unavailable')
        self.assertFalse(missing.exists())

    def test_health_requires_current_observation_and_service(self):
        self.assertTrue(collect(self.path,lambda _: {'status':'active'},STAMP)['healthy'])
        self.assertFalse(collect(self.path,lambda _: {'status':'stopped'},STAMP)['healthy'])
        for changes, status in [({'observed_at_ms':STAMP-181000},'stale'),
                                ({'observed_at_ms':STAMP+1},'stale'),
                                ({'as_of_ms':STAMP-4_000_000},'stale'),
                                ({'hard_lock':'RISK'},'locked'),
                                ({'source':'synthetic-demo'},'synthetic')]:
            original = self.state.copy()
            self.state.update(changes);self.write()
            result=collect(self.path,lambda _: {'status':'active'},STAMP)
            self.assertFalse(result['healthy'])
            self.assertEqual(result['ledger']['status'],status)
            self.state=original;self.write()

    def test_snapshot_expiration(self):
        snap=Snapshot(self.path)
        snap.data={'checked_at_ms':STAMP,'healthy':True}
        with patch('gptsalov.monitor.now_ms',return_value=STAMP+MAX_SNAPSHOT_AGE_MS+1):
            self.assertFalse(snap.read()['healthy'])

    def test_auth_scope_and_health_response(self):
        snap=Snapshot(self.path)
        snap.data=collect(self.path,lambda _: {'status':'active'},STAMP)
        server=make_server(snap,'v'*36,'h'*36,0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            for path,token in [('/api/status',''),('/api/status','h'*36),('/healthz','v'*36)]:
                with self.assertRaises(HTTPError) as exc:
                    urlopen(Request(base+path,headers={'Authorization':'Bearer '+token}),timeout=2)
                self.assertEqual(exc.exception.code,401)
            with patch('gptsalov.monitor.now_ms',return_value=STAMP):
                with urlopen(Request(base+'/api/status',headers={'Authorization':'Bearer '+'v'*36}),timeout=2) as r:
                    self.assertTrue(json.load(r)['healthy'])
                with urlopen(Request(base+'/healthz',headers={'Authorization':'Bearer '+'h'*36}),timeout=2) as r:
                    self.assertEqual(set(json.load(r)),{'schema','healthy','checked_at_ms'})
            with patch('gptsalov.monitor.now_ms',return_value=STAMP+100000):
                with self.assertRaises(HTTPError) as exc:
                    urlopen(Request(base+'/healthz',headers={'Authorization':'Bearer '+'h'*36}),timeout=2)
                self.assertEqual(exc.exception.code,503)
            with urlopen(base,timeout=2) as r:
                self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])
            with self.assertRaises(HTTPError):
                urlopen(base+'/../monitor.py',timeout=2)
        finally:
            server.shutdown();server.server_close();thread.join()


class WatchdogTests(unittest.TestCase):
    def test_failure_threshold_and_recovery(self):
        previous={}
        for i in range(3):
            previous,event=transition(previous,{'healthy':False,'reason':'unreachable'})
            self.assertEqual(bool(event),i==2)
        previous,event=transition(previous,{'healthy':False,'reason':'unreachable'})
        self.assertIsNone(event)
        previous,event=transition(previous,{'healthy':True,'reason':'ok'})
        self.assertIn('recovered',event)
        self.assertEqual(previous['failures'],0)

    def test_reject_insecure_urls(self):
        for url in ['http://host/healthz','https://host/healthz?token=x','https://user:pass@host/healthz','https://host/api/status']:
            with self.assertRaises(ValueError):check(url,'h'*36)

    def test_reject_stale_healthy_response(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return json.dumps({'schema':1,'healthy':True,'checked_at_ms':STAMP-100000}).encode()
        class Opener:
            def open(self,*args,**kwargs):return Response()
        self.assertFalse(check('https://example.com/healthz','h'*36,Opener(),STAMP)['healthy'])


if __name__=='__main__':unittest.main()
