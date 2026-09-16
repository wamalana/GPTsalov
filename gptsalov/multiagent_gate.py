"""Deterministic multi-agent execution gate for the bounded Testnet pilot."""
from .multiagent_shadow import analyze

def evaluate(bars, state, now, signal):
    result = analyze(bars, state, now)
    votes = {v['agent']: v['verdict'] for v in result['votes']}
    direction = 'LONG' if signal is not None and signal.side == 1 else 'SHORT'
    allowed = (signal is not None
        and result['decision'] == 'RESEARCH_CANDIDATE'
        and all(votes.get(k) == 'PASS' for k in ('data', 'risk', 'volatility'))
        and all(votes.get(k) == direction for k in ('trend', 'momentum')))
    result['execution_gate'] = 'ALLOW' if allowed else 'BLOCK'
    result['execution_policy'] = 'multiagent-testnet-v1'
    result['llm_role'] = 'advisory_only'
    return allowed, result
