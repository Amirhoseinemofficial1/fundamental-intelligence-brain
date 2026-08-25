# Historical Import and Replay

`HistoricalImporter` accepts JSONL canonical observations and CSV reference prices. Both formats require event, available and ingestion timestamps; invalid ordering fails rather than being silently repaired.

`ReplayEngine` emits observations in `(available_time, observation_id)` order, marks every row as replay and can preserve scaled inter-arrival timing. Replay data may populate feature/backtest evidence but does not advance live observation or Shadow runtime counters. Import row/file provenance is retained.
