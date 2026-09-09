from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import unittest

from gptsalov.__main__ import main
from gptsalov.paper import report


class CLITests(unittest.TestCase):
    def test_demo_and_restart_via_cli(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(StringIO()):
            db = str(Path(temp)/"demo.db")
            self.assertEqual(main(["run", "--db", db, "--cycles", "180"]), 0)
            before = report(db)
            self.assertEqual(main(["run", "--db", db, "--cycles", "2"]), 0)
            after = report(db)
            self.assertEqual(before["closed_trades"], 10)
            self.assertGreater(after["last_close_ms"], before["last_close_ms"])
            self.assertEqual(before["balance"], after["balance"])

    def test_invalid_config_stops_before_database_creation(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stderr(StringIO()):
            cfg, db = Path(temp)/"config.toml", Path(temp)/"paper.db"
            cfg.write_text('mode = "live"\n', encoding="utf-8")
            self.assertEqual(main(["run", "--config", str(cfg), "--db", str(db)]), 1)
            self.assertFalse(db.exists())

    def test_demo_scan_does_not_need_keys(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["scan"]), 0)
        self.assertIn("READ_ONLY", output.getvalue())
        self.assertIn("synthetic-demo", output.getvalue())

    def test_status_missing_file_is_error(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stderr(StringIO()):
            db = Path(temp)/"missing.db"
            self.assertEqual(main(["status", "--db", str(db), "--json"]), 1)
            self.assertFalse(db.exists())

    def test_expired_snapshot_skipped_then_recovers(self):
        from unittest.mock import patch, Mock
        from gptsalov.market import DemoFeed, SnapshotExpired
        snapshot = DemoFeed().snapshot()
        snapshot.source = "binance-public"
        feed = Mock()
        feed.snapshot.side_effect = [SnapshotExpired("boundary"), snapshot]
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(StringIO()), \
             patch("gptsalov.__main__.BinanceFeed", return_value=feed), \
             patch("gptsalov.__main__.time.sleep"):
            db = str(Path(temp)/"paper.db")
            self.assertEqual(main(["run", "--source", "binance", "--db", db, "--cycles", "2"]), 0)
            state = report(db)
            self.assertEqual(feed.snapshot.call_count, 2)
            self.assertIsNone(state["last_error"])
            self.assertIsNotNone(state["as_of_ms"])


if __name__ == "__main__":
    unittest.main()
