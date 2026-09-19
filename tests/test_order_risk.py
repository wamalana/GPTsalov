from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from gptsalov.core import Candle, Rules, Signal, dec
from gptsalov.order_risk import (VERSION, risk_budget, plan_order, strategy_review,
                                 costs, bracket_rows)
from gptsalov.testnet import Coordinator, Journal, ORDER, Uncertain, Rejected
from test_testnet import Exchange

STATE = dict(equity='50', high_water='50', day_start='50', loss_streak=0)
BRACKETS = dict(symbol='BTCUSDT', brackets=[dict(notionalFloor=0,
    notionalCap=10000, maintMarginRatio='.005', initialLeverage=20)])
RULES = Rules('BTCUSDT', dec('.01'), dec('.001'), dec('.001'),
              dec(1000), dec(1), dec(10000), dec('.01'), dec(1000000))


def proposal(**overrides):
    inputs = dict(symbol='BTCUSDT', side=1, reference='100', stop='98',
        target='106', atr='1', quantity_cap='1', rules=RULES, state=STATE,
        available_balance='50', brackets=BRACKETS)
    inputs.update(overrides)
    return plan_order(**inputs)


class RiskTests(unittest.TestCase):
    def test_default_budget_and_daily_room(self):
        self.assertEqual(risk_budget(STATE), dec('.25'))
        self.assertEqual(risk_budget({**STATE, 'equity':'49.05'}), dec('.05'))

    def test_drawdown_and_losses_reduce_budget(self):
        self.assertEqual(risk_budget({**STATE, 'loss_streak':2}), dec('.125'))
        self.assertEqual(risk_budget({**STATE, 'equity':'48','day_start':'48'}), dec('.12'))
        for state in ({**STATE,'equity':'49'}, {**STATE,'loss_streak':3},
                      {**STATE,'active':{'file':'x'}}, {**STATE,'lock':'ORDER_REVIEW'},
                      {**STATE,'equity':'NaN'}, {**STATE,'high_water':'0'}):
            with self.subTest(state=state), self.assertRaises(ValueError): risk_budget(state)

    def test_quantity_then_lowest_leverage(self):
        p=proposal()
        self.assertEqual(p['leverage'],1)
        self.assertLessEqual(p['modeled_risk'],dec('.25'))
        self.assertLessEqual(p['notional'],25)
        self.assertEqual(p['quantity']%RULES.step,0)
        p2=proposal(available_balance='5')
        self.assertEqual(p2['leverage'],2)
        self.assertEqual(p2['quantity'],p['quantity'])
        self.assertLessEqual(p2['initial_margin']+p2['cost_reserve'],5)

    def test_leverage_does_not_increase_quantity_above_risk_limit(self):
        base=proposal()
        for cash in ('50','9','5','2'):
            p=proposal(available_balance=cash)
            self.assertLessEqual(p['quantity'],base['quantity'])
            self.assertLessEqual(p['modeled_risk'],p['risk_budget'])

    def test_both_directions_and_high_volatility(self):
        for side,stop,target in ((1,'98','106'),(-1,'102','94')):
            p=proposal(side=side,stop=stop,target=target)
            self.assertGreater(p['stress_surplus'],0)
            self.assertGreaterEqual(p['net_rr'],dec('1.2'))
        self.assertLess(proposal(stop='95',target='115',atr='3')['quantity'],proposal()['quantity'])

    def test_minimum_never_forces_risk_increase(self):
        from dataclasses import replace
        with self.assertRaisesRegex(ValueError,'MINIMUM'):
            proposal(rules=replace(RULES,min_notional=dec(20)))

    def test_no_margin_no_brackets_and_invalid_numbers(self):
        for args in (dict(available_balance=0),dict(brackets=[]),dict(atr='NaN'),
                     dict(target='100.4'),dict(stop='101'),dict(quantity_cap=-1),
                     dict(side=0),dict(reference='Infinity')):
            with self.subTest(args=args),self.assertRaises(ValueError): proposal(**args)

    def test_costs_reject_apparently_attractive_gross_rr(self):
        # Gross 2R, but drift and trading costs remove most of the reward.
        with self.assertRaisesRegex(ValueError,'NET_RR'):
            proposal(stop='99',target='102')

    def test_custom_or_malformed_brackets_fail_closed(self):
        for b in ({**BRACKETS,'notionalCoef':'1.5'},
                  {**BRACKETS,'brackets':[]},
                  {**BRACKETS,'symbol':'ETHUSDT'}):
            with self.assertRaises(ValueError): bracket_rows(b,'BTCUSDT')
        b=deepcopy(BRACKETS);b['brackets'][0]['initialLeverage']=1
        p=proposal(available_balance=5,brackets=b)
        self.assertEqual(p['leverage'],1)
        self.assertLess(p['quantity'],proposal()['quantity'])

    def test_stress_rejects_margin_exhaustion_and_short_tier_transition(self):
        from dataclasses import replace
        with self.assertRaises(ValueError): proposal(atr=40)
        b=dict(symbol='BTCUSDT',brackets=[
            dict(notionalFloor=0,notionalCap=9,maintMarginRatio='.005',initialLeverage=20),
            dict(notionalFloor=9,notionalCap=10000,maintMarginRatio='.9',initialLeverage=1)])
        with self.assertRaises(ValueError):
            proposal(side=-1,stop='102',target='94',available_balance=5,brackets=b,
                     rules=replace(RULES,min_notional=dec(7)))

    def test_risk_invariants_over_prices_stops_cash_and_sides(self):
        for side in (-1,1):
            for distance in (1,2,4,8):
                for cash in (2,5,10,50):
                    p=proposal(side=side,stop=100-side*distance,
                               target=100+side*distance*4,available_balance=cash)
                    self.assertLessEqual(p['modeled_risk'],risk_budget(STATE))
                    self.assertLessEqual(p['notional'],25)
                    self.assertLessEqual(p['initial_margin']+p['cost_reserve'],min(cash,25))
                    self.assertLessEqual(p['leverage'],2)


