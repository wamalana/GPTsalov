"""Prospective, separately controlled entry-quality experiments. No live orders."""
import argparse
from .core import Config, dec, encode
from .forward import FilterEngine, run, status
from .research import reward_risk

# Predeclared round hypotheses, not fitted estimates or probability claims.
POLICIES = {
    'rr1_control': {'min_rr':'1','max_cost_share':None,'min_stop_fraction':None},
    'rr1_cost25': {'min_rr':'1','max_cost_share':'0.25','min_stop_fraction':None},
    'rr1_geometry50': {'min_rr':'1','max_cost_share':None,'min_stop_fraction':'0.5'},
}

def metrics(signal, plan, cfg):
    assessment=reward_risk(dict(entry=plan.entry,qty=plan.qty,target=plan.target,
        side=signal.side,entry_fee=plan.notional*cfg.fee_bps/10000,
        funding_reserve=plan.notional*cfg.funding_reserve_bps/10000,
        modeled_risk=plan.risk),cfg)
    gross=dec(assessment['target_gross_pnl'])
    original=abs(signal.reference-signal.stop)
    if gross<=0 or original<=0:
        raise ValueError('INVALID_ENTRY_GEOMETRY')
    return dict(net_rr=assessment['net_rr'],
        cost_share=str((gross-dec(assessment['net_reward']))/gross),
        stop_fraction=str(abs(plan.entry-plan.stop)/original))

class TuningEngine(FilterEngine):
    def __init__(self,cfg,store,policy):
        self.policy=dict(policy)
        super().__init__(cfg,store,policy['min_rr'])

    def validate_entry(self,signal,plan):
        super().validate_entry(signal,plan)
        m=metrics(signal,plan,self.cfg)
        if self.policy['max_cost_share'] is not None and dec(m['cost_share'])>dec(self.policy['max_cost_share']):
            raise ValueError('TARGET_COST_SHARE_TOO_HIGH')
        if self.policy['min_stop_fraction'] is not None and dec(m['stop_fraction'])<dec(self.policy['min_stop_fraction']):
            raise ValueError('STOP_DISTANCE_COMPRESSED')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('run','status'))
    p.add_argument('--directory',required=True)
    p.add_argument('--config')
    p.add_argument('--source',choices=('demo','binance'),default='binance')
    p.add_argument('--cycles',type=int,default=0)
    a=p.parse_args()
    if a.command=='status':
        print(encode(status(a.directory)))
        return
    if a.cycles<0 or (a.source=='demo' and not 1<=a.cycles<=380):
        p.error('Demo requires 1–380 cycles; Binance >=0')
    run(a.directory,Config.load(a.config),
        'synthetic-demo' if a.source=='demo' else 'binance-public',a.cycles,
        variants=POLICIES,engine_class=TuningEngine)

if __name__=='__main__':
    main()
