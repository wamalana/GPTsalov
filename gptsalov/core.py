from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from hashlib import sha256
import json
from math import lcm
from pathlib import Path
import tomllib

D = Decimal
ZERO = D("0")
BAR_MS = 900_000
HOUR_MS = 3_600_000


def dec(value) -> Decimal:
    result = D(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite decimal")
    return result


def encode(value) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("Invalid step")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("Invalid step")
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def common_step(a: Decimal, b: Decimal) -> Decimal:
    scale = D(10) ** max(0, -a.as_tuple().exponent, -b.as_tuple().exponent)
    return D(lcm(int(a * scale), int(b * scale))) / scale


@dataclass(frozen=True)
class Config:
    mode: str = "paper"
    initial_equity: Decimal = D("100")
    risk_fraction: Decimal = D("0.005")
    leverage: int = 2
    max_notional_fraction: Decimal = D("1")
    daily_loss_fraction: Decimal = D("0.02")
    drawdown_fraction: Decimal = D("0.08")
    max_consecutive_losses: int = 3
    fee_bps: Decimal = D("5")
    slippage_bps: Decimal = D("3")
    funding_reserve_bps: Decimal = D("10")
    max_spread_bps: Decimal = D("10")
    min_quote_volume: Decimal = D("10000000")
    min_age_days: int = 30
    shortlist: int = 20
    max_hold_bars: int = 16
    max_entry_drift: Decimal = D("0.005")
    poll_seconds: int = 60
    max_data_age_ms: int = 120_000

    def __post_init__(self):
        if self.mode != "paper":
            raise ValueError("Only paper mode exists; live trading is not implemented")
        for f in fields(self):
            default = f.default
            value = getattr(self, f.name)
            if isinstance(default, Decimal):
                object.__setattr__(self, f.name, dec(value))
            elif isinstance(default, int) and (type(value) is not int):
                raise ValueError(f"{f.name} must be an integer")
        if not (0 < self.risk_fraction <= D("0.005")):
            raise ValueError("Risk must be > 0 and <= 0.5%")
        if not (self.initial_equity > 0 and 1 <= self.leverage <= 3):
            raise ValueError("Invalid equity/leverage")
        if not (0 < self.max_notional_fraction <= 1):
            raise ValueError("Notional cap must be <= equity")
        if not (0 < self.daily_loss_fraction <= D("0.02")):
            raise ValueError("Daily loss trigger must be <= 2%")
        if not (0 < self.drawdown_fraction <= D("0.08")):
            raise ValueError("Drawdown trigger must be <= 8%")
        for name in ("fee_bps", "slippage_bps", "funding_reserve_bps"):
            if not (0 <= getattr(self, name) <= 100):
                raise ValueError(f"Invalid {name}")
        if not (0 < self.max_spread_bps <= 100 and self.min_quote_volume >= 0):
            raise ValueError("Invalid liquidity filter")
        if not (1 <= self.shortlist <= 40 and 1 <= self.max_hold_bars <= 16):
            raise ValueError("Invalid shortlist/holding period")
        if not (1 <= self.max_consecutive_losses <= 3 and self.min_age_days >= 0):
            raise ValueError("Invalid loss/age settings")
        if not (0 < self.max_entry_drift <= D("0.01")):
            raise ValueError("Invalid entry drift")
        if not (60 <= self.poll_seconds <= 120 and 30_000 <= self.max_data_age_ms <= 180_000):
            raise ValueError("Polling must be 60–120s and data age 30–180s")

    @property
    def fingerprint(self):
        return sha256(encode(asdict(self)).encode()).hexdigest()

    @classmethod
    def load(cls, path: str | None):
        if path is None:
            return cls()
        return cls(**tomllib.loads(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class Candle:
    open_ms: int
    close_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self):
        for name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, name, dec(getattr(self, name)))
        if not (0 <= self.open_ms <= self.close_ms):
            raise ValueError("Invalid candle time")
        if not (0 < self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high):
            raise ValueError("Invalid OHLC")
        if self.volume < 0:
            raise ValueError("Invalid volume")

    @classmethod
    def from_binance(cls, row):
        return cls(int(row[0]), int(row[6]), *(dec(row[i]) for i in (1, 2, 3, 4, 5)))


def closed_bars(rows, now_ms: int, interval_ms=BAR_MS) -> list[Candle]:
    candles = [Candle.from_binance(row) for row in rows if int(row[6]) < now_ms]
    for i, candle in enumerate(candles):
        if candle.open_ms % interval_ms or candle.close_ms != candle.open_ms + interval_ms - 1:
            raise ValueError("Bad candle boundaries")
        if i and candle.open_ms != candles[i-1].open_ms + interval_ms:
            raise ValueError("Missing, duplicate or unordered candle")
    return candles


@dataclass(frozen=True)
class Rules:
    symbol: str
    tick: Decimal
    step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal
    min_price: Decimal
    max_price: Decimal

    @classmethod
    def from_exchange(cls, row):
        filters = {item["filterType"]: item for item in row["filters"]}
        p, lot = filters["PRICE_FILTER"], filters["LOT_SIZE"]
        step, low, high = (dec(lot[k]) for k in ("stepSize", "minQty", "maxQty"))
        market = filters.get("MARKET_LOT_SIZE")
        if market:
            mstep = dec(market["stepSize"])
            if mstep > 0:
                step = common_step(step, mstep)
            low = max(low, dec(market["minQty"]))
            if dec(market["maxQty"]) > 0:
                high = min(high, dec(market["maxQty"]))
        notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL"))
        if notional is None:
            raise ValueError("Missing notional filter")
        minimum = dec(notional.get("minNotional", notional.get("notional")))
        maximum = dec(notional.get("maxNotional", "1e30"))
        tick = dec(p["tickSize"])
        min_price, max_price = dec(p["minPrice"]), dec(p["maxPrice"])
        if not (tick > 0 and step > 0 and high >= low >= 0 and maximum >= minimum >= 0):
            raise ValueError("Invalid exchange filters")
        if not (max_price >= min_price >= 0):
            raise ValueError("Invalid price filter")
        return cls(row["symbol"], tick, step, low, high, minimum, maximum, min_price, max_price)


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: int
    bar_close_ms: int
    reference: Decimal
    stop: Decimal
    target: Decimal
    score: Decimal
    reason: str = "hourly_ema20_donchian20_volume_v1"

    @property
    def key(self):
        return f"{self.reason}:{self.symbol}:{self.bar_close_ms}:{self.side}"

    @classmethod
    def restore(cls, data):
        values = dict(data)
        for name in ("reference", "stop", "target", "score"):
            values[name] = dec(values[name])
        return cls(**values)


def hourly(bars: list[Candle]) -> list[Candle]:
    groups = {}
    for bar in bars:
        groups.setdefault(bar.open_ms // HOUR_MS * HOUR_MS, []).append(bar)
    result = []
    for start, group in sorted(groups.items()):
        if len(group) == 4 and [b.open_ms for b in group] == [start+i*BAR_MS for i in range(4)]:
            result.append(Candle(start, start+HOUR_MS-1, group[0].open,
                                 max(b.high for b in group), min(b.low for b in group),
                                 group[-1].close, sum((b.volume for b in group), ZERO)))
    return result


def strategy(symbol: str, bars: list[Candle]) -> Signal | None:
    """Deterministic research baseline, NOT calibrated probability/expected value."""
    if len(bars) < 100:
        return None
    hours = hourly(bars)
    if len(hours) < 21:
        return None
    ema = sum((b.close for b in hours[:20]), ZERO) / 20
    previous_ema = ema
    for bar in hours[20:]:
        previous_ema = ema
        ema += D(2) / 21 * (bar.close-ema)
    current = bars[-1]
    prior = bars[-21:-1]
    atr = sum((max(bars[i].high-bars[i].low,
                   abs(bars[i].high-bars[i-1].close), abs(bars[i].low-bars[i-1].close))
               for i in range(len(bars)-14, len(bars))), ZERO) / 14
    if not D("0.002") <= atr/current.close <= D("0.05"):
        return None
    avg_volume = sum((b.volume for b in prior), ZERO)/20
    if avg_volume <= 0 or current.volume < avg_volume*D("1.2"):
        return None
    side = 0
    if current.close > max(b.high for b in prior) and hours[-1].close > ema > previous_ema:
        side = 1
    elif current.close < min(b.low for b in prior) and hours[-1].close < ema < previous_ema:
        side = -1
    if not side:
        return None
    distance = atr*D("1.5")
    stop, target = current.close-side*distance, current.close+side*2*distance
    if min(stop, target) <= 0:
        return None
    return Signal(symbol, side, current.close_ms, current.close, stop, target,
                  abs(current.close-ema)/atr)


@dataclass(frozen=True)
class Plan:
    entry: Decimal
    stop: Decimal
    target: Decimal
    qty: Decimal
    notional: Decimal
    risk: Decimal
    budget: Decimal


def size(signal: Signal, reference: Decimal, equity: Decimal, rules: Rules, cfg: Config) -> Plan:
    if signal.side not in (-1, 1) or signal.symbol != rules.symbol:
        raise ValueError("BAD_SIGNAL")
    if reference <= 0 or equity <= 0:
        raise ValueError("NO_EQUITY_OR_PRICE")
    if abs(reference / signal.reference-1) > cfg.max_entry_drift:
        raise ValueError("ENTRY_DRIFT")
    slip = cfg.slippage_bps / 10000
    # Entry/exit market fills are modeled, not tick-constrained submitted prices.
    entry = reference*(1+signal.side*slip)
    rounding = floor_step if signal.side == 1 else ceil_step
    stop, target = rounding(signal.stop, rules.tick), rounding(signal.target, rules.tick)
    if not (min(entry, stop, target) > 0 and signal.side*(entry-stop) > 0
            and signal.side*(target-entry) > 0):
        raise ValueError("INVALID_STOP_TARGET")
    for price in (entry, stop, target):
        if price < rules.min_price or (rules.max_price > 0 and price > rules.max_price):
            raise ValueError("PRICE_FILTER")
    stop_fill = stop*(1-signal.side*slip)
    fee = cfg.fee_bps/10000
    reserve = cfg.funding_reserve_bps/10000
    unit_risk = abs(entry-stop_fill) + fee*(entry+stop_fill) + entry*reserve
    budget = equity*cfg.risk_fraction
    unit_cash = entry/cfg.leverage + entry*(2*fee+reserve+2*slip)
    raw = min(budget/unit_risk, equity*cfg.max_notional_fraction/entry,
              equity/unit_cash, rules.max_qty, rules.max_notional/entry)
    qty = floor_step(raw, rules.step)
    if qty <= 0 or qty < rules.min_qty or qty*entry < rules.min_notional:
        raise ValueError("BELOW_EXCHANGE_MINIMUM")
    return Plan(entry, stop, target, qty, qty*entry, qty*unit_risk, budget)