class QualityTests(unittest.TestCase):
    def bars(self):
        bars=[]
        for i in range(22):
            p=dec(100)+dec(i)*dec('.2')
            bars.append(Candle(i*900000,(i+1)*900000-1,p,p+dec('.4'),p-dec('.4'),p,10))
        last=bars[-1]
        bars[-1]=Candle(last.open_ms,last.close_ms,last.open,dec('104.7'),dec('104'),dec('104.5'),20)
        return bars

    def test_trend_breakout_and_chase_filter(self):
        bars=self.bars()
        signal=Signal('BTCUSDT',1,bars[-1].close_ms,bars[-1].close,dec(102),dec(110),dec(1))
        self.assertTrue(strategy_review(bars,signal)['allow'])
        last=bars[-1]
        bars[-1]=Candle(last.open_ms,last.close_ms,last.open,110,104,109,20)
        r=strategy_review(bars,signal)
        self.assertIn('BREAKOUT_CHASE',r['reasons'])
        self.assertIn('SHOCK_CANDLE',r['reasons'])

    def test_choppy_path_is_vetoed(self):
        bars=[Candle(i*900000,(i+1)*900000-1,100,102,98,100+(i%2),10) for i in range(22)]
        signal=Signal('BTCUSDT',1,bars[-1].close_ms,dec(101),dec(98),dec(107),dec(1))
        self.assertIn('CHOPPY_TREND',strategy_review(bars,signal)['reasons'])


class RiskExchange(Exchange):
    def __init__(self):
        super().__init__()
        self.cash='50';self.reject_leverage=False;self.timeout_leverage=False
        self.accept_leverage=True;self.missing_brackets=False

    def call(self,method,path,**params):
        if path.endswith('/balance'):
            self.calls.append((method,path,params))
            return [dict(asset='USDT',availableBalance=self.cash)]
        if path.endswith('/leverageBracket'):
            self.calls.append((method,path,params))
            return [] if self.missing_brackets else BRACKETS
        if path.endswith('/leverage'):
            self.calls.append((method,path,params))
            if self.reject_leverage: raise Rejected(-1)
            if self.accept_leverage:self.leverage=params['leverage']
            if self.timeout_leverage:raise Uncertain('timeout')
            return dict(leverage=self.leverage,symbol='BTCUSDT',maxNotionalValue='10000')
        return super().call(method,path,**params)


