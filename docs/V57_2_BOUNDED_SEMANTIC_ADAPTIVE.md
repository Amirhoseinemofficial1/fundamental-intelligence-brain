# V57.2 bounded semantic adaptive release

## Production failure and correction

The previous host did not stop because Linux killed it for memory. Several
service processes simultaneously executed `PRAGMA journal_mode=WAL` while
initializing shared SQLite databases. That operation requests an exclusive
lock. The intelligence process exited with `sqlite3.OperationalError: database
is locked`, systemd retried it repeatedly, and the service hit its start limit.

V57.2 performs one systemd bootstrap before the supervisor fans out, serializes
each schema initialization with a stale-safe adjacent lock, and does not repeat
the WAL transition on every short-lived connection. SQLite has a 60-second busy
timeout and every store retains transaction rollback behavior.

## Bounded data contract

- Raw exchange websocket journals are disabled in the production profile.
- Market semantic state is sampled at a five-second floor and upserted by
  venue, symbol, event type and time bucket. Books retain aggregate measures and
  three top levels instead of every received depth message.
- Structured state is keyed by provider, type, stable entity and five-minute
  bucket. Immutable filings/records deduplicate across time. Oversized payloads
  retain scalar evidence, representative samples, byte count and SHA-256.
- News keeps the title, summary, canonical/source links and timestamps as first
  class columns. Oversized transport envelopes are capped at 64 KiB with their
  original size and SHA-256 retained. A shared transactional admission check
  caps pending plus leased news at 20,000 rows across every collector, so
  concurrent backfills cannot bypass per-service backpressure.
- Only three highest-impact assets and three representative horizons are
  evaluated per material event. During observation, one horizon is used. Low
  materiality general-news fanout is suppressed; curated official structured
  observations keep one bounded path.
- Market semantic evidence is retained for three days, completed operational
  queues and control-plane story copies for two days, reference prices for
  seven days, and runtime/log directories for three days. Processed
  news transport payloads are released immediately while their title, summary,
  canonical link, source and occurrence evidence remain durable. A
  five-minute disk guard stops the service cleanly if V57 data exceeds 12 GiB
  or root free space falls below 15 GiB.

## Semantic and adaptive learning

Material news is parsed through the configured OpenAI Responses model using a
strict schema: event class, entities, mapped assets, causal chain,
counter-evidence, regime dependencies, horizon rationale and directional
hypotheses. A 64-dimensional embedding is persisted by content hash and linked
to the event. Local validation still controls the canonical event/asset set.

The local delayed-feedback model is a fully connected contextual network with
76 inputs, 32 hidden tanh units and seven output heads for the symmetric 1:1
levels 0.1%, 0.3%, 0.5%, 1%, 3%, 5% and 10%. Inputs include event quality,
surprise, regime/risk context and the 64 semantic dimensions. It is updated
only after candle evidence resolves a paper barrier. Ambiguous candles never
become wins. MFE, MAE, timeout, source and exact feature identity are retained.

Learning reports are evidence-triggered rather than repeated templates. A new
report requires at least 100 delayed updates and six hours since the last
report; it summarizes measured event/asset/level patterns and remains labeled
shadow until governance qualifies the model.

This design does not train an LLM on an 8 GiB server and does not promise
profit. It uses an external language model for bounded semantic features and a
compact local adaptive network for measured market-response learning. Model
promotion remains controlled by walk-forward/shadow validation, calibration,
drift checks and rollback-capable champion/challenger governance.

## Fresh-start behavior

Deleting `/var/lib/v57-fundamental` resets observations, feature vectors,
signals, outcomes, semantic memories, models and both warm-up counters. On the
next service start the schema bootstrap recreates empty stores. The runtime
then spends 24 actual running hours in observation and 24 actual running hours
in shadow. Downtime and replay do not advance those counters.
