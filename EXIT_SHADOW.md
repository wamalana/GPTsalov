# Paired exit simulation v1

Research only. Reads the existing v8 Testnet journals using SQLite `mode=ro`.
Uses unsigned public production 1-minute candles only; no credentials and no
order endpoints. The Testnet pilot, signals, sizing, risk and slots are unchanged.

## Predeclared experiment

Each accepted Testnet entry plan (entry ACK) creates a paired simulated trade.
An ACK is NOT proof of a complete Testnet fill: this measures accepted signals,
not a reproduction of the live fill ledger. Both arms use the same quantity,
direction and next full production minute's open, with 3 bps adverse entry slip.
Stop/target percentage distances from the plan's reference price are reanchored
at this shared simulated entry. Production and Testnet prices differ; these
results must not be presented as actual Testnet PnL.

* `fixed_4h`: original stop and target, otherwise close at 240 minutes.
* `trail_1r_4h`: identical target and 240-minute maximum; activate trailing
  after a completed candle reaches +1R, then follow the favorable extreme with
  a 1R distance. R is the original price-to-stop distance, not fee-adjusted risk.
  The updated stop applies from the NEXT candle; it never loosens.

Stops win if stop and target occur in the same minute. A gap through a stop
fills at the worse open. Exits include 3 bps adverse slippage; both sides pay
5 bps fee. A 15 bps entry-notional funding reserve is deducted per trade.
Funding is a conservative fixed assumption, NOT historical funding payments.
This first experiment isolates trailing with the same maximum hold time; it
does not yet test extending trades beyond four hours.

## Excursions and validation

MFE = maximum favorable price excursion, MAE = maximum adverse excursion,
in USDT before fees. Full closed bars before exit are observed. Intraminute
ordering on the exit candle is unknown: lower bounds use prior bars and the
exit price; upper bounds include the whole exit candle and can include prices
AFTER exit. TIME exits include the whole final candle. These are bounds, not
tick-accurate extrema. Stop-first handling is deliberately conservative.

Missing/noncontiguous candles block that sample; incomplete minutes are never
used. Stored source plans, raw candles and candle hashes allow audit/replay.
Historical accepted plans are explicitly separate from forward observations
after experiment creation. Completed results are immutable on subsequent runs.
Any source/experiment policy change requires a new research ledger.

Aggregate comparisons include only pairs where BOTH arms closed successfully.
Open/error samples remain visible and excluded from the paired score. Report
paired net difference, profit factor and exit reasons for each cohort. These
are trade-level comparisons, not portfolio returns: overlapping hypothetical
trades, capital reuse, slot allocation and risk-lock counterfactuals are not
simulated. Input is selected by the existing pilot, with its selection bias.

Do not promote from the historical sample or tune trailing parameters on these
same trades. Review the first 30 completed forward pairs as exploratory evidence;
do not interpret that count alone as proof of profitability. Keep all samples,
including errors and ambiguous exit bars, in any review.

## Operations

`python3 -m gptsalov.exit_shadow run --source /path/to/pilot --db /path/to/research/ledger.db`

`python3 -m gptsalov.exit_shadow status --db /path/to/research/ledger.db`

Install the provided oneshot service and timer after setting its WorkingDirectory
to the reviewed release. Every minute it backfills or advances incomplete pairs.
No pilot restart or policy migration is needed. No user reports are scheduled.
