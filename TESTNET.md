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

## Execution component added 2026-09-12 (development branch)
`gptsalov.testnet` now contains a fixed-host HMAC client, exclusive SQLite
action journal and a single-plan Coordinator. No paper service imports it.
It is NOT yet a continuously running execution service or a validated exchange
integration. The earlier "no signed client" description above describes the
preflight-only release; the components below are new and dormant.

Implemented and tested against a simulated exchange:
- Validate dedicated flat one-way account, isolated 2x, quantity/price filters.
- Plan expires after 60 seconds; entry price drift gate; fixed 100 USDT notional
  and 0.50 USDT modeled risk caps. No margin/leverage settings changed automatically.
- Persist action before POST; query by stable client ID after ambiguous response.
- On partial fill, submit close-all Stop then cancel unfilled entry remainder.
- Confirm Stop; place take-profit only once Stop is confirmed.
- Verify actual average fill, position amount and modeled risk.
- If protection/fill checks fail after entry is terminal, attempt one reduce-only
  emergency exit; reconcile on subsequent calls. Never blindly repeat exit.
- Cancel only owned conditional orders when flat. Cleanup failure requires review.
- Bind journal to hashed API-key identity, immutable plan and exclusive writer.
- Reject production host paths; block redirects; sanitize transport errors.

Remaining blockers before enabling:
- Dedicated Testnet key provisioning and signed account/endpoint contract tests.
- Integrate a bounded continuous supervisor (including disconnect and alarm policy);
  process downtime before Stop acknowledgement can leave exposure unprotected.
- Reconcile trigger/fill races on the real Testnet, unexpected positions, partial
  exit, stale API responses and retention/not-found cases. REVIEW/UNCERTAIN are
  blocking states requiring inspection, not successful recovery guarantees.
- Connect strategy sizing/risk locks to the executor; implement realized account
  cost reporting and compare fees/funding to the paper assumptions.
- No production activation or paid AI service. Do not use this component live.

Provision ONLY a dedicated Binance Demo/Testnet key interactively on VPS:
`python3 scripts/provision_testnet_key.py`
Input is hidden and saved mode 0600 to ~/.config/gptsalov/testnet.json.
No API call occurs, and no existing file is overwritten.
Never paste keys/secrets into chat. No credential file is tracked in GitHub.
