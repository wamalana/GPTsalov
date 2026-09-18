"""Deterministic multi-agent execution gate for the bounded Testnet pilot."""
from .multiagent_shadow import analyze

def evaluate(bars, state, now, signal, symbol='ETHUSDT', risk_aware=False):
    result = analyze(bars, state, now, symbol=symbol)
    votes = {v['agent']: v['verdict'] for v in result['votes']}
    direction = 'LONG' if signal is not None and signal.side == 1 else 'SHORT'
    allowed = (signal is not None
        and result['decision'] == 'RESEARCH_CANDIDATE'
        and all(votes.get(k) == 'PASS' for k in ('data', 'risk', 'volatility'))
        and all(votes.get(k) == direction for k in ('trend', 'momentum')))
    result['execution_gate'] = 'ALLOW' if allowed else 'BLOCK'
    result['execution_policy'] = 'multiagent-testnet-v1'
    result['llm_role'] = 'advisory_only'
    if risk_aware and signal is not None:
        from .order_risk import strategy_review
        quality = strategy_review(bars, signal)
        result['strategy_quality'] = quality
        result['votes'].append(dict(agent='strategy_quality',
            verdict='PASS' if quality['allow'] else 'VETO',
            reason=','.join(quality['reasons']) or 'TREND_BREAKOUT_QUALITY'))
        allowed = allowed and quality['allow']
        result['execution_gate'] = 'ALLOW' if allowed else 'BLOCK'
    return allowed, result
