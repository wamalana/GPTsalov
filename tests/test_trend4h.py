import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gptsalov import trend4h
from gptsalov.backtest import VARIANTS, simulate, synthetic
from gptsalov.strategy_v2 import Series, V2Params, v2_signals

BAR = trend4h.BAR_MS


def series4h(n, seed):
    s = synthetic(n=n, seed=seed, vol=0.012)
    t0 = 1_700_000_000_000//BAR*BAR
    return Series([t0+i*BAR for i in range(n)], s.o, s.h, s.l, s.c, s.v, s.qv)


class FakeClient:
    def __init__(self, data):
        self.data, self.now = data, None

    def get(self, path, **params):
        if path == "/fapi/v1/time":
            return {"serverTime": self.now}
        if path == "/fapi/v1/exchangeInfo":
            return {"symbols": [{"symbol": k, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
                                 "marginAsset": "USDT", "underlyingType": "COIN", "onboardDate": 0} for k in self.data]}
        if path == "/fapi/v1/ticker/24hr":
            return [{"symbol": k, "quoteVolume": "1e9"} for k in self.data]
        s = self.data[params["symbol"]]
        rows = [[s.t[i], str(s.o[i]), str(s.h[i]), str(s.l[i]), str(s.c[i]), str(s.v[i]), s.t[i]+BAR-1, "0"]
                for i in range(len(s)) if s.t[i]+BAR-1 < self.now][-params["limit"]:]
        return rows


class ForwardParity(unittest.TestCase):
    def test_live_ledger_matches_backtest_simulation(self):
        n = 400
        data = {"BTCUSDT": series4h(n, 11), **{f"S{k}USDT": series4h(n, 20+k) for k in range(8)}}
        client = FakeClient(data)
        eng = VARIANTS[trend4h.VARIANT][2]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(trend4h, "LOOKBACK", n):
            path = Path(tmp)/"ledger.db"
            events = []
            for k in range(2, n+1):  # from the first closed bar, like the replay
                client.now = data["BTCUSDT"].t[k-1]+BAR+1000
                with trend4h.Ledger(path) as _:
                    pass
                ledger = trend4h.Ledger(path)
                try:
                    bars, bad = trend4h.fetch(client, sorted(data), client.now)
                    events += trend4h.step(ledger, bars, bad, client.now)
                    # polling again inside the same bar must be a no-op
                    self.assertEqual(trend4h.step(ledger, bars, bad, client.now+60_000), [])
                finally:
                    ledger.close()
            live = [d for _, k, d in events if k == "PAPER_CLOSE"]
            rep = trend4h.report(path, client.now)
        sigs = {s: v2_signals(x, trend4h.v2_params(), data["BTCUSDT"]) for s, x in data.items()}
        sim = simulate(data, sigs, eng).trades
        self.assertGreater(len(sim), 3)
        self.assertEqual(len(live), len(sim))
        for a, b in zip(live, sim):
            self.assertEqual((a["symbol"], a["side"], a["open_ms"], a["close_ms"], a["reason"]),
                             (b.symbol, b.side, b.open_ms, b.close_ms, b.reason))
            self.assertAlmostEqual(a["net"], b.net, places=9)
        self.assertEqual(rep["closed_trades"], len(sim))
        self.assertEqual(rep["execution"], "CANDLE_SIMULATOR_NO_REAL_ORDERS")

    def test_two_slots_one_per_side_and_shared_budget(self):
        n = 70
        data = {"BTCUSDT": series4h(n, 1), "S0USDT": series4h(n, 2), "S1USDT": series4h(n, 3), "S2USDT": series4h(n, 4)}
        client = FakeClient(data)
        from gptsalov.strategy_v2 import Sig

        def fake_signals(s, params, btc):  # long on S0/S2, short on S1, all on the last bar
            side = -1 if s.c[0] == data["S1USDT"].c[0] else 1
            if s.c[0] == data["BTCUSDT"].c[0]:
                return {}
            return {len(s)-1: Sig(side, s.c[-1], 3.0, None, 2.0, 1.0 if side == 1 else 0.5)}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(trend4h, "LOOKBACK", n), \
                mock.patch.object(trend4h, "v2_signals", fake_signals):
            path = Path(tmp)/"l.db"
            client.now = data["BTCUSDT"].t[n-2]+BAR+1000
            with trend4h.Ledger(path) as led:
                bars, bad = trend4h.fetch(client, sorted(data), client.now)
                ev = trend4h.step(led, bars, bad, client.now)
                intents = [d for _, k, d in ev if k == "PAPER_INTENT"]
                self.assertEqual([(x["sym"], x["sig"]["side"]) for x in intents], [("S0USDT", 1), ("S1USDT", -1)])
                self.assertAlmostEqual(sum(x["budget"] for x in intents), 100*0.005)
                client.now = data["BTCUSDT"].t[n-1]+BAR+1000
                bars, bad = trend4h.fetch(client, sorted(data), client.now)
                ev = trend4h.step(led, bars, bad, client.now)
                self.assertEqual(len(led.state["positions"]), 2)
                self.assertEqual(led.state["pending"], [])  # both slots busy: no third intent
                self.assertLessEqual(sum(p["risk"] for p in led.state["positions"]), 100*0.005+1e-9)

    def test_identity_mismatch_refuses_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"l.db"
            trend4h.Ledger(path).close()
            with mock.patch.object(trend4h, "VARIANT", "v3_4h_trail"):
                with self.assertRaisesRegex(ValueError, "identity"):
                    trend4h.Ledger(path)

    def test_second_writer_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"l.db"
            a = trend4h.Ledger(path)
            try:
                with self.assertRaises(RuntimeError):
                    trend4h.Ledger(path)
            finally:
                a.close()


if __name__ == "__main__":
    unittest.main()
