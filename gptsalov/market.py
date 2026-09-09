from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .core import BAR_MS, Candle, Config, D, Rules, Signal, closed_bars, dec, strategy


class MarketError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MarketError("Unexpected redirect; refusing to follow")


class BinancePublic:
    """Hard-coded unsigned GET-only allowlist. No credentials or order endpoint."""
    PATHS = frozenset(("/fapi/v1/time", "/fapi/v1/exchangeInfo",
                       "/fapi/v1/ticker/24hr", "/fapi/v1/ticker/bookTicker",
                       "/fapi/v1/klines"))

    def __init__(self):
        self.opener = build_opener(NoRedirect())
        self.last_request = 0.0

    def get(self, path, **params):
        if path not in self.PATHS:
            raise MarketError("Endpoint not allowed")
        allowed = {"symbol", "interval", "limit"} if path.endswith("klines") else set()
        if not set(params) <= allowed:
            raise MarketError("Parameters not allowed")
        time.sleep(max(0, 0.15-(time.monotonic()-self.last_request)))
        self.last_request = time.monotonic()
        url = "https://fapi.binance.com"+path
        if params:
            url += "?"+urlencode(params)
        request = Request(url, method="GET", headers={"User-Agent": "GPTsalov-paper/0.1"})
        try:
            with self.opener.open(request, timeout=12) as response:
                body = response.read(8_000_001)
                if len(body) > 8_000_000:
                    raise MarketError("Response too large")
                data = json.loads(body)
                if isinstance(data, dict) and "code" in data and data["code"] != 0:
                    raise MarketError(f"Binance error code {data['code']}")
                return data
        except HTTPError as exc:
            # No IP rotation, bypass, credentials, or automatic retry on a block.
            raise MarketError(f"Binance HTTP {exc.code}; stop and check access/rate limits") from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise MarketError(f"Public market request failed: {type(exc).__name__}") from exc


@dataclass
class Snapshot:
    now_ms: int
    source: str
    rules: dict[str, Rules]
    bars: dict[str, list[Candle]]
    signals: list[Signal]
    audit: dict

    @property
    def latest_close(self):
        return self.now_ms//BAR_MS*BAR_MS-1


def universe(exchange, tickers, books, cfg, now_ms):
    """Whole-market light scan; strict eligibility, then a volume shortlist."""
    tickers = {row["symbol"]: row for row in tickers}
    books = {row["symbol"]: row for row in books}
    candidates, rejected = [], {}
    for row in exchange["symbols"]:
        symbol = row.get("symbol", "UNKNOWN")
        reason = None
        try:
            if not (row["status"] == "TRADING" and row["contractType"] == "PERPETUAL"
                    and row["quoteAsset"] == "USDT" and row["marginAsset"] == "USDT"
                    and row["underlyingType"] == "COIN"):
                raise ValueError("OUTSIDE_CRYPTO_USDT_PERPETUAL")
            if now_ms-int(row["onboardDate"]) < cfg.min_age_days*86_400_000:
                raise ValueError("NEW_CONTRACT")
            rules = Rules.from_exchange(row)
            ticker, book = tickers[symbol], books[symbol]
            for stamp in (int(ticker["closeTime"]), int(book["time"])):
                if not -5000 <= now_ms-stamp <= cfg.max_data_age_ms:
                    raise ValueError("STALE_TICKER")
            volume = dec(ticker["quoteVolume"])
            bid, ask = dec(book["bidPrice"]), dec(book["askPrice"])
            if not (0 < bid <= ask):
                raise ValueError("INVALID_SPREAD")
            spread = (ask-bid)/((ask+bid)/2)*10000
            if volume < cfg.min_quote_volume:
                raise ValueError("LOW_VOLUME")
            if spread > cfg.max_spread_bps:
                raise ValueError("WIDE_SPREAD")
            if min(dec(book["bidQty"])*bid, dec(book["askQty"])*ask) < 2*cfg.initial_equity:
                raise ValueError("THIN_TOP_OF_BOOK")
            candidates.append((volume, symbol, rules))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "MALFORMED_MARKET_DATA"
        if reason:
            rejected[symbol] = reason
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[:cfg.shortlist], rejected


