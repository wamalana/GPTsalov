from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
from gptsalov.risk_upgrade import upgrade_policy
from gptsalov.order_risk import VERSION
from gptsalov.testnet_pilot import Book,POLICY


class UpgradeTests(unittest.TestCase):
    def state(self):
        return dict(policy={**POLICY,'decision_engine':'multiagent-testnet-v1'},
            equity='49.5',closed_trades=8,lock='PILOT_BATCH_COMPLETE',active=None,
            created_ms=100,batch_started_ms=200,loss_streak=2,adaptive_results=[{'net':'-.1'}])

    def test_upgrade_preserves_everything_except_policy(self):
        s=self.state();old=deepcopy(s)
        upgrade_policy(s,{**s['policy'],'risk_model':VERSION})
        expected={k:v for k,v in old.items() if k!='policy'}
        expected.update(active=[],next_trade_id=9)
        self.assertEqual({k:v for k,v in s.items() if k!='policy'},expected)

    def test_reject_active_changed_limits_or_unknown_version(self):
        for change in ('active','cap','version','environment'):
            s=self.state();target={**s['policy'],'risk_model':VERSION}
            if change=='active':s['active']={'file':'trade.db'}
            if change=='cap':target['risk_cap']='5'
            if change=='version':target['risk_model']='unknown'
            if change=='environment':s['policy']['environment']='live'
            old=deepcopy(s)
            with self.assertRaises(ValueError):upgrade_policy(s,target)
            self.assertEqual(s,old)

    def test_book_upgrade_is_durable_and_does_not_clear_lock(self):
        with TemporaryDirectory() as d,patch.dict(POLICY,decision_engine='multiagent-testnet-v1'):
            b=Book(d,'synthetic');b.s['lock']='PILOT_BATCH_COMPLETE';b.s['closed_trades']=8;b.save();b.close()
            with patch.dict(POLICY,risk_model=VERSION):
                with self.assertRaises(ValueError):Book(d,'synthetic')
                b=Book(d,'synthetic',allow_risk_upgrade=True)
                self.assertEqual(b.s['lock'],'PILOT_BATCH_COMPLETE')
                self.assertEqual(b.s['closed_trades'],8)
                self.assertEqual(b.db.execute('select count(*) from events').fetchone()[0],2)
                b.close()
                b=Book(d,'synthetic');self.assertEqual(b.s['policy']['risk_model'],VERSION);b.close()

    def test_setup_defers_leverage_to_durable_coordinator(self):
        from test_order_risk import RiskExchange
        from gptsalov.testnet_smoke import setup
        api=RiskExchange();api.leverage=2
        setup(api,'BTCUSDT',leverage=None)
        self.assertFalse(any(m=='POST' for m,_,_ in api.calls))

    def test_multi_market_path_keeps_adaptive_stop_and_adds_leverage(self):
        from types import SimpleNamespace
        from gptsalov import testnet_pilot as p
        from gptsalov.core import BAR_MS,Plan,Signal,dec
        from test_order_risk import RiskExchange,STATE
        api=RiskExchange();original=api.call
        def call(method,path,**kw):
            r=original(method,path,**kw)
            if path.endswith('exchangeInfo'):r['symbols'][0]['marginAsset']='USDT'
            return r
        api.call=call
        t=1789602350000;stamp=t//BAR_MS*BAR_MS-1
        scan=dict(status='current',started_ms=t-1000,rows=[dict(symbol='BTCUSDT',
            market='USD-M',quote='USDT',contract='PERPETUAL',decision='CANDIDATE',
            candle_close_ms=stamp,observed_ms=t,score=1,volume=100000000)])
        book=SimpleNamespace(s={**STATE,'seen_ms':None,'policy':{'risk_model':VERSION}},save=lambda *a:None)
        meta=call('GET','/fapi/v1/exchangeInfo')['symbols']
        public=SimpleNamespace(get=lambda path,**kw: {'symbols':meta} if path.endswith('exchangeInfo')
            else [] if path.endswith('klines') else [{'symbol':'BTCUSDT'}])
        signal=Signal('BTCUSDT',1,stamp,dec(100),dec(98),dec(106),dec(1))
        sized=Plan(dec(100),dec(98),dec(106),dec('.09'),dec(9),dec('.21'),dec('.25'))
        with patch.object(p,'now_ms',return_value=t), \
             patch('gptsalov.market_scanner.read',return_value=scan), \
             patch('gptsalov.market_scanner.classify',return_value={'status':'pending'}), \
             patch.object(p,'closed_bars',return_value=[SimpleNamespace(close_ms=stamp)]), \
             patch.object(p,'strategy',return_value=signal), \
             patch('gptsalov.multiagent_gate.evaluate',return_value=(True,{'strategy_quality':{'atr':'1'}})) as gate, \
             patch.object(p,'adaptive_stop',return_value=(signal,{'version':'atr-structure-v1'})) as stop, \
             patch.object(p,'buffered_size',return_value=sized):
            result=(p.multi_candidate(api,public,book) or [None])[0]
        self.assertEqual(result['risk_model'],VERSION)
        self.assertEqual(result['stop_model'],'atr-structure-v1')
        self.assertEqual(result['symbol'],'BTCUSDT')
        self.assertEqual(result['leverage'],1)
        self.assertLessEqual(dec(result['risk_cap']),dec('.25'))
        self.assertTrue(gate.call_args.kwargs['risk_aware'])
        stop.assert_called_once()
        self.assertFalse(any(m=='POST' for m,_,_ in api.calls))

from tests.conservative_limits import setUpModule, tearDownModule  # noqa: E402,F401