class ExecutionRiskTests(unittest.TestCase):
    def prepare(self,path,api):
        r=proposal()
        j=Journal(Path(path)/'trade.db');self.addCleanup(j.close)
        c=Coordinator(j,api)
        c.prepare(dict(environment='testnet',symbol='BTCUSDT',side='BUY',
            quantity=str(r['quantity']),reference='100',stop='98',target='106',
            purpose='STRATEGY_PILOT',risk_model=VERSION,leverage=r['leverage'],
            risk_state=STATE,risk_atr='1'))
        return c

    def test_selected_leverage_verified_before_entry(self):
        with TemporaryDirectory() as d:
            api=RiskExchange();c=self.prepare(d,api)
            self.assertEqual(c.advance(),'PROTECTED')
            self.assertEqual(api.leverage,1)
            self.assertIsNotNone(c.j.get('risk_preflight'))
            writes=[x[1] for x in api.calls if x[0]=='POST']
            self.assertLess(writes.index('/fapi/v1/leverage'),writes.index(ORDER))
            c.advance()
            self.assertEqual(sum(x[:2]==('POST','/fapi/v1/leverage') for x in api.calls),1)

    def test_timeout_accepted_setting_reconciles_via_get(self):
        with TemporaryDirectory() as d:
            api=RiskExchange();api.timeout_leverage=True;c=self.prepare(d,api)
            self.assertEqual(c.advance(),'PROTECTED')
            self.assertEqual(c.j.get('leverage')['phase'],'ATTEMPTED')

    def test_failed_or_unconfirmed_setting_never_enters_or_retries(self):
        for rejection in (False,True):
            with TemporaryDirectory() as d:
                api=RiskExchange();api.reject_leverage=rejection
                api.timeout_leverage=True;api.accept_leverage=False
                c=self.prepare(d,api)
                for _ in range(2):
                    with self.assertRaises(ValueError):c.advance()
                self.assertFalse(any(x[:2]==('POST',ORDER) for x in api.calls))
                self.assertEqual(sum(x[:2]==('POST','/fapi/v1/leverage') for x in api.calls),1)

    def test_balance_change_or_missing_tiers_blocks_before_any_write(self):
        for field,value in (('cash','1'),('missing_brackets',True),('amount','.01')):
            with TemporaryDirectory() as d:
                api=RiskExchange();c=self.prepare(d,api);setattr(api,field,value)
                with self.assertRaises(ValueError):c.advance()
                self.assertFalse(any(x[0]=='POST' for x in api.calls))

    def test_cannot_request_unbounded_leverage(self):
        from test_testnet import PLAN
        for value in (3,125,True,'2'):
            with TemporaryDirectory() as d,Journal(Path(d)/'x.db') as j:
                with self.assertRaises(ValueError):
                    Coordinator(j,RiskExchange()).prepare({**PLAN,'risk_model':VERSION,
                        'leverage':value,'purpose':'STRATEGY_PILOT'})


class CandidateIntegrationTests(unittest.TestCase):
    def test_post_fill_respects_reduced_budget(self):
        from test_testnet_pilot import PilotExchange
        from test_testnet import PLAN
        from gptsalov.testnet_pilot import reconcile
        with TemporaryDirectory() as d, Journal(Path(d)/'trade.db') as j:
            api=PilotExchange();c=Coordinator(j,api)
            c.prepare({**PLAN,'symbol':'ETHUSDT','risk_assessment':{'risk_budget':'.005'}})
            self.assertEqual(reconcile(c)[0],'EXIT_RECONCILIATION')
            self.assertTrue(any(x[:2]==('POST',ORDER) and x[2].get('reduceOnly')=='true'
                                for x in api.calls))

    def test_opt_in_candidate_records_risk_and_cannot_post(self):
        from unittest.mock import Mock, patch
        from types import SimpleNamespace
        from gptsalov import testnet_pilot as pilot
        from gptsalov.core import Plan
        from gptsalov.market import DemoFeed
        snap=DemoFeed().snapshot()
        state={**STATE,'seen_ms':None,'policy':dict(
            decision_engine='multiagent-testnet-v1',risk_model=VERSION)}
        book=SimpleNamespace(s=state,save=Mock())
        api=RiskExchange()
        signal=Signal('BTCUSDT',1,snap.bars['DEMOUSDT'][-1].close_ms,
                      dec(100),dec(98),dec(106),dec(1))
        sized=Plan(dec(100),dec(98),dec(106),dec('.09'),dec(9),dec('.21'),dec('.25'))
        with patch.object(pilot,'SYMBOL','BTCUSDT'), \
             patch.object(pilot,'now_ms',return_value=snap.now_ms), \
             patch.object(pilot,'closed_bars',return_value=snap.bars['DEMOUSDT']), \
             patch.object(pilot,'strategy',return_value=signal), \
             patch.object(pilot,'size',return_value=sized), \
             patch('gptsalov.multiagent_gate.evaluate',return_value=(True,
                 {'strategy_quality':{'atr':'1'}})) as gate:
            plan=pilot.candidate(api,Mock(),book)
        self.assertIsNotNone(plan)
        self.assertEqual(plan['risk_model'],VERSION)
        self.assertEqual(plan['leverage'],1)
        self.assertIn('last_risk_review',state)
        self.assertTrue(gate.call_args.kwargs['risk_aware'])
        self.assertFalse(any(x[0]=='POST' for x in api.calls))

    def test_quality_veto_survives_other_agent_agreement(self):
        from unittest.mock import patch
        from gptsalov.multiagent_gate import evaluate
        from types import SimpleNamespace
        result=dict(decision='RESEARCH_CANDIDATE',votes=[
            dict(agent=k,verdict=v) for k,v in [('data','PASS'),('risk','PASS'),
            ('volatility','PASS'),('trend','LONG'),('momentum','LONG')]])
        with patch('gptsalov.multiagent_gate.analyze',return_value=result), \
             patch('gptsalov.order_risk.strategy_review',return_value={
                 'allow':False,'reasons':['CHOPPY_TREND']}):
            allowed,review=evaluate([],{},0,SimpleNamespace(side=1),risk_aware=True)
        self.assertFalse(allowed)
        self.assertEqual(review['execution_gate'],'BLOCK')

from tests.conservative_limits import setUpModule, tearDownModule  # noqa: E402,F401
