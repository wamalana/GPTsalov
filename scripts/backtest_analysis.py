"""Run all variants on real data, then extra diagnostics. Usage: python3 analyze.py DIR"""
import json, random, statistics, sys, collections
from gptsalov.backtest import *
root = sys.argv[1]
data = {s: x for s in DEFAULT_SYMBOLS if (x := load_klines(root, s))}
funding = {s: load_funding(root, s) for s in data}
split = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()*1000)
out = {"symbols": {s: [len(x), x.t[0], x.t[-1]] for s, x in data.items()}, "variants": {}, "stress": {}}
trades = {}
for name in VARIANTS:
    r = run_variant(name, data, funding)
    trades[name] = r.trades
    rep = split_report(r, split)
    # per-symbol and per-quarter breakdown
    by_sym = collections.defaultdict(list); by_q = collections.defaultdict(list); by_hour = collections.defaultdict(list)
    for t in r.trades:
        by_sym[t.symbol].append(t.r)
        d = datetime.fromtimestamp(t.open_ms/1000, timezone.utc)
        by_q[f"{d.year}Q{(d.month-1)//3+1}"].append(t.r)
        by_hour[d.hour//4*4].append(t.r)
    rep["by_symbol"] = {k: [len(v), round(statistics.fmean(v), 3)] for k, v in sorted(by_sym.items())}
    rep["by_quarter"] = {k: [len(v), round(statistics.fmean(v), 3)] for k, v in sorted(by_q.items())}
    rep["by_utc_4h"] = {k: [len(v), round(statistics.fmean(v), 3)] for k, v in sorted(by_hour.items())}
    if r.trades:
        best = max(by_sym, key=lambda k: sum(by_sym[k]))
        rest = [t.r for t in r.trades if t.symbol != best]
        rep["avg_r_without_best_symbol"] = [best, round(statistics.fmean(rest), 3) if rest else None]
        rep["avg_gross_r"] = round(statistics.fmean(t.gross/t.risk for t in r.trades), 3)
        rep["avg_cost_r"] = round(statistics.fmean(t.costs/t.risk for t in r.trades), 3)
    out["variants"][name] = rep
    m = rep["all"]
    print(f"{name:18s} sig={rep['signals_generated']} n={m.get('trades',0)} win={m.get('win_rate')} avgR={m.get('avg_r')} CI={m.get('avg_r_ci95')} grossR={rep.get('avg_gross_r')} costR={rep.get('avg_cost_r')} PF={m.get('profit_factor')} eq={rep['final_equity']} dd={rep['max_drawdown']}", flush=True)
for name in VARIANTS:
    r = run_variant(name, data, funding, cost_mult=1.5)
    out["stress"][name] = metrics(r.trades)
# adaptive_risk promotion gate under each variant's own empirical R distribution (no edge change)
def gate(sample_r, p_boot=4000, seed=3):
    rnd = random.Random(seed); hits = 0
    for _ in range(p_boot):
        old = rnd.choices(sample_r, k=30); new = rnd.choices(sample_r, k=30)
        w_old = sum(x > 0 for x in old)/30; w_new = sum(x > 0 for x in new)/30
        g = sum(x for x in new if x > 0); l = -sum(x for x in new if x < 0)
        if w_new >= w_old+.10 and w_new >= .5 and statistics.fmean(new) > 0 and g > l*1.3:
            hits += 1
    return hits/p_boot
out["risk_promotion_gate_pass_prob"] = {n: round(gate([t.r for t in ts]), 4) for n, ts in trades.items() if len(ts) > 30}
json.dump(out, open("analysis.json", "w"), indent=1)
print(json.dumps({"stress": {k: [v.get("trades"), v.get("avg_r"), v.get("avg_r_ci95")] for k, v in out["stress"].items()},
                  "gate": out["risk_promotion_gate_pass_prob"]}, indent=1))
