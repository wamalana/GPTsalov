from dataclasses import replace
from decimal import Decimal
import unittest

from gptsalov.core import (BAR_MS, Candle, Config, D, Rules, Signal, closed_bars,
                          common_step, dec, floor_step, hourly, size, strategy)
from gptsalov.market import DemoFeed, demo_rule, universe


def exchange_row(symbol="AAAUSDT"):
    return {"symbol": symbol, "status": "TRADING", "contractType": "PERPETUAL",
            "quoteAsset": "USDT", "marginAsset": "USDT", "underlyingType": "COIN",
            "onboardDate": 0, "quantityPrecision": 0,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.001", "minPrice": "0.001", "maxPrice": "10000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1", "maxQty": "10000"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.2", "minQty": "0.2", "maxQty": "1000"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"}]}


def signal(side=1):
    return Signal("DEMOUSDT", side, BAR_MS-1, D("10"), D("10")-side*D("0.1"),
                  D("10")+side*D("0.2"), D("1"))


class ConfigTests(unittest.TestCase):
    def test_live_refused(self):
        with self.assertRaisesRegex(ValueError, "Only paper"):
            Config(mode="live")

    def test_risk_caps(self):
        for change in ({"risk_fraction": "0.006"}, {"leverage": 4}, {"daily_loss_fraction": ".03"},
                       {"drawdown_fraction": ".1"}, {"max_notional_fraction": 2}, {"shortlist": 100}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                Config(**change)

    def test_nonfinite_refused(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config(initial_equity=value)

    def test_wrong_integer_type(self):
        for value in (True, 2.0, "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config(leverage=value)

    def test_unknown_config_key(self):
        with self.assertRaises(TypeError):
            Config(api_key="secret")

    def test_hash_changes(self):
        self.assertNotEqual(Config().fingerprint, Config(fee_bps="6").fingerprint)


class SizingTests(unittest.TestCase):
    def test_floor_not_decimal_places(self):
        self.assertEqual(floor_step(D("1.23"), D("0.05")), D("1.20"))
        self.assertEqual(common_step(D("0.2"), D("0.3")), D("0.6"))

    def test_risk_and_notional_caps(self):
        for side in (-1, 1):
            for equity in (D("1"), D("10"), D("100"), D("1000")):
                for width in (D(".01"), D(".05"), D(".1"), D(".5")):
                    sig = replace(signal(side), stop=D("10")-side*width, target=D("10")+side*width*2)
                    try:
                        p = size(sig, D("10"), equity, demo_rule(), Config())
                    except ValueError as exc:
                        self.assertEqual(str(exc), "BELOW_EXCHANGE_MINIMUM")
                        continue
                    self.assertLessEqual(p.risk, p.budget)
                    self.assertLessEqual(p.notional, equity)
                    self.assertEqual(p.qty % D(".1"), 0)

    def test_minimum_is_skipped_not_rounded_up(self):
        rules = replace(demo_rule(), min_notional=D("1000"))
        with self.assertRaisesRegex(ValueError, "BELOW_EXCHANGE_MINIMUM"):
            size(signal(), D("10"), D("100"), rules, Config())

    def test_gap_invalidates_intent(self):
        with self.assertRaisesRegex(ValueError, "ENTRY_DRIFT"):
            size(signal(), D("11"), D("100"), demo_rule(), Config())

    def test_wrong_stop_rejected(self):
        with self.assertRaisesRegex(ValueError, "INVALID_STOP_TARGET"):
            size(replace(signal(), stop=D("11")), D("10"), D("100"), demo_rule(), Config())

    def test_costs_reduce_size(self):
        cheap = size(signal(), D("10"), D("100"), demo_rule(), Config(fee_bps=0, slippage_bps=0, funding_reserve_bps=0))
        normal = size(signal(), D("10"), D("100"), demo_rule(), Config())
        self.assertLess(normal.qty, cheap.qty)

    def test_leverage_does_not_multiply_risk_size(self):
        low = size(signal(), D("10"), D("100"), demo_rule(), Config(leverage=1))
        high = size(signal(), D("10"), D("100"), demo_rule(), Config(leverage=3))
        self.assertEqual(low.qty, high.qty)

    def test_uses_filters_not_precision(self):
        rules = Rules.from_exchange(exchange_row())
        self.assertEqual(rules.step, D(".2"))
        self.assertEqual(rules.max_qty, D("1000"))

    def test_missing_filters_fail_closed(self):
        row = exchange_row()
        row["filters"] = row["filters"][:-1]
        with self.assertRaisesRegex(ValueError, "Missing notional"):
            Rules.from_exchange(row)

    def test_disabled_market_step(self):
        row = exchange_row()
        row["filters"][2]["stepSize"] = "0"
        self.assertEqual(Rules.from_exchange(row).step, D(".1"))


class CandleTests(unittest.TestCase):
    def test_open_candle_removed(self):
        rows = [[0, "10", "11", "9", "10", "1", BAR_MS-1],
                [BAR_MS, "10", "11", "9", "10", "1", BAR_MS*2-1]]
        self.assertEqual(len(closed_bars(rows, BAR_MS)), 1)

    def test_gap_and_duplicate_refused(self):
        first = [0, "10", "11", "9", "10", "1", BAR_MS-1]
        for second in (first, [BAR_MS*2, "10", "11", "9", "10", "1", BAR_MS*3-1]):
            with self.assertRaisesRegex(ValueError, "Missing, duplicate"):
                closed_bars([first, second], BAR_MS*4)

    def test_bad_ohlc_refused(self):
        with self.assertRaises(ValueError):
            Candle(0, BAR_MS-1, D(10), D(9), D(8), D(10), D(1))

    def test_partial_hour_not_used(self):
        feed = DemoFeed()
        self.assertEqual(len(hourly(feed.history[:7])), 1)

    def test_no_signal_without_history(self):
        self.assertIsNone(strategy("X", DemoFeed().history[:10]))

    def test_baseline_long_and_short(self):
        feed = DemoFeed()
        sides = {s.side for i in range(100, 300)
                 if (s := strategy("DEMOUSDT", feed.history[max(0,i-199):i+1]))}
        self.assertEqual(sides, {-1, 1})


class UniverseTests(unittest.TestCase):
    def scan(self, row=None, book_changes=None, ticker_changes=None):
        now = 1783468800000
        ticker = {"symbol": "AAAUSDT", "quoteVolume": "100000000", "closeTime": now}
        book = {"symbol": "AAAUSDT", "bidPrice": "9.999", "askPrice": "10.001",
                "bidQty": "1000", "askQty": "1000", "time": now}
        book.update(book_changes or {})
        ticker.update(ticker_changes or {})
        return universe({"symbols": [row or exchange_row()]}, [ticker], [book], Config(), now)

    def test_eligible_contract(self):
        chosen, rejected = self.scan()
        self.assertEqual(len(chosen), 1)
        self.assertFalse(rejected)

    def test_tradfi_not_crypto(self):
        row = exchange_row()
        row["underlyingType"] = "INDEX"
        self.assertFalse(self.scan(row)[0])

    def test_stale_rejected(self):
        self.assertIn("STALE", self.scan(book_changes={"time": 0})[1]["AAAUSDT"])

    def test_spread_rejected(self):
        self.assertEqual(self.scan(book_changes={"askPrice": "11"})[1]["AAAUSDT"], "WIDE_SPREAD")

    def test_thin_book_rejected(self):
        self.assertEqual(self.scan(book_changes={"bidQty": "1"})[1]["AAAUSDT"], "THIN_TOP_OF_BOOK")

    def test_low_volume_rejected(self):
        self.assertEqual(self.scan(ticker_changes={"quoteVolume": "1"})[1]["AAAUSDT"], "LOW_VOLUME")


if __name__ == "__main__":
    unittest.main()
