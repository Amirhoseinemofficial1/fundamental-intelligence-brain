# V52 Architecture

## 0. Intelligence topology

```text
news + structured + market observations
  -> canonical observation (event/available/ingestion time)
  -> dynamic asset mapping + event graph/analogues
  -> point-in-time feature vector with provenance
  -> specialist Champion artifacts + identical-stream Shadows
  -> Meta Champion (controlling probability)
  -> uncertainty/no-trade + Entry/Exit policy artifacts
  -> signal lifecycle -> reference-price response
  -> dataset -> challenger -> unseen Shadow outcome -> promote/reject/rollback
  -> failure/missed/post-exit/hypothesis/knowledge/daily review
```

The realtime data plane, intelligence plane, research/training plane and control plane have separate stores and failure boundaries. A missing optional model or LLM dependency degrades to an explicit state and cannot fabricate a model prediction.

## 1. Process topology

V52 uses process isolation rather than one giant asynchronous process. The Supervisor owns lifecycle only; it does not perform collection or persistence itself.

### News plane

```text
Tier-0 / priority feeds ──> news_hot ───────┐
Coverage feeds ───────────> news_coverage ──┼──> durable raw_news (SQLite/WAL)
Discovery sites ──────────> discovery       │
                                            └──> news_processor
                                                   │
                                                   ├──> canonical stories/occurrences
                                                   ├──> durable translation_jobs
                                                   ├──> durable enrichment_jobs
                                                   └──> durable alerts
```

`news_hot` and `news_coverage` persist batches directly into the durable raw-news database. Duplicate suppression is a database uniqueness constraint, not an in-memory `_seen` decision made before persistence.

### Restored realtime news/social planes

Alongside `news_hot` and `news_coverage`, V52 FULL runs isolated `gdelt`, `google_mesh`, `x_social`, `bluesky`, `social_pollers`, and `sitemap` producers. All accepted observations converge on the same durable raw-news store, so a failure in one producer does not bypass durability or block another producer.

### Downstream plane

```text
translation_jobs ──> translation ──> story translation revision/cache

enrichment_jobs  ──> news_enrichment ──> story enrichment revision

alerts           ──> alerts ──> Telegram
```

All three queues use durable states (`pending`, `leased`, terminal state) with lease expiry so work is reclaimable after worker failure.

### Structured/public-data/institutional plane

- `structured`: generic free/provider-registry request adapters.
- `institutional_connectors`: restored V34/V35/V36 authorized REST/WS/node connector contracts.
- `provider_flows`: smart-money/on-chain provider flows and configurable provider-query contracts.
- `premium_actions`: V33 Wall-Street/whale action connectors when credentials are configured.
- `prediction`: Polymarket REST/WS and Kalshi market intelligence.
- `official_native`: native official/macro/regulatory adapters.
- `altdata_native`: public alternative-data and dataset-surface adapters.
- `filings_native`: issuer filing/public disclosure surfaces.

Structured persistence is independent from the raw-news queue.

### Market plane

Each market service owns an independent writer pair:

```text
Exchange WS/REST
   │
   ├── raw wire -> bounded queue -> append-only segmented raw journal -> fdatasync
   │
   └── parsed semantic events -> bounded batch writer -> per-service SQLite/WAL
                                   └── persisted sequence/recovery checkpoint
```

No market exchange shares an in-memory writer queue with another exchange.

## 2. Supervisor contracts

The Supervisor:

- creates one run id and run directory;
- starts only canonical V52 services;
- passes its PID + `/proc` start ticks to children;
- monitors status heartbeat identity (`service`, `pid`, `run_id`);
- terminates/restarts stale processes;
- applies a restart budget/window;
- fails the run when the budget is exhausted;
- writes resource samples and a final lifecycle report.

A live PID alone is not treated as proof of service health.

## 3. Shutdown ordering

Shutdown is dependency-aware:

1. Stop external producers (news collectors, structured/native collectors, discovery, and all self-contained market processes).
2. Allow committed raw news to drain into the story/control database.
3. Stop `news_processor`.
4. Allow already committed translation/enrichment/alert jobs to drain.
5. Stop downstream services.
6. Force-kill only after the global shutdown deadline; any forced kill is recorded and fails the production quality gate.

Market processes internally stop producers before closing raw and semantic writers, then assert persistence invariants.

## 4. Storage layout

Default runtime root: `/var/lib/v52-fundamental` under systemd.

```text
db/
  news.sqlite3
  control.sqlite3
  structured.sqlite3
market/
  market_<venue>/
    raw/
      *.jsonl
    semantic.sqlite3
state/
  <service>/status.json
  <service>/final.json
logs/
  <run_id>/*.jsonl
runs/
  <run_id>/
    run.json
    resources.jsonl
    supervisor_final.json
    quality_gate.json
    services/<service>/final.json
diagnostic_bundles/
```

SQLite uses WAL, busy timeout, foreign keys, and production synchronous mode `FULL` or `EXTRA`.

## 5. Failure domains

- A broken feed affects its source health/circuit state, not the entire news service.
- A broken market venue cannot block another venue's writer.
- Translation capacity cannot block raw-news ingestion.
- Telegram failures leave alerts durably pending/retryable.
- Processor failure leaves leased raw rows reclaimable.
- A process hang is caught by heartbeat watchdog even if the PID remains alive.
- A writer failure propagates as service fatal; it is not logged-and-ignored.
