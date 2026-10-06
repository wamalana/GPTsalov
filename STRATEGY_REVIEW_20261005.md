# Strategy review, 2026-10-05

Research only. No trading policy or lock changes. The API_OR_STATE_REVIEW lock
requires separate exchange reconciliation; this work does not clear it.

## Completed

VPS read-only audit: 1,872 completed simulated signal outcomes.

| Strategy | Signals | Gross incl modeled slippage | Fees | Funding reserve | Net |
|---|---:|---:|---:|---:|---:|
| Baseline | 1110 | -41.8730 | 30.2484 | 45.3981 | -117.5195 |
| Pullback | 550 | -17.9941 | 17.1050 | 25.6596 | -60.7586 |
| Compression | 212 | -14.1417 | 6.3192 | 9.4723 | -29.9332 |

These are overlapping fixed-price-risk signal sums, not account returns. Funding
is a conservative reserve, not actual historical funding. Gross already includes
simulated slippage, which is not separately identified in stored outcomes.

Of 778 losing baseline signals, 164 definitely reached 1R favorable excursion
before exit. This does not imply a trailing stop would have saved them all.
Baseline low ATR fraction (<0.005): 311 signals, net -69.9136, PF 0.2010.
A fixed cost/risk screen retained 414 baseline signals: net -12.2683, PF 0.8185.
Different group sizes mean sums alone must not be used as an improvement claim.
Mean net per baseline signal improved from -0.1059 to -0.0296 in this exploratory
screen; neither group is profitable. No in-sample screen is a validated strategy.

## Paired public-candle replay

Most recent 60 completed baseline signals with full 240-minute data:
- Fixed exits: -11.0954 simulated USDT.
- Trail activated at 1R, distance 1R: -9.3023, improvement +1.7931; still negative.
- Reference-price retest within 15 minutes, next-minute entry: 44 entries and
  16 skips, net -8.8760. Same 44 signals at original entry: -8.6206.
- Retest does not help on matched entries. It retests the signal reference, not
  the original breakout level. All entries share original four-hour deadline.

No sampling independence or confidence claim; only 60 recent signals, one period.

## ML

Frozen Oct 3 model evaluated on 1,197 newer mature signals using unchanged score
thresholds: threshold 0.4 retained 14 signals, net -1.5605, PF 0.3547. Higher
thresholds retained zero. Model scores were replayed after outcomes; model weights
were fixed before these observations, but this was not a live prediction trial.

New training run: 1,823 eligible samples; train 961, validation 203, test 358 after
purging. None of the original thresholds retained 10 validation signals. Test
performance remains unreported; do not lower thresholds to manufacture success.

## Frozen next experiment

Start: 2026-10-05T10:00:00Z. Use already-recorded baseline signals observed at or
after this time, independent of their later outcomes. Screen:

    cost_fraction = 2*0.0005 + 2*0.0003 + 0.0015
    stop_fraction = abs(reference-stop)/reference
    accept iff cost_fraction/stop_fraction <= 0.25

Keep original fixed exits and original simulated sizing. Compare all baseline
signals with accepted and rejected groups on the SAME future observation window.
Report sample counts, mean net, PF, cost decomposition and day-level variability.
Do not count unresolved outcomes as zero or compare only early exits. Preserve
invalid/expired counts. First review after >=7 complete days and >=200 accepted
closed signals, whichever takes longer; these are review gates, not proof.

Existing recorder already retains the required features, stops, observations and
outcomes, so no execution changes or new scheduler are needed. This document
freezes the hypothesis; it does not install automatic evaluation or retraining.
Retest, trailing and ML are not promoted to the trading path.
