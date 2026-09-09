import copy
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from gptsalov.core import BAR_MS, Config
from gptsalov.market import BinanceFeed, BinancePublic, DemoFeed, MarketError
from test_core import exchange_row


class FakeClient:
    def __init__(self):
        demo = DemoFeed()
        self.bars = demo.history[:120]
        self.now = self.bars[-1].close_ms+1
        self.missing = False
        self.called = []

    def get(self, path, **params):
        self.called.append((path, params))
        if path.endswith("/time"):
            return {"serverTime": self.now}
        if path.endswith("exchangeInfo"):
            return {"symbols": [exchange_row()]}
        if path.endswith("24hr"):
            return [{"symbol": "AAAUSDT", "quoteVolume": "100000000", "closeTime": self.now}]
        if path.endswith("bookTicker"):
            return [{"symbol": "AAAUSDT", "bidPrice": "9.999", "askPrice": "10.001",
                     "bidQty": "1000", "askQty": "1000", "time": self.now}]
        if path.endswith("klines"):
            bars = self.bars[:-1] if self.missing else self.bars
            return [[b.open_ms, str(b.open), str(b.high), str(b.low), str(b.close), str(b.volume), b.close_ms]
                    for b in bars]
        raise AssertionError(path)


class FeedTests(unittest.TestCase):
    def test_api_contract_shape_and_unsigned_only(self):
        client = FakeClient()
        with patch("gptsalov.market.time.time", return_value=client.now/1000):
            snap = BinanceFeed(Config(), client).snapshot()
        self.assertEqual(snap.source, "binance-public")
        self.assertEqual(len(snap.bars["AAAUSDT"]), 120)
        self.assertTrue(all(path in BinancePublic.PATHS for path, params in client.called))

    def test_local_clock_skew_stops(self):
        client = FakeClient()
        with patch("gptsalov.market.time.time", return_value=client.now/1000+10):
            with self.assertRaisesRegex(MarketError, "clock skew"):
                BinanceFeed(Config(), client).snapshot()

    def test_stale_candles_excluded_from_signals(self):
        client = FakeClient()
        client.missing = True
        with patch("gptsalov.market.time.time", return_value=client.now/1000):
            snap = BinanceFeed(Config(), client).snapshot()
        self.assertFalse(snap.signals)
        self.assertFalse(snap.bars)
        self.assertEqual(snap.audit["rejected"]["AAAUSDT"], "INVALID_OR_STALE_CANDLES")

    def test_stale_open_position_candles_stop(self):
        client = FakeClient()
        client.missing = True
        with patch("gptsalov.market.time.time", return_value=client.now/1000):
            with self.assertRaisesRegex(MarketError, "reconcile candles"):
                BinanceFeed(Config(), client).snapshot(["AAAUSDT"])

    def test_removed_position_contract_stops(self):
        client = FakeClient()
        with patch("gptsalov.market.time.time", return_value=client.now/1000):
            with self.assertRaisesRegex(MarketError, "reconcile contract"):
                BinanceFeed(Config(), client).snapshot(["REMOVEDUSDT"])

    def test_order_and_account_endpoints_unavailable(self):
        client = BinancePublic()
        for path in ("/fapi/v1/order", "/fapi/v1/algoOrder", "/fapi/v2/account", "https://other.host"):
            with self.subTest(path=path), self.assertRaisesRegex(MarketError, "not allowed"):
                client.get(path)

    def test_signature_parameter_refused(self):
        with self.assertRaisesRegex(MarketError, "Parameters not allowed"):
            BinancePublic().get("/fapi/v1/time", signature="not-real")

    def test_rate_limit_does_not_retry(self):
        client = BinancePublic()
        with patch.object(client.opener, "open", side_effect=HTTPError("url", 429, "rate limit", {}, None)) as call:
            with self.assertRaisesRegex(MarketError, "HTTP 429"):
                client.get("/fapi/v1/time")
            self.assertEqual(call.call_count, 1)

    def test_network_failure_no_synthetic_fallback(self):
        client = BinancePublic()
        with patch.object(client.opener, "open", side_effect=URLError("offline")):
            with self.assertRaisesRegex(MarketError, "request failed"):
                client.get("/fapi/v1/time")


if __name__ == "__main__":
    unittest.main()
