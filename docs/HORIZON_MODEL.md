# Horizon Model

Each supported horizon has an independent `horizon_score:<seconds>` regression Artifact trained from its own point-in-time response labels. Runtime scores all event candidate horizons and selects a preferred horizon. All per-horizon predictions remain internal for learning; `SignalConsolidator` ranks them and permits one user-facing event/asset message.

Supported targets are 30s, 1m, 3m, 5m, 15m, 30m, 1h, 4h, 12h, 24h and 48h.
