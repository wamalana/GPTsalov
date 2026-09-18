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
                return original(method,path,**kw)
            with patch.object(api,'call',side_effect=delayed),patch('gptsalov.testnet_pilot.now_ms',return_value=1000):
                self.assertEqual(reconcile(c)[0],'PROTECTED_TARGET_PENDING')
            with patch.object(api,'call',side_effect=delayed),patch('gptsalov.testnet_pilot.now_ms',return_value=31000):
                self.assertEqual(reconcile(c)[0],'PROTECTED_TARGET_REVIEW')
            self.assertEqual(reconcile(c)[0],'PROTECTED')
            posts=[x for x in api.calls if x[:2]==('POST',ALGO)]
            self.assertEqual(len(posts),2)

    def test_wrong_target_type_or_identity_refused(self):
        for field,value in [('orderType','STOP_MARKET'),('clientAlgoId','other'),('workingType','MARK_PRICE')]:
            with self.subTest(field=field),TemporaryDirectory() as d:
                api=pilot_tests.PilotExchange();c=self.make(d,api)
                reconcile(c)
                api.algos[c.cid('target')][field]=value
                self.assertEqual(reconcile(c)[0],'PROTECTED_TARGET_REVIEW')
                self.assertEqual(c.j.get('target_verification')['reason'],'MISMATCH')
