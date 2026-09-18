import unittest
from types import SimpleNamespace
from unittest.mock import patch
from gptsalov.multiagent_gate import evaluate
from gptsalov.market import DemoFeed

class GateTests(unittest.TestCase):
    def result(self):
        return dict(decision='RESEARCH_CANDIDATE', votes=[
            dict(agent=k, verdict=v) for k,v in
            [('data','PASS'),('risk','PASS'),('volatility','PASS'),
             ('trend','SHORT'),('momentum','SHORT'),('news','ABSTAIN')]])

    def test_requires_agreement_and_all_safety_votes(self):
        for agent in ('data','risk','volatility','trend','momentum'):
            r=self.result()
            next(x for x in r['votes'] if x['agent']==agent)['verdict']='ABSTAIN'
            with self.subTest(agent=agent),patch('gptsalov.multiagent_gate.analyze',return_value=r):
                self.assertFalse(evaluate([],{},0,SimpleNamespace(side=-1))[0])

    def test_consensus_allows_only_matching_baseline(self):
        for side,expected in [(-1,True),(1,False)]:
            with patch('gptsalov.multiagent_gate.analyze',return_value=self.result()):
                self.assertEqual(evaluate([],{},0,SimpleNamespace(side=side))[0],expected)
        with patch('gptsalov.multiagent_gate.analyze',return_value=self.result()):
            self.assertFalse(evaluate([],{},0,None)[0])

    def test_real_analyzer_blocks_locked_stale_and_missing_data(self):
        snap=DemoFeed().snapshot()
        for lock,heartbeat in [('ORDER_REVIEW',snap.now_ms),(None,snap.now_ms-120001)]:
            state=dict(lock=lock,last_check_ms=heartbeat,phase='WAIT_SIGNAL',active=None,error=None)
            self.assertFalse(evaluate(snap.bars['DEMOUSDT'],state,snap.now_ms,SimpleNamespace(side=-1))[0])
        self.assertFalse(evaluate([],{},snap.now_ms,None)[0])

    def test_candidate_blocks_before_exchange_and_records_votes(self):
        from gptsalov import testnet_pilot as pilot
        from unittest.mock import Mock
        snap=DemoFeed().snapshot()
        state={'policy':{'decision_engine':'multiagent-testnet-v1'},'seen_ms':None}
        book=SimpleNamespace(s=state,save=Mock())
        api=Mock()
        public=Mock()
        with patch.object(pilot,'now_ms',return_value=snap.now_ms), \
             patch.object(pilot,'closed_bars',return_value=snap.bars['DEMOUSDT']), \
             patch.object(pilot,'strategy',return_value=SimpleNamespace(side=-1)), \
             patch('gptsalov.multiagent_gate.evaluate',return_value=(False,{'execution_gate':'BLOCK'})):
            self.assertIsNone(pilot.candidate(api,public,book))
        api.call.assert_not_called()
        self.assertEqual(state['last_agent_review']['execution_gate'],'BLOCK')
        self.assertEqual(book.save.call_args.args[0]['event'],'MULTIAGENT_REVIEW')
