# Testnet readiness — 2026-09-11

Current scope: unsigned connectivity check, separate from all paper services:
`python3 -m gptsalov.testnet_check`

Only hard-coded https://demo-fapi.binance.com time/exchangeInfo GETs exist.
No signed client, credentials loading, order submission or production mode exists.

## Execution implementation gates (not yet passed)
- Separate Testnet-only credentials provisioned on VPS; never paste secrets in chat.
- Durable client order ID and intent written before submitting an entry.
- Timeout/unknown 503: query existing order; never blindly retry entry.
- Reconcile actual fills, partial fills and position after reconnect/restart.
- Round quantity against exchangeInfo filters at submission; validate min notional.
- Verify one-way position mode, isolated margin and 2x before any test entry.
- Protective conditional stop acknowledged at exchange; on rejection reconcile and
  reduce-only flatten the filled amount. Do not assume an HTTP timeout means no fill.
- Verify no duplicate entry across restart, disconnect, delayed response and retries.
- Stop/target order lifecycle must use current supported conditional endpoints.
- Production execution remains unsupported; separate review/authorization before live.

Strategy gate: compare prospective baseline/1R/1.5R/2R, include all costs and locks,
report sample size and drawdown. Do not choose best threshold from a few wins;
reserve a later observation period without retuning. No automatic promotion.

Official references checked:
https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info
https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade
