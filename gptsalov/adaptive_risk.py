"""Testnet experiment: structural stops and evidence-gated risk ceilings."""
from dataclasses import replace
from .core import dec

VERSION='atr-structure-v1'
MAX_RISK=dec('2')

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
    """No promotion from win rate alone; compare two disjoint 30-trade windows."""
    samples=state.get('adaptive_results',[])
    cap=dec('.25');reason='NEED_60_CLOSED_TRADES'
    report={'closed_samples':len(samples),'version':VERSION}
    if len(samples)>=60:
        old=samples[-60:-30];new=samples[-30:]
        previous=sum(dec(x['net'])>0 for x in old)/30
        wins=sum(dec(x['net'])>0 for x in new)/30
        gains=sum((max(dec(x['net']),dec(0)) for x in new),dec(0))
        losses=sum((max(-dec(x['net']),dec(0)) for x in new),dec(0))
        expectancy=sum((dec(x['net'])/dec(x['risk']) for x in new),dec(0))/30
        eligible=wins>=previous+.10 and wins>=.5 and expectancy>0 and gains>losses*dec('1.3')
        report.update(win_rate=wins,previous_win_rate=previous,mean_r=str(expectancy),
            profit_factor=str(gains/losses) if losses else None)
        reason='PERFORMANCE_GATE_NOT_MET'
        if eligible:
            cap=dec('.5') if len(samples)<90 else dec('1') if len(samples)<120 else MAX_RISK
            reason='PERFORMANCE_GATE_PASSED'
    equity=dec(state['equity'])
    # Existing account-level loss limits take priority over the ceiling.
    daily=max(dec(0),equity-dec(state['day_start'])*dec('.98'))
    drawdown=max(dec(0),equity-dec(state['high_water'])*dec('.92'))
    allowed=min(cap,MAX_RISK,equity*dec('.04'),daily,drawdown)
    report.update(reason=reason,performance_cap=str(cap),effective_cap=str(allowed),max_cap=str(MAX_RISK))
    return allowed,report
