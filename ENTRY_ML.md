# Entry ML v1

Offline, standard-library L2 logistic classifier for profitable simulated signals.
No order API, live gate, automatic promotion, or scheduled retraining is installed.

Run on a machine with a research database:

```sh
python3 -m gptsalov.entry_ml --db /path/research.db --output /path/run-001.json
```

The database is opened read-only. Use a unique output filename per experiment.
The JSON stores feature names, scaler, coefficients, data fingerprint, chronological
split ranges, evaluation eligibility, and modeled signal metrics.

Only completed four-hour observation cohorts are used. The dataset stops before
the earliest unresolved mature signal, preventing a backlog from selecting only
fast exits. Invalid/expired records remain excluded; missing data may still bias
results. Do not treat this classifier as calibrated win-rate estimation.

Fixed experiment: first 60% / next 20% / last 20% of distinct observation times;
purge four hours before both boundaries and check label completion before them.
Fit scaler and L2 logistic coefficients on training only (minimum 40 samples,
both classes). Require at least 20 validation and 20 test samples before evaluation.
Choose among thresholds 0.4/0.5/0.6/0.7 on validation simulated net, retaining at
least 10 signals. Report selected and all-signal baselines on held-out test data.
These minima are software eligibility gates, not statistical evidence of adequacy.

Feature whitelist: ATR fraction, volume ratio, compression ratio, hourly direction,
EMA distance, four-bar return, side and strategy indicators. No outcome fields are
features. Strongly correlated overlapping signals are not independent trades.
Metrics are simulated sums, not deployable portfolio returns. Repeated manual
training reuses the holdout and must be treated as exploratory, not new evidence.
A frozen model requires prospective evaluation before any Testnet integration.

Deploy by copying gptsalov/entry_ml.py and tests/test_entry_ml.py into the isolated
research release, run tests, then invoke the command above. Existing trading and
scanner services do not import the module and require no restart.
