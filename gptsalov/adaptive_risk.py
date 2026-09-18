"""Testnet experiment: structural stops and evidence-gated risk ceilings."""
from dataclasses import replace
from .core import dec

VERSION='atr-structure-v1'
MAX_RISK=dec('.25')  # escalation disabled; see risk_allowance

def adaptive_stop(signal,bars):
    if signal is None or len(bars)<15: raise ValueError('STOP_INPUT')
    atr=sum((max(bars[i].high-bars[i].low,abs(bars[i].high-bars[i-1].close),
        abs(bars[i].low-bars[i-1].close)) for i in range(len(bars)-14,len(bars))),dec(0))/14
    if atr<=0: raise ValueError('STOP_ATR')
    ref=signal.reference
    swing=min(b.low for b in bars[-6:-1]) if signal.side==1 else max(b.high for b in bars[-6:-1])
    structural=swing-signal.side*atr*dec('.25')
    distance=max(atr*2,signal.side*(ref-structural),abs(ref-signal.stop))
    if distance/ref>dec('.06'): raise ValueError('STRUCTURAL_STOP_TOO_WIDE')
    stop=ref-signal.side*distance
    target=ref+signal.side*distance*2
    if min(stop,target)<=0: raise ValueError('STOP_PRICE')
    return replace(signal,stop=stop,target=target,reason=signal.reason+':'+VERSION),{
        'version':VERSION,'atr14':str(atr),'swing5':str(swing),
        'distance':str(distance),'distance_pct':str(distance/ref*100)}

def risk_allowance(state):
    """Fixed 0.25 USDT ceiling. Performance-based promotion is DISABLED.

    The former 30-trade win-rate gate passed by chance 4-8% of the time on a
    strategy with negative expectancy (research/backtest-2026-09-18), and was
    re-checked after every trade, so escalation to 2 USDT was near-certain
    without any real edge. Re-enable only for a strategy that has passed the
    out-of-sample criteria in STRATEGY_V2.md. The report keeps the sample
    statistics for review; they no longer change the cap.
    """
    samples=state.get('adaptive_results',[])
    cap=dec('.25');reason='PROMOTION_DISABLED_PENDING_VALIDATED_EDGE'
    report={'closed_samples':len(samples),'version':VERSION}
    if len(samples)>=60:
        new=samples[-30:]
        wins=sum(dec(x['net'])>0 for x in new)/30
        gains=sum((max(dec(x['net']),dec(0)) for x in new),dec(0))
        losses=sum((max(-dec(x['net']),dec(0)) for x in new),dec(0))
        expectancy=sum((dec(x['net'])/dec(x['risk']) for x in new),dec(0))/30
        report.update(win_rate=wins,mean_r=str(expectancy),
            profit_factor=str(gains/losses) if losses else None)
    equity=dec(state['equity'])
    # Existing account-level loss limits take priority over the ceiling.
    daily=max(dec(0),equity-dec(state['day_start'])*dec('.98'))
    drawdown=max(dec(0),equity-dec(state['high_water'])*dec('.92'))
    # 0.5% of equity per trade: the same rule core.Config enforces for paper.
    allowed=min(cap,equity*dec('.005'),daily,drawdown)
    report.update(reason=reason,performance_cap=str(cap),effective_cap=str(allowed),max_cap=str(cap))
    return allowed,report
