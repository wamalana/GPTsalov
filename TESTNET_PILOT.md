# Bounded Testnet strategy pilot v1
Testnet-only ETHUSDT; production orders are unsupported. This is a supervised
development pilot, not proven profitable or ready for real funds.

The pilot uses public production ETH 15-minute closed candles for the existing
EMA/Donchian/volume signal, then checks Testnet price drift and Testnet contract
filters. Entry occurs on observation, not the paper engine's delayed modeled
bar-open schedule; do not claim exact paper/Testnet PnL parity.
Only fresh signals (<=120 seconds since closed candle) are evaluated, once per
candle. No signal means no order; no forced trades to increase sample size.

- Virtual reporting budget: 50 USDT, distinct from actual Testnet account balance.
- Modeled risk per entry <=0.25 USDT; notional <=25 USDT; one active position.
- Net target reward/risk >=1 after modeled costs.
- One-way account, isolated 2x; setup only while flat with no outstanding orders.
- Stop and target are placed at exchange, close-all in the appropriate direction.
- Terminal entry evidence and matching owned closing-order fills are required
  before considering a flat-position response sufficient to cancel conditionals.
- Unknown entry is never automatically resubmitted.
- Risk limits: 2% daily loss, 8% drawdown, 3 consecutive losses.
- Stop new trades after 3 closed trades or 24 hours; no automatic round reset.
- Active position time limit 4 hours; exit is reduce-only and attempted once.
- Latency, duplicate-order rejection, partial exit or unowned state requires
  review. No self-unlock. Active reconciliation continues while new entries halt.

Accounting uses actual fill prices/USDT fees and Testnet funding income within
the owned trade interval. Incomplete fills/non-USDT commission require review.
Open equity is an estimate with conservative fee/reserve allowances; funding
may post later than the first history query. This is not exchange wallet equity.
All trade/account state is in a separate pilot DB plus immutable trade journals.
No main/forward/tuning paper ledger is opened for writing.

Credentials: owner-only regular JSON file, environment=testnet.
The service does not depend on an interactive .bashrc. Never log credentials.
CLI status:
python3 -m gptsalov.testnet_pilot status --directory /home/wamalana/GPTsalov/data/testnet-pilot-v1

Operational limits:
- No user-data websocket; 10-second polling during active trades.
- Exchange protections remain while process is down, but downtime before
  protection acknowledgement can leave exposure unprotected.
- API/state failures lock new entries; no automatic process restart.
- History completeness is checked by quantity, not assumed from an empty result.
- Pilot must be reviewed before broadening symbols/duration/trade count.
Stopping the service alone does NOT close an exchange position. Reconcile it
and its owned protective orders first. Do not delete journals to reset a lock.

Tests cover transient empty position responses, triggered stop evidence,
forced exit accounting, missing entry, incomplete fills, persistent risk locks
and bounded batches. Main paper strategy behavior is unchanged.
