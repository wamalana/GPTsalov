# XAUUSD demo research foundation

Status: offline replay implemented; no broker connection, no orders, no historical
broker data imported. This module does not migrate or stop the Binance bot.

Two independent five-minute strategies:
- Trend pullback: EMA20/EMA50 direction; previous bar touches EMA20, current
  completed bar breaks previous high/low in trend direction.
- Session breakout: close crosses prior complete hour range during configured
  session opening hour. Session timezone uses IANA names to handle daylight saving.

Next-bar bid/ask entry, ATR14 x 1.5 stop, 2R target, maximum one-hour hold. Stop
wins ambiguous bars. One position per independent replay. Session window ends
three hours after configured start and requires contiguous data. Broker sessions
and rollover MUST be checked before choosing this window. Not a full holiday,
margin, minimum stop-distance or variable tick-value simulator.

Size includes round-trip commissions and two-sided slippage; it rounds DOWN and
skips signals below minimum lot. Actual gap losses may exceed modeled risk.

Before running, supply verified broker-specific JSON fields:

```json
{
  "environment": "demo",
  "account_currency": "USD",
  "broker": "REQUIRED",
  "symbol": "REQUIRED",
  "tick_size": null,
  "tick_value_loss_per_lot": null,
  "volume_min": null,
  "volume_step": null,
  "volume_max": null,
  "risk_usd": null,
  "initial_equity_usd": null,
  "commission_roundtrip_usd_per_lot": null,
  "slippage_price_per_side": null,
  "max_spread_price": null,
  "session_timezone": "Europe/London",
  "session_start_hour": 8
}
```

Null fields deliberately prevent running with invented broker specifications.
Prices in USD per ounce, tick value in USD per tick per lot. Current version
supports only constant tick-value USD contracts. The symbol must match the broker,
including any suffix. Session choice above is a hypothesis, not an optimized rule.

CSV columns: time,bid_open,bid_high,bid_low,bid_close,ask_open,ask_high,ask_low,ask_close.
Time is the timezone-aware five-minute BAR OPEN. Use completed broker bars from
bid/ask ticks; do not substitute a mid-price chart or fabricate historical spread.

```sh
python3 -m gptsalov.xau_research --config broker.json --bars broker-5m.csv --output xau-run.json
```

Next dependency: broker name, MT5 versus API platform, demo account type and exact
XAUUSD contract specification. Never put passwords/API secrets into this repo.
After data import: evaluate chronological holdout and Demo forward results with
cost sensitivity before any execution integration. No profitability claim yet.
