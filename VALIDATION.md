# Validation — GPTsalov v0.1.1

Date: 2026-09-09. Environment: Linux, Python 3.12.14.

## Verified locally

- `python3 -m unittest discover -s tests -v`: 69 test methods, all passed locally.
- `python3 -m compileall -q gptsalov tests`: passed.
- CLI synthetic run: 180 snapshots, 10 closed paper trades, zero real orders.
- Synthetic final balance/equity: 99.449089652360 from 100; modeled net result -0.550910347640 USDT. This deliberately unoptimized synthetic sequence is an accounting regression fixture, not evidence for or against market profitability.
- CLI restart resumed the existing synthetic ledger without resetting its balance or duplicating fills.
- Release ZIP is CRC-checked and its test suite is rerun in a fresh extracted directory by the packaging script.

## Test coverage areas

- Reject live mode, invalid/nonfinite configuration and unauthorized endpoint/parameter paths.
- Decimal sizing, fee/slippage/funding-reserve budgets, quantity-step intersection, minimum-size skip, price drift, stop direction and notional cap.
- Closed-candle exclusion, missing/duplicate bars, hourly aggregation and baseline long/short signals.
- Crypto-perpetual universe eligibility, volume, top-of-book liquidity, spread and freshness filters.
- Mocked public API parsing, local clock skew, missing contract/candle reconciliation, network errors and no retry on HTTP 429.
- Persisted precommitted entry intent, no past entry before signal observation, repeated-snapshot idempotency, single-writer exclusion, configuration/source binding and SQL transaction rollback.
- Long/short stop-first handling, gap losses exceeding planned risk, holding-time exit, daily/drawdown/loss-streak locks and Asia/Bangkok day boundary.
- Status reads do not create a missing ledger; errors do not advance market timestamps or present healthy status.

## Not verified / not implemented

- Initial scratch environment could not reach Binance. Subsequent authorized VPS check on Debian 13 / Python 3.13.5 succeeded: public server time and a full preflight scan (897 returned symbols, 20 shortlisted, 20 histories loaded). Counts describe that snapshot, not a guaranteed future universe.
- Original 66-test suite passed on the VPS. Python 3.13 revealed an unclosed report connection; v0.1.1 closes it explicitly and adds a regression test. Two additional tests verify expired-snapshot skipping/recovery instead of stopping the loop.
- Docker build/runtime was not executed. The VPS source/preflight checks are recorded above; the systemd user unit requires verification on activation.
- No real historical profitability backtest, walk-forward study, calibrated probability, statistical confidence interval or parameter optimization.
- No account authentication, live/demo exchange orders, actual funding settlements, partial fills, liquidation modeling or realtime risk guarantees.
- No connected news/LLM, dashboard, external monitoring or ChatGPT automation.

This release starts milestones A/B of the design with a CLI and a deliberately restricted candle simulator. It does not complete the full v1 specification or establish any daily return expectation.
