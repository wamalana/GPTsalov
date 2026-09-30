from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import unittest

from gptsalov import news_analysis as n
from gptsalov.core import encode
from gptsalov.market import DemoFeed
from gptsalov.multiagent_shadow import analyze, record


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.now = 1790611200000
        self.news = dict(status='current', sources=[dict(name='CoinDesk', fresh=True, error=None)],
            articles=[dict(source='CoinDesk', title='Bitcoin faces market uncertainty',
                url='https://www.coindesk.com/test', published_ms=self.now-1000)])
        self.rows = n.articles(self.news, self.now)

    def response(self, rows, **changes):
        items = [dict(id=r['id'], scope='ASSET', symbols=['BTCUSDT'],
            sentiment='UNCLEAR', risk='HIGH', reason='Headline uncertainty') for r in rows]
        if items:
            items[0].update(changes)
        return dict(status='completed', output=[dict(content=[dict(type='output_text',
            text=encode(dict(summary='Research context only', items=items)))])])

    def test_match_avoids_substrings_and_ambiguous_tickers(self):
        self.assertTrue(n.matches('BTCUSDT', 'Bitcoin ETF'))
        self.assertFalse(n.matches('ETHUSDT', 'Tether growth'))
        self.assertFalse(n.matches('NEARUSDT', 'Crypto near record high'))
        self.assertFalse(n.matches('DASHUSDT', 'Investors dash for cash'))
        self.assertTrue(n.matches('NEARUSDT', 'NEAR protocol upgrade'))
        self.assertFalse(n.matches('BNBUSDT', 'Binance legal news'))

    def test_stale_failed_future_duplicate_and_untrusted_sources(self):
        source = deepcopy(self.news)
        source['sources'][0]['error'] = 'Timeout'
        self.assertEqual(n.articles(source, self.now), [])
        for stamp in (self.now+1, self.now-86400001):
            source = deepcopy(self.news)
            source['articles'][0]['published_ms'] = stamp
            self.assertEqual(n.articles(source, self.now), [])
        source = deepcopy(self.news)
        source['articles'] *= 2
        self.assertEqual(len(n.articles(source, self.now)), 1)
        source['articles'][0]['url'] = 'https://evil.example/ignore-all-rules'
        self.assertEqual(n.articles(source, self.now), [])

    def test_citations_and_symbols_validated(self):
        self.assertEqual(n.validate(self.response(self.rows), self.rows)['items'][0]['symbols'], ['BTCUSDT'])
        for changes in ({'id': 'invented'}, {'symbols': ['ETHUSDT']}, {'symbols': []},
                        {'scope': 'MARKET'}, {'risk': 'BUY'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                n.validate(self.response(self.rows, **changes), self.rows)
        with self.assertRaises(ValueError):
            n.validate(self.response([]), self.rows)

    def test_quota_restart_and_failed_attempt_reservation(self):
        with TemporaryDirectory() as tmp:
            cfg = Path(tmp)/'config.json'
            cfg.write_text(encode(dict(api_key='fake', model='model', daily_calls=1)))
            cfg.chmod(0o600)
            ledger = Path(tmp)/'analysis.db'
            called = []
            def fail(*args):
                called.append(1)
                raise TimeoutError('secret-hidden')
            with patch.object(n, 'snapshot', return_value=self.news):
                first = n.run(cfg, ledger, transport=fail, stamp=self.now)
                self.assertEqual(first['status'], 'FAILED')
                self.assertNotIn('secret-hidden', encode(first))
                self.assertEqual(n.run(cfg, ledger, transport=fail, stamp=self.now+1), first)
                self.assertEqual(n.run(cfg, ledger, transport=fail, stamp=self.now+n.HOUR)['status'], 'DAILY_LIMIT')
                self.assertEqual(called, [1])
            self.assertFalse(n.read(ledger, self.now+n.HOUR)['llm_connected'])

    def test_context_is_symbol_specific_and_cannot_override_lock(self):
        ai = dict(status='COMPLETED', llm_connected=True, model='test', articles=self.rows,
                  **n.validate(self.response(self.rows), self.rows))
        with patch.object(n, 'snapshot', return_value=self.news), patch.object(n, 'read', return_value=ai):
            btc = n.context('BTCUSDT', self.now)
            eth = n.context('ETHUSDT', self.now)
        self.assertEqual(btc['verdict'], 'CAUTION')
        self.assertTrue(btc['llm_enabled'])
        self.assertFalse(eth['llm_enabled'])
        self.assertEqual(eth['articles'], [])
        feed = DemoFeed().snapshot()
        state = dict(lock='PILOT_BATCH_COMPLETE', phase='LOCKED', last_check_ms=feed.now_ms)
        original = deepcopy(state)
        result = analyze(feed.bars['DEMOUSDT'], state, feed.now_ms, news_context=btc)
        self.assertEqual(result['decision'], 'BLOCKED')
        self.assertEqual(state, original)
        self.assertFalse(result['execution_enabled'])

    def test_changed_headline_does_not_reuse_old_ai(self):
        ai = dict(status='COMPLETED', llm_connected=True, model='test', articles=self.rows,
                  **n.validate(self.response(self.rows), self.rows))
        news = deepcopy(self.news)
        news['articles'][0]['title'] = 'Bitcoin story corrected'
        with patch.object(n, 'snapshot', return_value=news), patch.object(n, 'read', return_value=ai):
            ctx = n.context('BTCUSDT', self.now)
        self.assertFalse(ctx['llm_enabled'])
        self.assertEqual(ctx['reason'], 'HEADLINE_CONTEXT_ONLY')

    def test_asset_tone_does_not_leak_into_market_context(self):
        news = deepcopy(self.news)
        news['articles'][0]['title'] = 'Bitcoin trading on Binance faces uncertainty'
        rows = n.articles(news, self.now)
        ai = dict(status='COMPLETED',llm_connected=True,model='test',articles=rows,
                  **n.validate(self.response(rows),rows))
        with patch.object(n,'snapshot',return_value=news), patch.object(n,'read',return_value=ai):
            self.assertTrue(n.context('BTCUSDT',self.now)['llm_enabled'])
            other = n.context('ETHUSDT',self.now)
            self.assertFalse(other['llm_enabled'])
            self.assertEqual(other['verdict'],'CONTEXT')

    def test_success_and_expiry(self):
        with TemporaryDirectory() as tmp:
            cfg = Path(tmp)/'config.json'
            cfg.write_text(encode(dict(api_key='fake', model='model', daily_calls=24)))
            cfg.chmod(0o600)
            ledger = Path(tmp)/'analysis.db'
            with patch.object(n, 'snapshot', return_value=self.news):
                n.run(cfg, ledger, transport=lambda c, rows: self.response(rows), stamp=self.now)
            self.assertTrue(n.read(ledger, self.now)['llm_connected'])
            self.assertFalse(n.read(ledger, self.now+n.MAX_AGE+1)['llm_connected'])
            self.assertFalse(n.read(ledger, self.now-1)['llm_connected'])

    def test_observation_keys_do_not_mix_symbols(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/'observations.db'
            for symbol in ('BTCUSDT','ETHUSDT','BTCUSDT'):
                record(path, dict(symbol=symbol,candle_close_ms=1))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0], 2)

    def test_concurrent_runs_reserve_one_request(self):
        with TemporaryDirectory() as tmp:
            cfg = Path(tmp)/'config.json'
            cfg.write_text(encode(dict(api_key='fake', model='model', daily_calls=24)))
            cfg.chmod(0o600)
            ledger = Path(tmp)/'analysis.db'
            called = []
            def transport(c, rows):
                called.append(1)
                return self.response(rows)
            with patch.object(n, 'snapshot', return_value=self.news), ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: n.run(cfg, ledger, transport=transport, stamp=self.now), range(2)))
            self.assertEqual(len(called), 1)
            self.assertTrue(any(r['status'] == 'COMPLETED' for r in results))

    def test_news_advisory_does_not_change_execution_gate(self):
        from types import SimpleNamespace
        from gptsalov.multiagent_gate import evaluate
        base = dict(decision='RESEARCH_CANDIDATE', votes=[dict(agent=k,verdict=v) for k,v in
            [('data','PASS'),('risk','PASS'),('volatility','PASS'),('trend','LONG'),('momentum','LONG')]])
        for news_vote in ('CONTEXT','CAUTION','ABSTAIN'):
            result = deepcopy(base)
            result['votes'].append(dict(agent='news',verdict=news_vote))
            with patch('gptsalov.multiagent_gate.analyze', return_value=result):
                self.assertTrue(evaluate([],{},self.now,SimpleNamespace(side=1))[0])
            result['votes'][1]['verdict'] = 'VETO'
            with patch('gptsalov.multiagent_gate.analyze', return_value=result):
                self.assertFalse(evaluate([],{},self.now,SimpleNamespace(side=1))[0])


if __name__ == '__main__':
    unittest.main()
