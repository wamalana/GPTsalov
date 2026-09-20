from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
import test_testnet_pilot as pilot_tests
from gptsalov.testnet import ALGO, Rejected
from gptsalov.testnet_pilot import reconcile


class VerificationTests(unittest.TestCase):
    make = pilot_tests.PilotTests.make
    def test_delayed_target_get_never_reposts(self):
        with TemporaryDirectory() as d:
            api=pilot_tests.PilotExchange();c=self.make(d,api)
            original=api.call
            def delayed(method,path,**kw):
                if method=='GET' and path==ALGO and kw.get('clientAlgoId')==c.cid('target'):
                    raise Rejected(-2013)
                if method=='GET' and path=='/fapi/v1/openAlgoOrders':  # lag in both views
                    return [r for r in original(method,path,**kw) if r.get('clientAlgoId')!=c.cid('target')]
                return original(method,path,**kw)
            with patch.object(api,'call',side_effect=delayed),patch('gptsalov.testnet_pilot.now_ms',return_value=1000):
                self.assertEqual(reconcile(c)[0],'PROTECTED_TARGET_PENDING')
            with patch.object(api,'call',side_effect=delayed),patch('gptsalov.testnet_pilot.now_ms',return_value=31000):
                self.assertEqual(reconcile(c)[0],'PROTECTED_TARGET_REVIEW')
            self.assertEqual(reconcile(c)[0],'PROTECTED')
            posts=[x for x in api.calls if x[:2]==('POST',ALGO)]
            self.assertEqual(len(posts),2)

    def lagging_stop(self, c, api, hide_in_list):
        original=api.call
        def lag(method,path,**kw):
            if method=='GET' and path==ALGO and kw.get('clientAlgoId')==c.cid('stop'):
                raise Rejected(-2013)
            if hide_in_list and method=='GET' and path=='/fapi/v1/openAlgoOrders':
                return [r for r in original(method,path,**kw) if r.get('clientAlgoId')!=c.cid('stop')]
            return original(method,path,**kw)
        return lag

    def test_stop_lookup_lag_confirmed_by_open_orders_list(self):
        # 2026-09-19 GALAUSDT: stop ACKed, GET 32ms later returned -2013, pilot market-closed.
        with TemporaryDirectory() as d:
            api=pilot_tests.PilotExchange();c=self.make(d,api)
            with patch.object(api,'call',side_effect=self.lagging_stop(c,api,False)):
                self.assertEqual(reconcile(c)[0],'PROTECTED')
            self.assertEqual(c.j.get('stop_verification')['via'],'openAlgoOrders')
            self.assertIsNone(c.j.get('exit'))

    def test_stop_lag_waits_then_exits_after_grace(self):
        with TemporaryDirectory() as d:
            api=pilot_tests.PilotExchange();c=self.make(d,api)
            lag=self.lagging_stop(c,api,True)
            with patch.object(api,'call',side_effect=lag),patch('gptsalov.testnet_pilot.now_ms',return_value=1000):
                self.assertEqual(reconcile(c)[0],'STOP_PENDING_VISIBILITY')
            self.assertIsNone(c.j.get('exit'))
            with patch.object(api,'call',side_effect=lag),patch('gptsalov.testnet_pilot.now_ms',return_value=20000):
                self.assertEqual(reconcile(c)[0],'STOP_PENDING_VISIBILITY')
            with patch.object(api,'call',side_effect=lag),patch('gptsalov.testnet_pilot.now_ms',return_value=31001):
                self.assertEqual(reconcile(c)[0],'EXIT_RECONCILIATION')
            self.assertIsNotNone(c.j.get('exit'))
            stop_posts=[x for x in api.calls if x[:2]==('POST',ALGO) and x[2].get('clientAlgoId')==c.cid('stop')]
            self.assertEqual(len(stop_posts),1)  # never re-posts the stop

    def test_stop_mismatch_still_exits_immediately(self):
        with TemporaryDirectory() as d:
            api=pilot_tests.PilotExchange();c=self.make(d,api)
            original=api.call
            def wrong(method,path,**kw):
                r=original(method,path,**kw)
                if method=='GET' and path==ALGO and kw.get('clientAlgoId')==c.cid('stop'):
                    r=dict(r,triggerPrice='1')
                return r
            with patch.object(api,'call',side_effect=wrong):
                self.assertEqual(reconcile(c)[0],'EXIT_RECONCILIATION')

    def test_wrong_target_type_or_identity_refused(self):
        for field,value in [('orderType','STOP_MARKET'),('clientAlgoId','other'),('workingType','MARK_PRICE')]:
            with self.subTest(field=field),TemporaryDirectory() as d:
                api=pilot_tests.PilotExchange();c=self.make(d,api)
                reconcile(c)
                api.algos[c.cid('target')][field]=value
                self.assertEqual(reconcile(c)[0],'PROTECTED_TARGET_REVIEW')
                self.assertEqual(c.j.get('target_verification')['reason'],'MISMATCH')
