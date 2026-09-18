"""Robustness sweep of the 4h candidate. Usage: PYTHONPATH=. python3 scripts/backtest_sweep.py DIR OUT.json"""
import json, statistics, sys
from gptsalov.backtest import *
root, out_path = sys.argv[1], sys.argv[2]
data = {s: x for s in DEFAULT_SYMBOLS if (x := load_klines(root, s))}
funding = {s: load_funding(root, s) for s in data}
split = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()*1000)
out = {}
for name in SWEEP:
    r = run_sweep_cell(name, data, funding)
    rep = split_report(r, split)
    rep["signals_generated"] = r.signal_count
    sub = run_sweep_cell(name, data, funding, cost_mult=1.5).trades
    rep["stress_avg_r"] = round(statistics.fmean(t.r for t in sub), 3) if sub else None
    out[name] = rep
    m, i, o = rep["all"], rep["in_sample"], rep["out_of_sample"]
    print(f"{name:16s} n={m.get('trades',0):4d} avgR={m.get('avg_r')} CI={m.get('avg_r_ci95')} PF={m.get('profit_factor')} "
          f"IS={i.get('avg_r')}({i.get('trades')}) OOS={o.get('avg_r')}({o.get('trades')}) x1.5={rep['stress_avg_r']} "
          f"eq={rep['final_equity']} dd={rep['max_drawdown']} maxpos={rep['max_concurrent_positions']}", flush=True)
json.dump(out, open(out_path, "w"), indent=1)
