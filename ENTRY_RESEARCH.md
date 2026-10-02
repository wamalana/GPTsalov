# Prospective entry study v1

Goal: collect evidence for the user's +5%/day research target. This release does
not claim that return or change the Testnet trading policy. It starts prospective
signal experiments while preserving the existing scanner consumer contract.

## Registered experiments

- `baseline_v1`: exact `core.strategy` on scanner closed 15m candles. This is the
  baseline signal function, NOT all adaptive stops, sizing or portfolio gates of
  the Testnet pilot. Actual pilot results remain in the existing pilot journal.
- `trend_pullback_v1`: completed hourly EMA20 trend; previous 15m candle touches
  the prior 15m EMA20 and closes within one ATR on the adverse side; current
  candle reclaims EMA and breaks the previous high/low; volume >= 1.2x prior 20.
- `compression_breakout_v1`: same hourly trend; prior ATR14 / prior ATR50 <= .8;
  current close breaks the prior 20-bar high/low, volume >= 1.2x prior 20.
- New variants: ATR14 / close in [.002,.05], stop distance 1.5 ATR, target 2R.
  Parameters are predefined hypotheses, not optimized results.

All functions use closed, contiguous bars available at observation. Signals
are first registered after analysis; repeat scans never rewrite entry time.
Every completed/partial scan retains ALL scanner rows, including rejected,
unvisited and error rows. Per-analyzed-symbol observations retain variant
reasons, feature values, news evidence references, original bars and timestamps.
A failed recorder is exposed as `entry_research.status=ERROR` in the latest
scanner snapshot; it cannot change a candidate or send an order. Storage is
bounded to 512 MiB with no silent deletion; operator archival/versioning is
required before filling the database. A killed scan can leave observations
without a completed scan row; absence of that row is evidence of incomplete work.

## Outcomes and limitations

Public production 1m prices are evaluated every two minutes. Entry is the first
full minute strictly AFTER observation. Stop wins if both barriers are touched;
gaps through stop fill at the worse open. The existing tested fixed-4h simulator
is reused with 5 bps fees and 3 bps slippage per side plus a 15 bps funding reserve.
Quantity normalizes price-stop risk to .25 USDT before costs at the modeled
entry. The resulting net loss can exceed .25 USDT. There is no actual order.
An unresolved entry expires visibly after 24h rather than receiving a fabricated
fill. Network/data errors stop the batch and impose a 30-minute cooldown.

**These are overlapping signal outcomes, not a portfolio equity curve.** No
exchange lot/min-notional rounding, margin, three-slot scheduling, actual
funding, latency queue or intraminute path is modeled. Do not interpret the sum
as daily portfolio return. The report expressly labels these limitations.
No synthetic win probability, trained ML model, automatic strategy promotion,
leverage increase, mainnet order, account reset or chat report is introduced.

## Commands

    python3 -m gptsalov.market_scanner --pilot-db PATH --research-db SEPARATE_DB
    python3 -m gptsalov.entry_research evaluate --db SEPARATE_DB
    python3 -m gptsalov.entry_research report --db SEPARATE_DB
    python3 -m gptsalov.entry_research export --db SEPARATE_DB > closed-signals.jsonl

Export joins entry-time features only to CLOSED future outcomes for later ML
research. There is not enough evidence to train/promote a filter merely because
export works. Freeze a chronological split and purge overlapping 4h labels;
fit preprocessing on training data only. Compare models against no-filter
control on unseen periods, with uncertainty and all attempted trials retained.

First evidence review: >=100 CLOSED signals per candidate across >=30 days,
multiple regimes, positive net expectancy under stressed costs and robustness
without the best trade. These are review minimums, not proof. Portfolio replay
with actual market rules and funding is a separate required gate before any
claim about +5% days or deployment of a new execution policy.

## Deployment

`scripts/deploy_entry_research.py STAGED_DIR` copies the current scanner release
to an isolated release, verifies original scanner/core hashes, overlays only the
research changes, runs targeted tests and installs one scanner override plus the
outcome timer. Existing pilot, monitor, news, slots, locks and risk are untouched.
Rollback metadata is saved before activation; automatic rollback restores the
previous units and symlink on failure. Do not run two scanner processes manually.
