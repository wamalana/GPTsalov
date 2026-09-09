from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from gptsalov.core import BAR_MS, Candle, Config, D, Signal
from gptsalov.market import DemoFeed, MarketError, Snapshot, demo_rule
from gptsalov.paper import PaperEngine, Store, day_at, report


START = 1783468800000


def snapshot(i=0, signals=False, **prices):
    start = START+i*BAR_MS
    values = {"open": D("10"), "high": D("10.01"), "low": D("9.99"), "close": D("10"), "volume": D("100")}
    values.update({k: D(str(v)) for k,v in prices.items()})
    bar = Candle(start, start+BAR_MS-1, **values)
    sig = Signal("DEMOUSDT", 1, bar.close_ms, D("10"), D("9.9"), D("10.2"), D(1))
    return Snapshot(bar.close_ms+1, "synthetic-demo", {"DEMOUSDT": demo_rule()},
                    {"DEMOUSDT": [bar]}, [sig] if signals else [], {})


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name)/"paper.db")
        self.cfg = Config()
        self.store = Store(self.path, self.cfg, "synthetic-demo")
        self.engine = PaperEngine(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def open_position(self):
        self.engine.step(snapshot(0, signals=True))
        self.engine.step(snapshot(1))
        self.engine.step(snapshot(2))
        self.assertIsNotNone(self.store.state["position"])

    def test_signal_commits_future_intent_no_lookahead_fill(self):
        self.engine.step(snapshot(0, signals=True))
        self.assertIsNone(self.store.state["position"])
        pending = self.store.state["pending"]
        self.assertGreater(pending["execute_open_ms"], pending["observed_ms"])
        self.engine.step(snapshot(1))
        self.assertIsNone(self.store.state["position"])
        self.engine.step(snapshot(2))
        self.assertIsNotNone(self.store.state["position"])

    def test_same_snapshot_idempotent(self):
        snap = snapshot(0, signals=True)
        self.engine.step(snap)
        self.engine.step(snap)
        self.assertEqual(report(self.path)["event_counts"]["PAPER_INTENT"], 1)

    def test_same_entry_bar_not_double_open(self):
        self.open_position()
        self.engine.step(snapshot(2))
        self.assertEqual(report(self.path)["event_counts"]["PAPER_OPEN"], 1)
        self.assertEqual(self.store.state["position"]["bars_held"], 1)

    def test_stop_before_target_and_cost_accounting(self):
        self.open_position()
        self.engine.step(snapshot(3, low="9.8", high="10.3"))
        r = report(self.path)
        self.assertIsNone(r["position"])
        trade = r["recent_trades"][-1]
        self.assertEqual(trade["reason"], "STOP_FIRST_CONSERVATIVE")
        self.assertLess(D(trade["net_pnl"]), 0)
        self.assertEqual(D(r["balance"])-D(100), D(trade["net_pnl"]))
        self.assertEqual(D(r["balance"])-D(100), D(r["gross_realized"])-D(r["fees"])-D(r["funding_reserve_charged"]))

    def test_stop_gap_can_exceed_risk(self):
        self.open_position()
        budget = D(self.store.state["position"]["modeled_risk"])
        self.engine.step(snapshot(3, open="8", high="8.1", low="7.9", close="8"))
        trade = report(self.path)["recent_trades"][-1]
        self.assertLess(D(trade["exit"]), D("8"))
        self.assertGreater(-D(trade["net_pnl"]), budget)

    def test_restart_keeps_state(self):
        self.open_position()
        prior = self.store.state
        self.store.close()
        self.store = Store(self.path, self.cfg, "synthetic-demo")
        self.assertEqual(prior, self.store.state)

    def test_lock_blocks_second_writer(self):
        with self.assertRaisesRegex(RuntimeError, "Another writer"):
            Store(self.path, self.cfg, "synthetic-demo")

    def test_config_change_refused(self):
        self.store.close()
        with self.assertRaisesRegex(ValueError, "config mismatch"):
            Store(self.path, replace(self.cfg, fee_bps=D(6)), "synthetic-demo")
        self.store = Store(self.path, self.cfg, "synthetic-demo")

    def test_source_change_refused(self):
        self.store.close()
        with self.assertRaisesRegex(ValueError, "mix"):
            Store(self.path, self.cfg, "binance-public")
        self.store = Store(self.path, self.cfg, "synthetic-demo")

    def test_gap_with_open_position_halts_without_inventing_fill(self):
        self.open_position()
        asof = self.store.state["as_of_ms"]
        with self.assertRaisesRegex(MarketError, "DATA_GAP"):
            self.engine.step(snapshot(5))
        self.assertIsNotNone(self.store.state["position"])
        self.assertEqual(self.store.state["as_of_ms"], asof)
        self.assertEqual(self.store.state["hard_lock"], "DATA_GAP_REQUIRES_RECONCILIATION")

    def test_flat_gap_discards_pending(self):
        self.engine.step(snapshot(0, signals=True))
        self.engine.step(snapshot(5))
        self.assertIsNone(self.store.state["pending"])
        self.assertIsNone(self.store.state["position"])

    def test_stale_signal_refused(self):
        snap = snapshot(0, signals=True)
        snap.now_ms += 200_000
        self.engine.step(snap)
        self.assertIsNone(self.store.state["pending"])

    def test_data_error_preserves_asof(self):
        self.engine.step(snapshot())
        asof = self.store.state["as_of_ms"]
        self.engine.fault("offline", START+100*BAR_MS)
        self.assertEqual(self.store.state["as_of_ms"], asof)
        self.assertEqual(self.store.state["last_error"], "offline")

    def test_out_of_order_refused(self):
        self.engine.step(snapshot(2))
        with self.assertRaisesRegex(MarketError, "OUT_OF_ORDER"):
            self.engine.step(snapshot(1))

    def test_missing_position_data_refused(self):
        self.open_position()
        snap = snapshot(3)
        snap.bars = {}
        with self.assertRaisesRegex(MarketError, "MISSING_POSITION"):
            self.engine.step(snap)

    def test_daily_lock_persists_until_new_day(self):
        self.open_position()
        self.engine.step(snapshot(3, open="9", high="9.1", low="8.9", close="9"))
        self.assertTrue(self.store.state["daily_locked"])
        self.engine.step(snapshot(4, signals=True))
        self.assertIsNone(self.store.state["pending"])
        self.assertTrue(self.store.state["daily_locked"])
        self.engine.step(snapshot(100))
        self.assertFalse(self.store.state["daily_locked"])

    def test_drawdown_lock_persists_new_day(self):
        self.open_position()
        self.engine.step(snapshot(3, open="7", high="7.1", low="6.9", close="7"))
        self.assertEqual(self.store.state["hard_lock"], "DRAWDOWN_REVIEW")
        self.engine.step(snapshot(100, signals=True))
        self.assertEqual(self.store.state["hard_lock"], "DRAWDOWN_REVIEW")
        self.assertIsNone(self.store.state["pending"])

    def test_loss_streak_review(self):
        for first in (0, 5, 10):
            self.engine.step(snapshot(first, signals=True))
            self.engine.step(snapshot(first+1))
            self.engine.step(snapshot(first+2))
            self.engine.step(snapshot(first+3, low="9.8"))
            self.engine.step(snapshot(first+4))
        self.assertEqual(self.store.state["hard_lock"], "LOSS_STREAK_REVIEW")

    def test_timeout_exit(self):
        self.open_position()
        for i in range(3, 18):
            self.engine.step(snapshot(i))
        self.assertEqual(report(self.path)["recent_trades"][-1]["reason"], "TIME_EXIT")

    def test_bangkok_midnight(self):
        # 2026-07-08 17:00 UTC is next day in Bangkok.
        from datetime import datetime, timezone
        t = int(datetime(2026, 7, 8, 17, tzinfo=timezone.utc).timestamp()*1000)
        self.assertEqual(day_at(t), "2026-07-09")
        self.assertEqual(day_at(t-1), "2026-07-08")

    def test_full_demo_end_to_end(self):
        feed = DemoFeed()
        for _ in range(180):
            self.engine.step(feed.snapshot())
        r = report(self.path)
        self.assertGreater(r["closed_trades"], 0)
        self.assertFalse(r["live_trading_supported"])
        self.assertEqual(D(r["balance"]), D("99.449089652360"))

    def test_sql_failure_rolls_back_state_and_events(self):
        import sqlite3
        self.store.db.execute("CREATE TRIGGER abort_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'test_failure'); END")
        self.store.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.step(snapshot(0, signals=True))
        self.assertIsNone(report(self.path)["pending"])
        self.assertIsNone(self.store.state["pending"])
        self.assertFalse(report(self.path)["event_counts"])

    def test_report_error_not_healthy(self):
        self.engine.step(snapshot())
        self.engine.fault("offline", START+BAR_MS)
        self.assertEqual(report(self.path)["health"], "ERROR_MARKS_MAY_BE_STALE")

    def test_short_stop_and_target_accounting(self):
        snap = snapshot(0, signals=True)
        snap.signals = [replace(snap.signals[0], side=-1, stop=D("10.1"), target=D("9.8"))]
        self.engine.step(snap)
        self.engine.step(snapshot(1))
        self.engine.step(snapshot(2))
        self.engine.step(snapshot(3, low="9.7", high="10.2"))
        trade = report(self.path)["recent_trades"][-1]
        self.assertEqual(trade["reason"], "STOP_FIRST_CONSERVATIVE")
        self.assertLess(D(trade["net_pnl"]), 0)

    def test_read_only_status_does_not_create_database(self):
        import sqlite3
        missing = str(Path(self.temp.name)/"missing.db")
        with self.assertRaises(sqlite3.OperationalError):
            report(missing)
        self.assertFalse(Path(missing).exists())

    def test_report_closes_database_connection(self):
        import sqlite3
        from unittest.mock import patch
        opened = []
        real_connect = sqlite3.connect
        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection
        with patch("gptsalov.paper.sqlite3.connect", side_effect=connect):
            report(self.path)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
