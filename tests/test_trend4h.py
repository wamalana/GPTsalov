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
        s = self.data[params["symbol"]]
        rows = [[s.t[i], str(s.o[i]), str(s.h[i]), str(s.l[i]), str(s.c[i]), str(s.v[i]), s.t[i]+BAR-1, "0"]
                for i in range(len(s)) if s.t[i]+BAR-1 < self.now][-params["limit"]:]
        return rows


class ForwardParity(unittest.TestCase):
    def test_live_ledger_matches_backtest_simulation(self):
        n = 400
        data = {"BTCUSDT": series4h(n, 11), "AUSDT": series4h(n, 12), "BUSDT": series4h(n, 13)}
        client = FakeClient(data)
        eng = VARIANTS["v3_4h_trail"][2]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(trend4h, "LOOKBACK", n), mock.patch.object(trend4h, "DEFAULT_SYMBOLS", tuple(data)):
            path = Path(tmp)/"ledger.db"
            events = []
            for k in range(80, n+1):
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
        sigs = {s: v2_signals(x, V2Params(), data["BTCUSDT"]) for s, x in data.items()}
        sim = simulate(data, sigs, eng).trades
        self.assertGreater(len(sim), 3)
        self.assertEqual(len(live), len(sim))
        for a, b in zip(live, sim):
            self.assertEqual((a["symbol"], a["side"], a["open_ms"], a["close_ms"], a["reason"]),
                             (b.symbol, b.side, b.open_ms, b.close_ms, b.reason))
            self.assertAlmostEqual(a["net"], b.net, places=9)
        self.assertEqual(rep["closed_trades"], len(sim))
        self.assertEqual(rep["execution"], "CANDLE_SIMULATOR_NO_REAL_ORDERS")

    def test_identity_mismatch_refuses_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"l.db"
            trend4h.Ledger(path).close()
            with mock.patch.object(trend4h, "VARIANT", "v3_4h_fixed3R"):
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
