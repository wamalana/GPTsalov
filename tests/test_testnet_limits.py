"""Pins the 2 USDT / 4% Testnet data-collection setting (real limits, no patching)."""
import unittest
from gptsalov import testnet_limits as L
from gptsalov.adaptive_risk import risk_allowance
from gptsalov.core import Config, Rules, Signal, dec
from gptsalov.execution_sizing import buffered_size, modeled_fill_risk
from gptsalov.order_risk import plan_order, risk_budget
from gptsalov.testnet_pilot import POLICY

BRACKETS = dict(symbol='XUSDT', brackets=[dict(notionalFloor=0, notionalCap=100000,
                maintMarginRatio='.01', initialLeverage=20)])
RULES = Rules('XUSDT', dec('.001'), dec('.001'), dec('.001'), dec(100000), dec(1), dec(1000000), dec('.001'), dec(1000000))


def st(equity='50', day='50', high='50', streak=0):
    return dict(equity=equity, day_start=day, high_water=high, loss_streak=streak)


class TwoUsdtSetting(unittest.TestCase):
    def test_values_and_policy_record_them(self):
        self.assertEqual((L.RISK_CAP, L.RISK_FRACTION, L.NOTIONAL_CAP), (dec(2), dec('.04'), dec(100)))
        self.assertEqual((POLICY['risk_cap'], POLICY['notional_cap'], POLICY['daily_loss'], POLICY['max_drawdown']),
                         ('2', '100', '0.10', '0.25'))

    def test_fresh_budget_is_two_and_shrinks_with_losses(self):
        self.assertEqual(risk_budget(st()), 2)
        self.assertEqual(risk_budget(st(equity='48', day='50')), dec('1.92'))       # one loss: 4% of 48
        self.assertEqual(risk_budget(st(equity='46', high='50')), dec('0.92'))      # dd 8%: halved
        self.assertEqual(risk_budget(st(equity='50', streak=2)), 1)                 # 2 losses in a row: halved
        self.assertEqual(risk_budget(st(equity='45.5', day='50', high='45.5')), dec('0.5'))  # only daily room left
        with self.assertRaises(ValueError):
            risk_budget(st(equity='45', day='50', high='45'))                       # daily 10% used up

    def test_allowance_matches(self):
        self.assertEqual(risk_allowance(st())[0], 2)
        self.assertEqual(risk_allowance(st(equity='46', day='50', high='50'))[0], 1)  # daily room 1

    def test_plan_reaches_about_two_usdt_within_caps(self):
        for stop_pct in ('2.5', '3', '4', '6'):
            d = dec(stop_pct)
            for side in (1, -1):
                p = plan_order(symbol='XUSDT', side=side, reference='100', stop=str(100-side*d),
                               target=str(100+side*2*d), atr=str(d/2), quantity_cap='1000', rules=RULES,
                               state=st(), available_balance='5000', brackets=BRACKETS)
                self.assertLessEqual(p['modeled_risk'], 2)
                self.assertLessEqual(p['notional'], L.NOTIONAL_CAP)
                self.assertLessEqual(p['leverage'], L.MAX_LEVERAGE)
                self.assertLessEqual(p['initial_margin'], dec(50)*L.CASH_FRACTION+dec('0.01'))
                if d >= 3:  # wide enough that notional cap does not bind
                    self.assertGreater(p['modeled_risk'], dec('1.8'))

    def test_fill_envelope_never_exceeds_two(self):
        cfg = Config(initial_equity=dec(50), max_notional_fraction=dec('.5'))
        for side in (-1, 1):
            sig = Signal('XUSDT', side, 0, dec(100), dec(100)-side*4, dec(100)+side*8, dec(1))
            p = buffered_size(sig, dec(100), dec(50), RULES, cfg, risk_cap=dec(2))
            for i in range(101):
                price = dec('99.5')+dec(i)/100
                self.assertLessEqual(modeled_fill_risk(p.qty, price, p.stop), 2)
                self.assertLessEqual(p.qty*price, L.NOTIONAL_CAP)
            self.assertGreater(modeled_fill_risk(p.qty, dec(100), p.stop), dec('1.5'))


class PrepareAcceptsNewLimits(unittest.TestCase):
    def test_real_wif_plan_from_2026_09_19_is_accepted(self):
        # WIFUSDT short that failed in production: qty 129.2, notional ~52 > old 25 cap.
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from gptsalov.order_risk import VERSION
        from gptsalov.testnet import Coordinator, Journal

        class Api:
            identity = 'acct'
        plan = dict(environment='testnet', side='SELL', symbol='WIFUSDT', quantity='129.2', reference='0.4020',
                    stop='0.4088', target='0.3884', risk_model=VERSION, leverage=1, purpose='MULTI_MARKET_TESTNET',
                    stop_model='atr-structure-v1', risk_cap='2')
        with TemporaryDirectory() as d, Journal(Path(d)/'t.db') as j:
            Coordinator(j, Api()).prepare(plan)
            self.assertEqual(Coordinator(j, Api()).plan()['symbol'], 'WIFUSDT')
        with TemporaryDirectory() as d, Journal(Path(d)/'t.db') as j:
            with self.assertRaisesRegex(ValueError, 'notional'):
                Coordinator(j, Api()).prepare({**plan, 'quantity': '300'})  # ~120 > 100


if __name__ == '__main__':
    unittest.main()