class BinanceFeed:
    def __init__(self, cfg: Config, client=None):
        self.cfg = cfg
        self.client = client or BinancePublic()

    def snapshot(self, required=()):
        clock_start = time.monotonic()
        now_ms = int(self.client.get("/fapi/v1/time")["serverTime"])
        if abs(now_ms-int(time.time()*1000)) > 5000:
            raise MarketError("Local/exchange clock skew exceeds 5 seconds")
        exchange = self.client.get("/fapi/v1/exchangeInfo")
        tickers = self.client.get("/fapi/v1/ticker/24hr")
        books = self.client.get("/fapi/v1/ticker/bookTicker")
        chosen, rejected = universe(exchange, tickers, books, self.cfg, now_ms)
        rules = {symbol: rule for _, symbol, rule in chosen}
        rows = {row["symbol"]: row for row in exchange["symbols"]}
        for symbol in required:
            if symbol not in rules:
                try:
                    rules[symbol] = Rules.from_exchange(rows[symbol])
                except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                    raise MarketError(f"Cannot reconcile contract {symbol}") from exc
        eligible = {symbol for _, symbol, _ in chosen}
        histories, signals = {}, []
        expected_close = now_ms//BAR_MS*BAR_MS-1
        for symbol in rules:
            rows = self.client.get("/fapi/v1/klines", symbol=symbol, interval="15m", limit=200)
            try:
                bars = closed_bars(rows, now_ms)
                if not bars or bars[-1].close_ms != expected_close:
                    raise ValueError("STALE_CANDLES")
            except (ValueError, TypeError, IndexError, ArithmeticError) as exc:
                if symbol in required:
                    raise MarketError(f"Cannot reconcile candles for {symbol}") from exc
                rejected[symbol] = "INVALID_OR_STALE_CANDLES"
                continue
            histories[symbol] = bars
            signal = strategy(symbol, bars) if symbol in eligible else None
            if signal:
                signals.append(signal)
            elif symbol in eligible:
                rejected[symbol] = "NO_BASELINE_SIGNAL"
        elapsed = int((time.monotonic()-clock_start)*1000)
        if elapsed > self.cfg.max_data_age_ms or (now_ms+elapsed)//BAR_MS != now_ms//BAR_MS:
            raise MarketError("Snapshot crossed candle boundary or expired; retry next cycle")
        signals.sort(key=lambda s: (-s.score, s.symbol))
        return Snapshot(now_ms+elapsed, "binance-public", rules, histories, signals,
                        {"total_symbols": len(exchange["symbols"]), "shortlisted": len(chosen),
                         "histories_loaded": len(histories), "rejected": rejected,
                         "ranking": "heuristic_not_probability", "news": "NOT_CONNECTED"})


def demo_rule(symbol="DEMOUSDT"):
    return Rules(symbol, D("0.001"), D("0.1"), D("0.1"), D("100000"),
                 D("5"), D("1000000"), D("0.001"), D("100000"))


class DemoFeed:
    """Reproducible synthetic candles. Never a profitability benchmark."""
    def __init__(self):
        # UTC midnight, aligned to 15m. Each snapshot advances exactly one bar.
        self.start = 1_783_468_800_000
        self.index = 119
        self.history = []
        price = D("10")
        for i in range(500):
            phase = (i//40) % 2
            delta = D("0.007") if not phase else D("-0.007")
            if i % 12 == 11:
                delta *= 9
            close = price+delta
            volume = D("2000") if i % 12 == 11 else D("1000")
            t = self.start+i*BAR_MS
            self.history.append(Candle(t, t+BAR_MS-1, price,
                                       max(price, close)+D("0.009"),
                                       min(price, close)-D("0.009"), close, volume))
            price = close

    def snapshot(self, required=()):
        if self.index >= len(self.history):
            raise MarketError("Synthetic sequence finished")
        bars = self.history[max(0, self.index-199):self.index+1]
        self.index += 1
        signal = strategy("DEMOUSDT", bars)
        return Snapshot(bars[-1].close_ms+1, "synthetic-demo", {"DEMOUSDT": demo_rule()},
                        {"DEMOUSDT": bars}, [signal] if signal else [],
                        {"total_symbols": 1, "shortlisted": 1, "synthetic": True,
                         "ranking": "heuristic_not_probability", "news": "NOT_CONNECTED"})
