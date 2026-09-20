"""2026-09-19 UNIUSDT: leverage ACK not yet visible in symbolConfig -> ValueError
before entry -> pilot locked API_OR_STATE_REVIEW for 14h with a flat account."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

import test_testnet_pilot as pilot_tests
import test_order_risk as order_risk_tests
RiskExchange=order_risk_tests.RiskExchange
from gptsalov.testnet import ORDER
from gptsalov.testnet_pilot import reconcile


class LaggingLeverage(RiskExchange):
    """Accepts the POST but symbolConfig shows the old value for `lag` reads."""
    def __init__(self, lag):
        super().__init__(); self.lag=lag; self.shown=None
    def call(self, method, path, **params):
        if method=='GET' and path.endswith('symbolConfig') and self.lag>0 and self.shown is not None:
            self.lag-=1
            real=self.leverage; self.leverage=self.shown
            try: return super().call(method, path, **params)
            finally: self.leverage=real
        if method=='POST' and path.endswith('/leverage'):
            self.shown=self.leverage
        return super().call(method, path, **params)


class LeverageVisibility(unittest.TestCase):
    prepare=order_risk_tests.ExecutionRiskTests.prepare

    def test_short_lag_is_absorbed_without_second_post(self):
        with TemporaryDirectory() as d, patch('gptsalov.testnet.time.sleep') as sleep:
            api=LaggingLeverage(lag=2); c=self.prepare(d, api)
            self.assertEqual(c.advance(), 'PROTECTED')
            self.assertEqual(sum(x[:2]==('POST','/fapi/v1/leverage') for x in api.calls), 1)
            self.assertTrue(sleep.called)

    def test_persistent_lag_still_refuses_entry(self):
        with TemporaryDirectory() as d, patch('gptsalov.testnet.time.sleep'):
            api=LaggingLeverage(lag=99); c=self.prepare(d, api)
            with self.assertRaises(ValueError): c.advance()
            self.assertFalse(any(x[:2]==('POST', ORDER) for x in api.calls))


class PreEntryAbort(unittest.TestCase):
    make=pilot_tests.PilotTests.make

    def test_failed_preflight_is_a_skipped_trade_not_a_lock(self):
        with TemporaryDirectory() as d:
            api=pilot_tests.PilotExchange(); c=self.make(d, api)
            with patch.object(c, 'preflight', side_effect=ValueError('Dynamic leverage not confirmed')):
                phase, reason=reconcile(c)
                self.assertEqual(phase, 'ABORTED_BEFORE_ENTRY')
                self.assertEqual(reason, 'Dynamic leverage not confirmed')
                self.assertEqual(reconcile(c)[0], 'ABORTED_BEFORE_ENTRY')  # idempotent
            self.assertIsNone(c.j.get('entry'))
            self.assertEqual(c.j.get('aborted')['reason'], 'Dynamic leverage not confirmed')
            self.assertFalse(any(x[:2]==('POST', ORDER) for x in api.calls))

    def test_after_entry_post_errors_still_propagate(self):
        # Once 'entry' is journaled the order may exist: never silently drop the slot.
        with TemporaryDirectory() as d:
            api=pilot_tests.PilotExchange(); c=self.make(d, api)
            self.assertEqual(reconcile(c)[0], 'PROTECTED')
            with patch.object(c, 'preflight', side_effect=ValueError('x')):
                self.assertNotEqual(reconcile(c)[0], 'ABORTED_BEFORE_ENTRY')


from tests.conservative_limits import setUpModule, tearDownModule  # noqa: E402,F401


if __name__=='__main__':
    unittest.main()
