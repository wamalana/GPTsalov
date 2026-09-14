"""Isolated prospective paper portfolios; no credentials or real orders."""
from contextlib import ExitStack
from pathlib import Path
import argparse
import json
import time
from .core import Config, dec, encode
from .market import BinanceFeed, DemoFeed, MarketError, SnapshotExpired
from .paper import PaperEngine, Store, report
from .research import reward_risk

VARIANTS = {'baseline': None, 'rr1': '1', 'rr1_5': '1.5', 'rr2': '2'}

class FilterEngine(PaperEngine):
    def __init__(self, cfg, store, threshold):
        super().__init__(cfg, store)
        self.threshold = None if threshold is None else dec(threshold)

    def validate_entry(self, signal, plan):
        if self.threshold is None:
            return
        r = reward_risk(dict(entry=plan.entry, qty=plan.qty, target=plan.target,
            side=signal.side, entry_fee=plan.notional*self.cfg.fee_bps/10000,
            funding_reserve=plan.notional*self.cfg.funding_reserve_bps/10000,
            modeled_risk=plan.risk), self.cfg)
        if dec(r['net_rr']) < self.threshold:
            raise ValueError('NET_RR_BELOW_THRESHOLD')

def run(root, cfg, source, cycles, *, variants=None, engine_class=FilterEngine):
    variants = VARIANTS if variants is None else variants
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    identity = {'schema':1,'config_hash':cfg.fingerprint,'source':source,
                'variants':variants,'mode':'PROSPECTIVE_PAPER'}
    manifest = root/'experiment.json'
    if manifest.exists():
        if json.loads(manifest.read_text()) != identity:
            raise ValueError('Experiment identity mismatch')
    else:
        if any(root.iterdir()):
            raise ValueError('New experiment requires empty directory')
        with manifest.open('x') as f:
            f.write(encode(identity))
    feed = DemoFeed() if source == 'synthetic-demo' else BinanceFeed(cfg)
    with ExitStack() as stack:
        engines = {}
        for name, threshold in variants.items():
            path = root/(name+'.db')
            if path.is_symlink() or (path.exists() and path.stat().st_nlink != 1):
                raise ValueError('Research ledger alias refused')
            store = stack.enter_context(Store(str(path),cfg,source))
            engines[name] = engine_class(cfg,store,threshold)
        stamps = {e.store.state['last_close_ms'] for e in engines.values()}
        if len(stamps) != 1:
            raise ValueError('Partial portfolio cycle; manual review required')
        stamp = next(iter(stamps))
        if source == 'synthetic-demo' and stamp is not None:
            feed.index = (stamp-feed.start+1)//900000
        count = 0
        while cycles == 0 or count < cycles:
            required = sorted({s for e in engines.values() for s in e.required_symbols()})
            try:
                snap = feed.snapshot(required)
            except SnapshotExpired as exc:
                for e in engines.values():
                    e.fault(str(exc),int(time.time()*1000))
                print(encode({'status':'SKIPPED_EXPIRED_SNAPSHOT'}),flush=True)
            except MarketError as exc:
                for e in engines.values():
                    e.fault(str(exc),int(time.time()*1000))
                raise
            else:
                for name,e in engines.items():
                    events = e.step(snap)
                    if events or count % 20 == 0:
                        print(encode({'variant':name,'equity':e.store.state['equity'],
                                      'lock':e.store.state['hard_lock'],'events':events}),flush=True)
            count += 1
            if source != 'synthetic-demo' and (cycles == 0 or count < cycles):
                time.sleep(cfg.poll_seconds)

def status(root):
    root=Path(root)
    result={'experiment':json.loads((root/'experiment.json').read_text()),'portfolios':{},
            'warning':'Prospective candle simulation. Independent locks persist; no auto resets. No real orders.'}
    now=int(time.time()*1000)
    for name in result['experiment']['variants']:
        r=report(root/(name+'.db'),now_ms=now)
        result['portfolios'][name]={k:r[k] for k in ('equity','net_pnl_estimate',
            'closed_trades','position','pending','hard_lock','daily_locked',
            'drawdown_fraction','last_error','as_of_ms','observed_age_ms','event_counts')}
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['run','status'])
    p.add_argument('--directory',required=True)
    p.add_argument('--config')
    p.add_argument('--source',choices=['binance','demo'],default='binance')
    p.add_argument('--cycles',type=int,default=0)
    a=p.parse_args()
    if a.command=='status':
        print(encode(status(a.directory)))
        return
    if a.cycles<0 or (a.source=='demo' and not 1<=a.cycles<=380):
        p.error('Demo requires 1–380 cycles; Binance >=0')
    run(a.directory,Config.load(a.config),
        'synthetic-demo' if a.source=='demo' else 'binance-public',a.cycles)

if __name__=='__main__':
    main()
