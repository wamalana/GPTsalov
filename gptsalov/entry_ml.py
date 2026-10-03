"""Offline signal classifier. Read-only research DB; never imports an order client."""
import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

FEATURES = ('atr_fraction', 'volume_ratio', 'compression_ratio', 'hourly_direction',
            'ema_distance_atr', 'return_4bars', 'side', 'baseline', 'pullback', 'breakout')
EMBARGO_MS = 4 * 60 * 60 * 1000


def load_rows(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
        # Do not train on quick exits while long-duration outcomes are still pending.
        cutoff = int(time.time()*1000)-EMBARGO_MS
        pending = db.execute('''SELECT MIN(s.observed_ms) FROM signals s
          LEFT JOIN outcomes o ON s.id=o.id
          WHERE COALESCE(o.status,'') NOT IN ('CLOSED','EXPIRED','INVALID')
          AND s.observed_ms < ?''', (cutoff,)).fetchone()[0]
        if pending is not None:
            cutoff = min(cutoff, pending)
        records = db.execute('''SELECT s.id,s.variant,s.observed_ms,s.data,o.data
          FROM signals s JOIN outcomes o ON s.id=o.id WHERE o.status='CLOSED'
          AND s.observed_ms < ? ORDER BY s.observed_ms,s.id''', (cutoff,)).fetchall()
    rows, rejected = [], 0
    for identity, variant, observed, payload, outcome in records:
        try:
            data, result = json.loads(payload), json.loads(outcome)
            f = data['features']
            x = [float(f[k]) for k in FEATURES[:6]]
            x += [float(data['signal']['side']), float(variant=='baseline_v1'),
                  float(variant=='trend_pullback_v1'), float(variant=='compression_breakout_v1')]
            net = float(result['net'])
            end = int(result['exit_bar_ms'])+60000
            if not all(math.isfinite(v) for v in x+[net]) or end <= observed:
                raise ValueError('Invalid sample')
            rows.append(dict(id=identity, t=observed, end=end, x=x, net=net, y=int(net>0)))
        except (KeyError, ValueError, TypeError):
            rejected += 1
    return rows, rejected


def split_rows(rows):
    """Group equal observation times; purge full trade horizon before each boundary."""
    times = sorted({r['t'] for r in rows})
    if len(times)<5:
        return [], [], []
    a, b = times[int(len(times)*.6)], times[int(len(times)*.8)]
    train = [r for r in rows if r['t'] < a-EMBARGO_MS and r['end'] < a]
    validation = [r for r in rows if a <= r['t'] < b-EMBARGO_MS and r['end'] < b]
    test = [r for r in rows if r['t'] >= b]
    return train, validation, test


def probability(x, model):
    z = model['bias'] + sum(w*max(-10,min(10,(v-m)/s)) for w,v,m,s in
                            zip(model['weights'],x,model['mean'],model['scale']))
    return 1/(1+math.exp(-max(-35,min(35,z))))


def fit(rows):
    n = len(rows)
    mean = [sum(r['x'][j] for r in rows)/n for j in range(len(FEATURES))]
    scale = [max(1e-8,math.sqrt(sum((r['x'][j]-mean[j])**2 for r in rows)/n))
             for j in range(len(FEATURES))]
    xs = [[max(-10,min(10,(v-m)/s)) for v,m,s in zip(r['x'],mean,scale)] for r in rows]
    weights, bias = [0.0]*len(FEATURES), 0.0
    for _ in range(600):
        errors = []
        for x,r in zip(xs,rows):
            z = max(-35,min(35,bias+sum(w*v for w,v in zip(weights,x))))
            errors.append(1/(1+math.exp(-z))-r['y'])
        bias -= .05*sum(errors)/n
        weights = [w-.05*(sum(e*x[j] for e,x in zip(errors,xs))/n+.1*w)
                   for j,w in enumerate(weights)]
    return dict(kind='l2_logistic_v1', features=FEATURES, mean=mean, scale=scale,
                weights=weights, bias=bias, probability_calibrated=False)


def metrics(rows):
    wins = sum(max(0,r['net']) for r in rows)
    losses = -sum(min(0,r['net']) for r in rows)
    return dict(signals=len(rows), net_simulated_usdt=sum(r['net'] for r in rows),
                win_rate=sum(r['y'] for r in rows)/len(rows) if rows else None,
                profit_factor=wins/losses if losses else None)


def train(path):
    rows, rejected = load_rows(path)
    parts = split_rows(rows)
    result = dict(version=1, generated_ms=int(time.time()*1000), execution_enabled=False,
                  status='INSUFFICIENT_DATA', features=FEATURES, closed_samples=len(rows),
                  rejected_samples=rejected, embargo_ms=EMBARGO_MS,
                  splits={name:dict(count=len(part),first_ms=min((r['t'] for r in part),default=None),
                                    last_ms=max((r['t'] for r in part),default=None))
                          for name,part in zip(('train','validation','test'),parts)},
                  data_sha256=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest(),
                  limitations=['Retrospective chronological replay, not prospective model performance.',
                    'Overlapping signals are correlated; net sums are not portfolio returns.',
                    'Simulated costs and funding reserve; no exchange sizing or margin model.',
                    'Threshold selected on validation only; repeated training reuses the holdout.',
                    'No evidence of a 5 percent daily return; no automatic trading promotion.'])
    training, validation, test = parts
    if len(training)<40 or len({r['y'] for r in training})<2:
        result['reason']='Need >=40 purged training samples and both outcome classes'
        return result
    model = fit(training)
    result.update(status='TRAINED_RESEARCH_ONLY', model=model,
                  training=metrics(training), evaluation_status='INSUFFICIENT_DATA')
    if len(validation)<20 or len(test)<20:
        result['reason']='Model trained; need >=20 validation and >=20 test samples after embargo'
        return result
    options=[]
    for threshold in (.4,.5,.6,.7):
        kept=[r for r in validation if probability(r['x'],model)>=threshold]
        if len(kept)>=10:
            options.append((sum(r['net'] for r in kept),threshold))
    if not options:
        result['reason']='No validation threshold retains >=10 signals'
        return result
    _, threshold=max(options)
    selected=[r for r in test if probability(r['x'],model)>=threshold]
    result.update(evaluation_status='HOLDOUT_EVALUATED', threshold=threshold,
                  validation_baseline=metrics(validation),
                  validation_selected=metrics([r for r in validation if probability(r['x'],model)>=threshold]),
                  test_baseline=metrics(test),test_selected=metrics(selected),
                  test_brier=sum((probability(r['x'],model)-r['y'])**2 for r in test)/len(test))
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',required=True);p.add_argument('--output',required=True)
    args=p.parse_args()
    output=Path(args.output).resolve();source=Path(args.db).resolve()
    if output==source or output.suffix!='.json':
        p.error('Output must be a separate .json artifact')
    result=train(source)
    output.parent.mkdir(parents=True,exist_ok=True)
    temp=output.with_suffix('.json.tmp');temp.write_text(json.dumps(result,indent=2)+'\n');temp.replace(output)
    print(json.dumps(result))


if __name__=='__main__':
    main()
