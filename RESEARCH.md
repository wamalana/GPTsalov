# Net reward/risk observation — v0.1.2

This is an optional data collector, not a new trading strategy. It never blocks
an entry, changes a stop/target, resizes a position, changes locks or calls an AI API.
Baseline config and its hash remain unchanged. No live order adapter is added.

## Enable

Add `--research-db /home/wamalana/GPTsalov/data/net-rr-research.db` to the existing
`run` command after upgrading the source to v0.1.2. The supplied systemd unit
includes it. Omitting the flag runs without recording. Never point it at the
paper ledger (including a symlink/hardlink); the CLI rejects this.

Read results:

```bash
python3 -m gptsalov research-status --db /home/wamalana/GPTsalov/data/net-rr-research.db
```

## Measurements

At every new validated candle, AFTER committing the baseline ledger:

- Save available closed OHLCV history with first observation timestamps. This
  covers loaded pairs, not all 897 exchange symbols. A newly shortlisted pair's
  old bars are historical context, not evidence of observations at those times.
- Save scan rejections, account/position state, pending intent and timestamped
  events. Signal candidates include selected and unselected signals, baseline
  exclusion reason, exchange rules and reference-price sizing assessment.
- For each actual paper open, record rounded stop/target, modeled fill, size,
  entry fee, funding reserve, modeled risk and estimated target net reward.
- For closes, retain close timestamp and realized net PnL linked by signal ID.

For side `s`, entry `e`, target `t` and quantity `q`:

```
target_fill = t * (1 - s * slippage_bps / 10000)
net_reward = s * (target_fill - e) * q - target_exit_fee - entry_fee - funding_reserve
net_rr = net_reward / modeled_risk
```

The denominator is the existing sizing model's loss at its slipped stop, with
fees/reserve. Gaps can lose more. Thresholds 1, 1.5 and 2 are experimental inclusive
cutoffs, versioned here, not optimized or recommendations. Only the actual open
assessment drives the report's pass/block grouping. Reference-only candidate
estimates are explicitly separate. No future high/low or realized PnL enters
the assessment. The candle simulator still learns fills at candle close; this
collector does not create realtime execution or an earlier observation.

## Interpretation and gaps

`research-status` compares net PnL of baseline closed trades grouped by whether
they passed each cutoff. It is NOT a backtest or counterfactual equity curve:
skipping a trade can free a slot, change sizing and alter subsequent risk locks.
There is no alternate simulation of unexecuted signals and no news filter here.
An existing position opened before collection has an outcome but no entry
assessment; the report explicitly counts it as unmatched and excludes it from
comparisons. Research rows are not retrospectively fabricated for old trades.

The collector uses a separate SQLite transaction. A crash between ledger and
research commits can lose one observation. Inspect coverage before interpreting
results. Deduplication uses candle timestamps and signal IDs, including after
restart. Repeated polling does not duplicate rows.

Bars and cycle snapshots retain 30 days. Candidate/entry/outcome records remain
until the approximately 128 MiB database-page limit is reached. SQLite may need
additional transient journal space. Export/checkpoint before the limit; disk
failure or identity mismatch emits `RESEARCH_DISABLED` once and disables further
recording for that process. Paper trading continues. The research timestamp and
service journal must be checked; an existing sidecar does not prove it is fresh.
The collector adds local I/O but no market requests; it has a 0.2s SQLite lock
timeout. A severely stalled filesystem can still affect overall process timing.

## Status report additions

The existing `status --json` now adds close timestamps (read from original event
rows, including old trades), `observed_age_ms`, `window_start_ms`,
`closed_trades_last_4h` and `closed_trade_net_pnl_last_4h`. The 4-hour window is
inclusive and excludes future timestamps. This sum is closed-trade PnL, not
the change in equity across the window. Status uses a consistent read transaction.

## Deployment / rollback

1. Stage a complete new source release and run its tests before stopping the bot.
2. Stop `gptsalov-paper.service`; preserve the existing config exactly and make
   a SQLite backup of the stopped paper ledger. Keep the prior release/unit.
3. Point `current` at the new release and install the supplied unit, then run
   `systemctl --user daemon-reload` and `systemctl --user start gptsalov-paper.service`.
4. Check service, `status --json`, and the journal immediately. Research records
   start on the next new closed candle; verify `research-status` then.
5. Roll back by stopping the unit and restoring the old source symlink and unit.
   Keep the current ledger: no schema or config migration was introduced.
   Do NOT restore an old ledger snapshot over newer trades or delete a risk lock.

A deployment interruption can trigger the existing data-gap reconciliation
lock. Never clear it automatically. ChatGPT's reporting schedule remains disabled.
