# Risk-aware multi-agent candidate v1

> 2026-09-18 update (see STRATEGY_V2.md): historical replay of this exact selection
> logic over 20 symbols, Jan 2024-Aug 2026, gives -0.057R per trade (CI -0.075..-0.038,
> 4,769 trades). Performance-based risk promotion in `adaptive_risk.risk_allowance` is
> now disabled and slots are limited to one position per direction.


Status: integrated with the deployed multi-market/adaptive-stop source snapshot
and tested. Enabled explicitly with `--multi-agent --multi-market --risk-aware`.
This is a research candidate, not evidence of improved profitability.

## Findings and changes

The existing trend and momentum agents recheck the same hourly EMA and 15-minute
breakout inputs as the baseline. Their agreement is not independent evidence or
a calibrated win probability. The existing exchange setup fixes leverage at 2x.
This candidate retains the baseline signal and adds:

1. A strategy-quality veto: prior 20-bar directional efficiency >= 0.25,
   breakout extension > 0 and <= 0.75 prior ATR, and current true range <= 3
   prior ATR. These are explicit unvalidated hypotheses, not optimized settings.
2. Risk sizing with fees, stop slippage, funding allowance and worst accepted
   entry drift. Net reward/risk must be >= 1.2 after these allowances.
3. A margin/leverage gate that selects 1x or 2x without increasing the risk-sized
   quantity. If margin cannot support that quantity, it reduces quantity or
   rejects the trade. It never rounds up to satisfy a minimum order.
4. Recorded quality votes, risk budget, modeled loss, margin, cost reserve,
   effective leverage, stress surplus and selected exchange leverage.

News and LLM opinions remain advisory. No paid API integration is added. No
changes to account mode, duration or trade-count limits are made by this patch.
Existing adaptive stops and symbol routing remain active. The legacy adaptive
ceiling of 2 USDT remains in its policy for compatibility; the new final risk
gate restricts new orders to <=0.25 USDT, including when the adaptive promotion
test would allow more. Existing risk locks are preserved.

## Calculation

For a linear USDT contract, let E be virtual strategy equity, Q quantity, P
entry price, S stop, T target, L exchange leverage. A larger L primarily reduces
initial margin for a fixed Q; it does not improve the signal or expected profit.

Risk budget = minimum of:

- 0.25 USDT;
- 0.5% of min(E, 50 USDT), halved at >=4% drawdown or >=2 consecutive losses;
- equity remaining above the existing 2% daily-loss threshold;
- equity remaining above the existing 8% high-water drawdown threshold.

The pilot must be flat and unlocked. Three consecutive losses remain a block.
The one-position rule also prevents overlapping BTC/ETH/altcoin directional risk;
this version does not claim to estimate a covariance matrix or diversify assets.

The risk engine uses adverse entry movement of 0.5% (the accepted execution
drift limit), stop and target execution slippage of 0.1%, fee allowance of 0.08%
per leg, and a funding allowance of 0.1% of entry notional. These are modeling
assumptions, not current exchange fee quotes or funding forecasts.

Unit loss = adverse entry-to-stop-fill loss + both-leg fees + funding allowance.
Unit reward = adverse target-fill gain - both-leg fees - funding allowance.

Quantity = round DOWN to the valid exchange step of the minimum allowed by
risk budget/unit loss, the upstream size, 25 USDT notional, 50% of virtual
equity, exchange maximums and available margin. Notional limits include the
upper entry-drift bound. Minimum-notional checks use the lower drift bound.

The engine preserves the largest permitted quantity, then chooses the lowest
L in [1, 2] that supports it. Cash use includes margin and a cost reserve and
is capped at min(exchange available USDT, 50% of virtual equity). A large
Testnet faucet balance never increases virtual capital or risk limits.

For example, with E=50, reference=100, long stop=98, target=106, ATR=1, quantity
step=0.001 and a 1 USDT minimum, the implementation returns:

