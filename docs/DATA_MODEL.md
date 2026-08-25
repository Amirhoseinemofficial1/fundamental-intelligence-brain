# Data Model

The brain database is `data/brain/intelligence.sqlite3`, initialized by `BrainStore` with schema migration records. Core lineages are:

- `canonical_observations` enforces `event_time <= available_time <= ingestion_time` in the domain object and retains source, assets, value, quality, raw reference and replay marker.
- `events`, `event_updates`, `event_assets`, `event_entities`, and `relations` retain event threads, asset mappings and graph provenance.
- `feature_vectors` records version, prediction cutoff, values, quality and all contributing observation IDs.
- `datasets`, `model_artifacts`, `expert_outputs`, and `shadow_predictions` retain reproducible learning and inference lineage.
- `predictions`, `signals`, `signal_lifecycle`, `response_targets`, `reference_prices`, and `market_responses` close the outcome loop.
- `experiments`, `hypotheses`, `knowledge`, `failure_attribution`, `missed_opportunities`, `post_exit`, `improvement_records`, and `daily_reports` retain governed learning.

SQLite foreign keys, WAL, uniqueness keys and transactions provide restart persistence and idempotency. Secrets and raw `.env` files are excluded from releases.
