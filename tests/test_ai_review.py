from pathlib import Path
from tempfile import TemporaryDirectory
import json
import time
import unittest
from gptsalov.ai_review import review,validate


class AITests(unittest.TestCase):
    def setup_files(self,d):
        cfg=Path(d)/'openai.json'
        cfg.write_text(json.dumps(dict(api_key='fake',model='test-model',daily_calls=1)))
        cfg.chmod(0o600)
        snap=dict(observed_ms=int(time.time()*1000),version='test',candle_close_ms=1,decision='BLOCKED')
        return snap,cfg,Path(d)/'ledger.db'

    def test_timeout_consumes_budget_and_never_retries(self):
        with TemporaryDirectory() as d:
            snap,cfg,ledger=self.setup_files(d);calls=[]
            def fail(*args):
                calls.append(1);raise TimeoutError('secret must not appear')
            first=review(snap,cfg,ledger,fail)
            self.assertEqual(first['status'],'FAILED')
            self.assertNotIn('secret',json.dumps(first))
            self.assertEqual(review(snap,cfg,ledger,fail),first)
            snap['candle_close_ms']=2
            self.assertEqual(review(snap,cfg,ledger,fail)['status'],'DAILY_LIMIT')
            self.assertEqual(len(calls),1)

    def test_ai_does_not_override_block(self):
        with TemporaryDirectory() as d:
            snap,cfg,ledger=self.setup_files(d)
            def success(*args):
                return dict(status='completed',output=[dict(content=[dict(type='output_text',text=json.dumps(dict(stance='LONG',reason='test',evidence_agents=['trend'])))])])
            self.assertEqual(review(snap,cfg,ledger,success)['status'],'COMPLETED')
            self.assertEqual(snap['decision'],'BLOCKED')

    def test_invalid_and_refusal(self):
        for response in ({'status':'incomplete'},dict(status='completed',output=[dict(content=[dict(type='refusal')])]),dict(status='completed',output=[])):
            with self.assertRaises(ValueError):validate(response)

    def test_config_permissions_and_stale(self):
        with TemporaryDirectory() as d:
            snap,cfg,ledger=self.setup_files(d)
            snap['observed_ms']=0
            self.assertEqual(review(snap,cfg,ledger)['status'],'STALE')
            cfg.chmod(0o644)
            with self.assertRaises(ValueError):review(snap,cfg,ledger)