| Available USDT | Quantity | Selected leverage | Margin allowance | Modeled loss |
|---:|---:|---:|---:|---:|
| 50 | 0.087 | 1x | 8.7435 | 0.24858 |
| 5 | 0.087 | 2x | 4.37175 | 0.24858 |
| 2 | 0.039 | 2x | 1.95975 | 0.11143 |

The first two rows have identical position risk. Only the margin requirement
changes. The values are synthetic, not BTC/ETH prices or an order recommendation.

## Exchange and liquidation checks

Authenticated Testnet `GET /fapi/v3/balance` and `GET /fapi/v1/leverageBracket`
must succeed. Missing tiers, invalid values, gaps, overlaps, or a non-unit
custom `notionalCoef` fail closed pending explicit support for that account.
Initial and stressed notional must fit tiers and their leverage caps.

The stress screen moves mark price adversely by max(2 * entry-stop distance,
3 * ATR). Allocated isolated margin must exceed the stressed loss, maintenance
margin and reserved costs. Ignoring the tier `cum` deduction intentionally
overestimates maintenance. This is a conservative screen, NOT the exact Binance
liquidation price. Stop fills and liquidation can still be affected by gaps,
mark/contract-price divergence, exchange outages and funding beyond the model.

Before entry the coordinator rechecks the immutable quantity against fresh
balance and bracket data, confirms isolated margin, and persists any leverage
write before sending it. Leverage is changed only while the dedicated account is
flat with no open orders. A GET must confirm the selected setting, including
after a timeout. An unconfirmed write cannot be retried by this journal or permit
entry. The final price and plan age are rechecked. After fill, the pilot also
honors a reduced risk budget instead of always allowing the full 0.25 USDT.

Binance sets initial leverage **per symbol**, not independently for each order
sharing a position. The pilot's one-position-at-a-time rule is therefore retained.

Official references checked 2026-09-17:

- [Change initial leverage](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Change-Initial-Leverage)
- [Notional and leverage brackets](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Notional-and-Leverage-Brackets)
- [Exchange information and filters](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information)

## Validation and rollout

Run `python3 -m unittest discover -s tests -q` from the repo root.
169 tests pass, covering pure sizing, long/short cases,
64 combinations of side/stop/cash, drawdown, daily loss room, minimum sizes,
tiers, stress, quality vetoes, multi-agent integration and uncertain leverage
writes. Multi-market integration, policy-upgrade persistence and preservation of
existing locks are tested. Existing execution, reconciliation and paper tests
also pass.

The opt-in flag is `--multi-agent --risk-aware` for `gptsalov.testnet_pilot`.
An explicit risk-aware startup permits one narrowly validated policy migration:
adding only the known risk-model key to the existing Testnet multi-agent policy.
All other policy keys must match, the ledger must have no active trade, and the
account must be flat before deployment. The Book writer lock and SQLite
transaction protect the migration. Equity, journals, trade counters, batch
timestamps and locks are retained; no trial batch is automatically reset.
All other policy mismatches fail closed. Take an on-VPS backup before upgrading.
No production trading activation is included.

Before claiming a strategy improvement:

1. Freeze these settings and compare baseline vs candidate on identical
   timestamped data, using chronological train/validation/untouched test windows.
2. Purge overlapping holding periods at boundaries (at least the 4-hour maximum
   hold). Include rejected signals, delisted-contract handling, fee/funding
   costs, spread, failed fills and intrabar stop/target ambiguity.
3. Report net expectancy per trade, drawdown, profit factor, turnover, acceptance
   rate and uncertainty intervals by market regime and symbol. Stress higher
   slippage/costs and test stability in nearby parameter values. Do not select
   parameters on the final test window or silently omit unprofitable pairs.
4. Compare risk at equal virtual capital and exposure. Validate order handling
   in Testnet separately: Testnet PnL alone cannot establish production edge.
5. Keep any additional trend/pullback or range strategy as a separate research
   candidate until it adds out-of-sample value; do not combine more indicator
   votes and label the count a win probability.

No historical profitability backtest or performance comparison was performed.
Deployment validation uses read-only Testnet checks; no synthetic signal or
forced order is submitted to demonstrate the feature.
