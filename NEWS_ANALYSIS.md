# News context for the Testnet research pilot

The RSS collector polls CoinDesk and Cointelegraph every 15 minutes. The new
`news_analysis` worker reads the existing news database and uses the configured
OpenAI Responses model to classify up to 16 recent headlines once per UTC hour.
The owned 0600 configuration remains on the VPS; no credentials belong in Git.
The existing `daily_calls` limit is honored (24 in the deployed configuration).
Reservations are committed before calls, including failed attempts. There are no
automatic API retries. The worker sends only public headlines, publication times,
sources and URLs, not account state or credentials.

Every result cites an input article ID. Unknown IDs, unsupported symbol attribution,
malformed results and refusals are rejected. Symbol relevance is matched with word
boundaries and an explicit alias list; common-word tickers are treated conservatively.
Broad macro, exchange and stablecoin headlines are market context, not evidence
about every individual asset. Coverage is limited to headlines, not full articles.

`multiagent_shadow.analyze` and the existing Testnet execution gate now attach
current per-symbol news evidence and a CONTEXT / CAUTION / ABSTAIN news vote.
The market scanner records the same evidence IDs. These votes are advisory:
they cannot create a signal, override any veto, increase size or unlock a batch.
`llm_enabled` is true only when validated, fresh AI evidence is attached to that
symbol's assessment. Missing/stale/failed AI falls back to explicitly labeled
headline context, not a fabricated AI result.

News sources must have succeeded within 30 minutes. Headlines expire after 24
hours; AI results expire after 75 minutes and are not reused after a headline
changes. The office dashboard distinguishes RSS status from AI completion and
shows the Thai summary, model and analysis time. A fresh AI cache may still have
no relevant article for a particular asset.

## Deployment

Stage an isolated release, preserving the existing Testnet policy and ledger.
Install `deploy/gptsalov-news-ai.service` and `.timer`, with `news-ai-current`
pointing to that release. Run one review and verify `COMPLETED` before enabling
the timer. Update WorkingDirectory overrides for pilot, scanner and shadow to
the tested release. Use `--symbols BTCUSDT ETHUSDT` for shadow observations.
Keep the existing news collector and schedule. The AI timer runs at minute 03,
after the hourly RSS refresh, and sends no scheduled chat reports.

The office dashboard has a separately deployed version. Copy that release first,
overlay the news modules and run `scripts/patch_office_news.py` on the copy. It
fails on unexpected source drift, preserves the existing UI and handles v8's
list of active slots. Keep the previous WorkingDirectory overrides for rollback.
No batch reset, strategy change, production orders or risk-limit change is part
of this deployment.

## Validation

Run `python3 -m unittest discover -s tests -q`. New cases cover stale/future news,
source failure, symbol collisions, fabricated citations, changed headlines,
API failure reservations, daily limits, restart deduplication, expiration,
per-symbol observation keys and preservation of risk locks.

Research follow-up: evaluate the recorded news flags on a new, predefined sample.
Do not claim a profitability improvement from attaching news or promote AI votes
to execution vetoes without a separate paired evaluation.

## Deployment evidence (2026-09-28 UTC)

- Unit suite: 229 tests passed; the 10 news integration cases also passed on VPS.
- Live AI: configured `gpt-5.6-terra`, validated `COMPLETED` with 16 headline items.
- Live dashboard `/api/status`: RSS `current`, `llm_connected: true`, AI `COMPLETED`.
- BTCUSDT and ETHUSDT shadow observations: `llm_enabled: true`, news `CAUTION`,
  while the existing batch risk gate correctly kept both decisions `BLOCKED`.
- Pilot, monitor, shadow and hourly news AI timer active. Existing 30-trade batch
  lock and equity 48.62079409 preserved; no orders or batch reset performed.
- Prior pilot database and service override manifest preserved under
  `data/news-deploy-20260928T170106Z` on the VPS.

The first staged request failed validation and was not consumed as evidence.
The next hourly attempt completed after bounding reasoning and supplying allowed
asset names. The first response was not retained, so its exact failure cause is
unknown. Subsequent responses are retained privately on VPS for offline validation
diagnostics, avoiding another API call during debugging.
