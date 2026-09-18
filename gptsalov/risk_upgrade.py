"""Narrow, opt-in ledger policy upgrade; never resets trading history or locks."""
from copy import deepcopy
from .order_risk import VERSION


def upgrade_policy(state, target):
    old=state['policy']
    expected=deepcopy(old)
    if 'risk_model' in old or target.get('risk_model') != VERSION:
        raise ValueError('Unsupported risk policy transition')
    expected['risk_model']=VERSION
    if expected != target or old.get('environment') != 'testnet':
        raise ValueError('Risk upgrade must preserve all existing policy limits')
    if old.get('decision_engine') != 'multiagent-testnet-v1' or state.get('active'):
        raise ValueError('Risk upgrade requires idle multi-agent Testnet ledger')
    # Caller holds the Book writer lock; save() commits the new policy and event
    # together. Preserve equity, time budgets, counters, locks and history.
    state['policy']=deepcopy(target)
