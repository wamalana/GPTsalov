import unittest
from tempfile import TemporaryDirectory
from unittest import mock
from gptsalov import testnet_pilot as tp
from gptsalov.core import BAR_MS


class ScanRetry(unittest.TestCase):
    def test_stale_scan_does_not_consume_the_60s_gate(self):
        stamp = 1_789_000_000_000//BAR_MS*BAR_MS-1
        now = stamp+30_000  # 30s after the candle close; scanner not finished yet
        with TemporaryDirectory() as d, mock.patch.object(tp, 'now_ms', return_value=now), \
                mock.patch('gptsalov.market_scanner.read', return_value={'status': 'running', 'started_ms': now-20_000}):
            b = tp.Book(d, 'synthetic')
            try:
                b.s['last_scan_ms'] = now
                self.assertEqual(tp.multi_candidate(None, None, b, limit=2), [])
                self.assertEqual(b.s['last_selection']['status'], 'WAIT_FRESH_SCAN')
                self.assertEqual(b.s['last_scan_ms'], 0)   # next 10s tick may check again
                self.assertNotEqual(b.s.get('seen_ms'), stamp)  # candle not marked as seen
            finally:
                b.close()


if __name__ == '__main__':
    unittest.main()
