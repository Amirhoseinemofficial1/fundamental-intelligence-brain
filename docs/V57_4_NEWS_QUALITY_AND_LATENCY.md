# V57.4 news quality and latency contract

## Production finding before this change

The stopped V57.2 installation on 2026-09-20 held 539,749 raw news rows and
158,341 stories from its previous run. 133,261 stories (84.2%) had materiality
2 or 3. Sitemap sources dominated raw volume. Examples included generic car
loan shopping, political commentary, and old articles. These counts were read
from the production SQLite databases; they are a baseline, not a claim about
the new release. The sitemap plane is disabled by the bounded production
profile in V57.3.

## Implemented boundaries

1. The hot direct collector, Google mesh and GDELT now apply one admission
   decision *before* SQLite insertion. A qualifying item needs a specific
   fundamental event, materiality at least 4, and a publication time no more
   than one hour old. Untimed first-party items need materiality at least 7.
   The processor repeats this decision so older persisted pending rows cannot
   bypass it during an upgrade.
2. Atom `updated` is no longer used as the original publication time. An item
   with only an edit timestamp is treated as untimed.
3. A time-sensitive Telegram trading signal needs a first-party source,
   verifiable publication and observation times, and age at send decision of
   at most 60 seconds. Google News, GDELT and unverified social posts may still
   contribute discovery and delayed, labelled evaluation, but cannot alone
   trigger a live trading signal.
4. OpenAI API semantics are disabled in code, and disabled in both the default
   example and bounded production profile. The local deterministic event
   classifier and 64-dimensional text embedding feed the existing delayed
   outcome learner. No paid OpenAI request is needed.
5. `scripts/news_sla_report.py` reports acquisition time, local processing
   delay, queue age and repeated source failures from the actual databases.
6. On the target host, all six GDELT searches repeatedly opened their circuit
   and provided no new items. The bounded production profile disables GDELT;
   direct feeds and Google fallback continue independently. The adapter stays
   available for a future measured re-enablement.
7. A short live audit found Bluesky posts in the story ledger that quoted or
   speculated about market events without an independently verified release.
   The bounded server profile disables social pollers, X, Bluesky, Reddit,
   Mastodon and NewsAPI to keep unverified commentary out of learning. Their
   adapters remain in code for controlled source-specific evaluation.
8. The direct-feed registry also contained a GDELT mirror even while the
   separate GDELT service was disabled. That mirror is now excluded by the
   same configuration switch and counted as an aggregator in the SLA report.
   Repeated 403/404/open-circuit responses now use a long probe interval; `Retry-After`
   from a 429 response is respected even when it exceeds the hot poll cap.

## Live production audit and learning-noise repair

After roughly ten hours of the first V57.4 run, the brain contained about
40,600 internal predictions and signals, all in the observation mode and
none delivered as a trading signal. A generic prediction-market-state event
accounted for over 31,000 predictions because thousands of provider updates
were merged into one event and inferred repeatedly. The brain DB reached
about 588 MiB. This is an observed defect in the learning input, not evidence
of 40,600 independent fundamental events.

Prediction-market snapshots and bare SEC current-filing index rows now remain
in the raw structured database for audit, but do not enter the canonical
learning stream or trigger event and prediction fan-out. Polymarket and Kalshi admission excludes unrelated sports,
entertainment and other markets before they are stored. Repeated structured
or market updates still enter the event-update record; they do not repeat
inference within one hour during observation, or within five minutes in
shadow/live mode. Historical generic prediction-market and bare-filing
predictions are excluded from the signal-outcome learning query and new
barrier-evaluation queue. Existing historical rows remain in the database for
audit and rollback.

The feature builder now ignores numeric values from unknown observation types
when calculating an asset's feature quality. This change has a new feature
version; training reads only outcomes produced under that version, and runtime
inference on the production profile uses only model artifacts trained on the
matching feature version.
The ten old shadow artifacts observed on the server remain stored for audit
but cannot influence new-version predictions.
The live audit also found unparsed CFTC positioning tables dominating the
canonical stream. Their source rows stay in the structured database under its
retention policy; until contract, period, side and net position are normalized
into asset-specific features, those generic table rows do not enter the brain.
SEC fails-to-deliver records were another large, delayed bulk source with no
implemented asset-specific feature. The production profile stops its polling;
previously stored FTD rows are skipped by the canonical bridge.

The prediction-market topic gate is intentionally conservative. A title alone
cannot establish a tradable surprise, direction, asset mapping or causal
effect. Actual signal qualification still requires the full feature,
evidence, learning and live gates.

## What the 60-second target means

`publisher_to_first_observed` compares the publisher's `published_at` with
the collector's `first_observed_at`. The publisher controls its publication
timestamp and RSS/API update schedule; polling every 10 seconds does not make
an upstream feed publish within 60 seconds. A first-party direct feed can be
measured against the target. GDELT and Google are useful coverage and backstop
sources, but should not be cited as proof of original publication latency.
GDELT's broader data platform documents 15-minute update intervals, so an
unqualified sub-minute guarantee across that path would be false.
The report excludes social and structured events from news latency statistics;
it reports their count separately. A target result requires at least 30 timed
direct news items over a 24-hour window, not one fast item.

Run hourly and daily reports:

```bash
/opt/v57-fundamental/.venv/bin/python /opt/v57-fundamental/scripts/news_sla_report.py --hours 1
/opt/v57-fundamental/.venv/bin/python /opt/v57-fundamental/scripts/news_sla_report.py --hours 24
```

Operational acceptance is: direct-source p95 acquisition latency at or below
60 seconds, local observed-to-story p95 below 5 seconds, pending queue age
below 30 seconds, no sustained source failures, and review of missed priority
releases against an independent official calendar. These are targets requiring
time-series evidence, not assertions established by unit tests or a short run.

## Learning and release sequence

The local classifier, semantic vector and supervised delayed outcome updates
are distinct from an LLM that reads and reasons like a person. There is no
claim of human-equivalent understanding or guaranteed profitable signals.
First collect live source and price evidence, then allow the existing 24-hour
observation and 24-hour shadow windows to run. Promote a model only after the
existing sample, calibration, unseen-outcome, drift and champion checks pass.
Do not bypass those gates to send early trading signals. Compare shadow
precision, false-positive rate, calibration and net performance with fees to
the existing baseline before enabling live delivery.

## Next source work after sustained measurement

Add or tune official event feeds based on actual missed-event audits. Prefer
first-party central bank, regulator, exchange, filing and issuer releases.
Keep publisher-specific rate limits and conditional GETs. If an official feed
does not expose original publication time, acquire another verified source
before allowing a sub-minute live signal. Measure the full
publisher-to-collector-to-story-to-alert path continuously.
