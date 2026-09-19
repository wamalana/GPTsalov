"""Sizing with an explicit entry-price envelope shared with fill reconciliation."""
from dataclasses import replace, asdict
from types import SimpleNamespace
from .core import dec, floor_step, size

ENTRY_DRIFT=dec('.005')
from . import testnet_limits as L
ABS_RISK_CAP=L.RISK_CAP
NOTIONAL_CAP=L.NOTIONAL_CAP
RISK_HEADROOM=dec('.90')

def modeled_fill_risk(qty,entry,stop):
    q,e,s=map(dec,(qty,entry,stop))
    return q*(abs(e-s)+(e+s)*dec('.0008')+e*dec('.001'))

def buffered_size(signal,reference,equity,rules,cfg,risk_cap=None):
    reference,equity=dec(reference),dec(equity)
    cap=L.RISK_CAP if risk_cap is None else dec(risk_cap)
    if not 0<cap<=dec('2'): raise ValueError('INVALID_TESTNET_RISK_CAP')
    if risk_cap is not None:
        cfg=SimpleNamespace(**dict(asdict(cfg),risk_fraction=cap/equity,
            max_notional_fraction=L.NOTIONAL_CAP/equity,leverage=L.MAX_LEVERAGE))
    plan=size(signal,reference,equity,rules,cfg)
    low,high=reference*(1-ENTRY_DRIFT),reference*(1+ENTRY_DRIFT)
    # Both permitted prices must leave stop and target on the correct side.
    if signal.side==1:
        if low<=plan.stop or high>=plan.target:raise ValueError('ENTRY_ENVELOPE_CROSSES_STOP_OR_TARGET')
        worst=high
    else:
        if high>=plan.stop or low<=plan.target:raise ValueError('ENTRY_ENVELOPE_CROSSES_STOP_OR_TARGET')
        worst=low
    unit=max(modeled_fill_risk(1,low,plan.stop),modeled_fill_risk(1,high,plan.stop))
    budget=min(cap,equity*cfg.risk_fraction)*RISK_HEADROOM
    qty=floor_step(min(plan.qty,budget/unit,L.NOTIONAL_CAP/high),rules.step)
    if qty<=0 or qty<rules.min_qty or qty*low<rules.min_notional:
        raise ValueError('BELOW_EXCHANGE_MINIMUM_WITH_BUFFER')
    risk=unit*qty
    return replace(plan,entry=worst,qty=qty,notional=qty*worst,risk=risk,budget=budget)
