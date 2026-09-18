import unittest
from dataclasses import replace

from gptsalov.backtest import (Engine, VARIANTS, baseline_signals, metrics, run_variant,
                               simulate, synthetic)
from gptsalov.core import BAR_MS, Candle, strategy
from gptsalov.strategy_v2 import Series, Sig, V2Params, resample, v2_signals


def series(rows, start=0):
    """rows: (o, h, l, c, v) on consecutive 15m bars."""
    t = [start+i*BAR_MS for i in range(len(rows))]
    o, h, l, c, v = map(list, zip(*rows))
    return Series(t, o, h, l, c, v, [x*1e9 for x in v])


class BaselineParity(unittest.TestCase):
    def test_prefilter_never_drops_a_production_signal(self):
        s = synthetic(n=1400, seed=3)
        fast = baseline_signals("SYNUSDT", s)
        candles = [Candle(s.t[k], s.t[k]+BAR_MS-1, repr(s.o[k]), repr(s.h[k]), repr(s.l[k]),
                          repr(s.c[k]), repr(s.v[k])) for k in range(len(s))]
        exact = {i for i in range(198, len(s)) if strategy("SYNUSDT", candles[i-198:i+1])}
        self.assertTrue(exact)
        self.assertEqual(set(fast), exact)


class NoLookahead(unittest.TestCase):
    def test_future_bars_do_not_change_past_signals(self):
        s = synthetic(n=3000, seed=5)
        cut = 2000
        head = Series(*(getattr(s, k)[:cut] for k in ("t", "o", "h", "l", "c", "v", "qv")))
        self.assertEqual({i for i in baseline_signals("X", s) if i < cut}, set(baseline_signals("X", head)))
        h1, s1 = resample(head, 4, BAR_MS), resample(s, 4, BAR_MS)
        p = replace(V2Params(), btc_filter=False)
        full = {i: g for i, g in v2_signals(s1, p).items() if i < len(h1)}
        self.assertEqual(full, v2_signals(h1, p))


class Resample(unittest.TestCase):
    def test_hourly_aggregation_and_incomplete_group_dropped(self):
        s = series([(10, 12, 9, 11, 1), (11, 13, 10, 12, 2), (12, 12, 8, 9, 3), (9, 10, 9, 10, 4),
                    (10, 11, 10, 11, 5)])
        h = resample(s, 4, BAR_MS)
        self.assertEqual(len(h), 1)
        self.assertEqual((h.o[0], h.h[0], h.l[0], h.c[0], h.v[0]), (10, 13, 8, 10, 10))


class Execution(unittest.TestCase):
    def one_trade(self, bars, sig, eng=Engine(min_quote_volume_24h=0)):
        s = series(bars)
        return simulate({"AUSDT": s}, {"AUSDT": {0: sig}}, eng)

    def test_next_open_entry_and_stop_first_when_both_touch(self):
        sig = Sig(1, 100.0, 1.0, None, 1.0, 1.0, stop=99.0, target=102.0)
        r = self.one_trade([(100, 100, 100, 100, 1), (100, 103, 98, 101, 1)], sig)
        self.assertEqual(len(r.trades), 1)
        t = r.trades[0]
        self.assertEqual(t.reason, "STOP")
        self.assertAlmostEqual(t.entry, 100*1.0003)
        self.assertAlmostEqual(t.exit, 99*(1-0.0003))
        self.assertLess(t.r, -0.99)
        self.assertGreater(t.r, -1.01)

    def test_gap_through_stop_fills_at_worse_open(self):
        sig = Sig(1, 100.0, 1.0, None, 1.0, 1.0, stop=99.0, target=102.0)
        r = self.one_trade([(100, 100, 100, 100, 1), (100, 100.2, 99.9, 100, 1), (97, 97.5, 96, 97, 1)], sig)
        self.assertAlmostEqual(r.trades[0].exit, 97*(1-0.0003))
        self.assertLess(r.trades[0].r, -2.5)

    def test_short_target_net_of_costs(self):
        sig = Sig(-1, 100.0, 1.0, None, 1.0, 1.0, stop=101.0, target=98.0)
        r = self.one_trade([(100, 100, 100, 100, 1), (100, 100.5, 97.9, 98, 1)], sig)
        t = r.trades[0]
        self.assertEqual(t.reason, "TARGET")
        self.assertAlmostEqual(t.gross, (t.entry-t.exit)*t.qty)
        self.assertAlmostEqual(t.net, t.gross-t.costs)
        self.assertTrue(1.0 < t.r < 2.0)  # 2R gross shrinks after fees/slippage/reserve

    def test_reanchor_rejects_adverse_drift_and_cost_gate(self):
        eng = Engine(min_quote_volume_24h=0, reanchor=True, max_adverse_drift_r=0.25)
        sig = Sig(1, 100.0, 1.0, 2.0, 1.0, 1.0)
        r = self.one_trade([(100, 100, 100, 100, 1), (100.3, 100.4, 100.2, 100.3, 1)], sig, eng)
        self.assertEqual(r.rejects, {"ADVERSE_DRIFT": 1})
        eng = replace(eng, cost_gate=0.25)  # 26 bps round trip vs 0.5% stop -> 52% > 25%
        r = self.one_trade([(100, 100, 100, 100, 1), (100, 100, 100, 100, 1)], Sig(1, 100.0, 0.5, 2.0, 1, 1), eng)
        self.assertEqual(r.rejects, {"COST_GATE": 1})


class RandomWalkSanity(unittest.TestCase):
    def test_no_edge_data_loses_roughly_its_costs(self):
        data = {f"S{k}USDT": synthetic(n=5000, seed=40+k) for k in range(4)}
        m = metrics(run_variant("v1_baseline", data).trades)
        self.assertGreater(m["trades"], 100)
        self.assertLess(m["avg_r"], 0)
        self.assertLess(m["avg_r_ci95"][0], 0)

    def test_all_variants_run(self):
        data = {"BTCUSDT": synthetic(n=3000, seed=1), "S1USDT": synthetic(n=3000, seed=2)}
        for name in VARIANTS:
            run_variant(name, data)


if __name__ == "__main__":
    unittest.main()
