# Bounded Testnet execution check
One ETHUSDT BUY/SELL round trip for order-lifecycle validation, NOT a strategy
signal, profitability backtest, or autonomous trading service.
Only demo-fapi.binance.com is supported. The dedicated account must start flat
with no open regular or conditional orders, in one-way mode. Set only ETHUSDT
to isolated margin, 2x, then verify settings. Quantity rounds up to exchange
minimum plus 5%, with a hard 25-USDT notional ceiling and existing 0.50 modeled
risk cap; if minimum exceeds cap, skip.
Stop is 0.5% below reference; target 1% above reference. These are test fixtures,
not tuned trading parameters. Immediately after Stop/Target confirmation,
submit one reduce-only exit and reconcile closure/cancel owned conditionals.
Collect actual order/fill IDs, fill prices, realized PnL and commission asset.
An incomplete/uncertain round is reported REVIEW, never silently retried as
a new entry. Re-run with the same journal to reconcile; do not replace/delete it.

CLI:
python3 -m gptsalov.testnet_smoke --journal /dedicated/path/smoke.db
Credentials use APIKEYBD/SECKEYBD from environment. No keys in logs or journal.
Paper research services do not import this runner and continue unchanged.
No continuous trading enabled; the first actual exchange results determine
which endpoint/race handling needs repair before strategy integration.

Verification: 3 new tests cover minimum size/caps, reduce-only exit idempotence,
and refusing exit when entry state is unknown. Existing Testnet tests retained.
Official endpoint reference:
https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade

## Actual Testnet result — 2026-09-14
Initial attempt stopped before plan/entry creation. Read-only inspection showed
no position/order, and ETHUSDT settings had become ISOLATED/2x. The exact initial
ValueError reason was not captured; setting propagation was a hypothesis.
The runner now skips already-correct settings and allows three read confirmations.

Second attempt used the same journal and completed:
- BUY 0.009 ETH at 2508.02; order 16793494295.
- STOP_MARKET 2495.22 and TAKE_PROFIT_MARKET 2532.84 acknowledged/confirmed.
- First target lookup required another observation; no duplicate target submitted.
- Reduce-only SELL 0.009 ETH at 2507.76; order 16793494322.
- Realized gross PnL -0.00233999 USDT; commissions 0.00902887 + 0.00902793 USDT.
- Net -0.02039679 USDT in virtual Testnet funds.
- Final account flat; no regular/conditional orders remaining.
- 96 tests passed on VPS before this actual round.
The new setting-confirmation regression test additionally verifies no repeated writes.

This proves one successful execution lifecycle, not stop-trigger performance,
failure recovery under every race, strategy profitability, or live readiness.
Automated strategy remains disabled; paper research services continue separately.
Journal: /home/wamalana/GPTsalov/data/testnet-smoke-20260914.db
