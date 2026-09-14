# Entry-quality experiment v2 — 2026-09-13 22:46 Asia/Bangkok

## Evidence
Read-only VPS audit of the v1 forward ledgers:
- Baseline: 18 closed trades, gross PnL +0.98912188468 USDT,
  fees + fixed funding reserves 2.10849253414, net -1.11937064946.
  Equity 98.59198132668 also includes the still-open ADA position.
- Baseline long: 8 closed, +1.50372141147 net. Short: 10 closed, -2.62309206093 net.
- 1R: 15 closed, -0.91360112954 net; 1.5R: 5 closed, -0.55200754122;
  2R: 5 closed, -0.82916494803. All three have LOSS_STREAK_REVIEW.
- Baseline LINK short at 1789307100000 had planned net RR 3.5866,
  but actual entry-to-stop distance was only 0.07552 times the original
  signal reference-to-stop distance. It stopped out for -0.257594056179.
- Funding reserves are simulator assumptions, not observed exchange funding.
  Gross above already includes modeled slippage. Do not subtract slippage twice.

These are overlapping trades, not independent samples. A conditional split of
closed trades is not a new portfolio backtest. Long-only is not selected from
this short sample. No coin blacklist or automatic risk-lock reset is introduced.
The evidence motivates hypotheses; it does not prove the proposed filters help.

## Frozen prospective comparison
Three NEW ledgers start together at 100 USDT, each preserving the original risk
config (0.5% risk, 2% daily loss, 8% drawdown, 3-loss review stop, one position).
None is a reset or continuation of an old ledger.

1. rr1_control: original 1R filter, same ranking and execution.
2. rr1_cost25: 1R plus modeled fee/reserve cost <=25% of gross target profit.
3. rr1_geometry50: 1R plus entry-to-rounded-stop distance >=50% of original
   signal-reference-to-stop distance.

The 25% and 50% cutoffs are round engineering hypotheses, NOT fitted optima.
They are tested separately, not combined. No target/stop is moved. Entry checks
use only already-known signal and modeled entry; future candle high/low does not
enter the filter. No historical sample is relabeled as prospective validation.

The common 1R control is necessary: compare only the new simultaneous cohort,
not the new equity against old ledgers starting at different dates.
Higher RR is not interpreted as higher win probability. The geometry guard
is intended to reject extreme stop compression, not every pullback.
Keep all outcomes including missed winning trades, fees and locks.
Do not auto-reset a locked group. If it locks, record that as an outcome.
Review the full fixed cohort before introducing another parameter change.
No automatic promotion to Testnet or live based on this experiment.

## Run / read
python3 -m gptsalov.tuning run --directory NEW_EMPTY_DIRECTORY --config config.toml
python3 -m gptsalov.tuning status --directory DIRECTORY

A distinct source symlink, service and data directory are deployed; original
main/forward-v1 services, locks and ADA stop/target remain untouched.
All three v2 groups use the same snapshots and required-symbol union.
Third scanner adds public API load; expiry skips, other API faults stop service.
Rollback: stop/disable only gptsalov-tuning.service. Retain v2 ledgers for audit.

## Validation
Four new tests verify 1R control full-ledger parity over 180 demo snapshots,
long/short cost and geometry boundaries, resume equivalence and immutable
policies. Existing forward and baseline tests must continue to pass.
No historical price replay claiming profitability has been performed.

Background on selection bias:
https://arxiv.org/abs/1905.05023
