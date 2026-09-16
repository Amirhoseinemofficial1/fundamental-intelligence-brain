# V57.1 engineering notes

The previous server journal reports 7.4 GiB peak memory and 1.7 GiB swap for V57 on 2026-09-08. On 2026-09-16 the V57 service was stopped and its configured `/var/lib/v57-fundamental` database directory was absent. 218 archived diagnostic files were inspected. Historical accuracy cannot be reconstructed from these logs. Eight unrelated-version fundamental services V43–V50 were repeatedly restarting against missing directories and were disabled. Other projects on this host remain outside this deployment.

## Evaluation and learning

Seven independent symmetric price-distance arms: ±0.1%, ±0.3%, ±0.5%, ±1%, ±3%, ±5%, ±10%. This is a paper evaluation of each directional prediction, including internal predictions. It is not a record of broker executions. Entry is the first complete one-minute candle open after prediction, so pre-decision highs/lows cannot contaminate a signal. Intrabar order is unknown: a candle reaching both barriers is AMBIGUOUS and excluded from reward updates. Missing minutes, invalid OHLC, incomplete endpoints and unsupported instruments are retried and ultimately UNOBSERVABLE, never invented wins. Closed-market gaps also remain unobservable until an exchange-calendar-aware provider is added.

Results retain entry, targets, stop, first-hit time, gross/net return, estimated round-trip costs (default 10 bps; configure for the actual instrument), MFE/MAE over the full horizon, times to excursions, candle count, provider and path SHA-256. Gap-through stops use the observed opening price. Costs make net R:R differ from the nominal symmetric price-distance 1:1.

`evaluation_candles` stores OHLC with actual fetched time. `signal_barriers` contains seven outcomes. `signal_outcomes` aligns supervised labels to the exact immutable opening feature ID. The production profile uses only these aligned labels, including their real label-availability time in purged training. Legacy event-anchored training is available only when MODEL_SIGNAL_LABELS_ONLY=0 for compatibility.

`neural_bandit_state` is a small 12-input, 16-hidden-unit shared network with seven reward heads. Delayed, once-only net-R rewards update the shared representation; reward gradients are bounded. `neural_bandit_updates` audits version, feature and reward. `contextual_bandit` separately retains contextual sample counts, mean/decaying reward and Wilson uncertainty. Revisions of the same event/asset/horizon/direction cannot multiply rewards. This is full-information contextual-bandit learning from paper outcomes, not a validated deep reinforcement-learning trading policy. Forecasts are expected reward, not calibrated win probabilities. All new policy output remains SHADOW pending independent forward validation; no daily improvement is promised.

## Resource controls

SQLite contexts now close on exit and cap page caches. Training uses up to 2,000 recent resolved rows, two new models per stage and a six-hour per-expert cooldown. Linear epochs default to 80; optional temporal training is explicitly off in the small-server profile. The light neural reward learner remains active. Artifacts use an eight-entry cache. Classification calibration metrics are measured on held-out OOS predictions. Collector concurrency and crypto hot streams are bounded; production streams Binance/Bybit/OKX while preserving multi-asset news/structured/public-chart coverage. This reduces hot market coverage and can increase collection latency; it is an explicit capacity tradeoff.

The systemd cgroup is capped at 150% CPU (1.5 cores), 3 GiB memory with a 2.5 GiB soft threshold, and 512 MiB swap. These protect other services but are not proof the workload will never hit a limit. Monitor backlog, freshness, retries and RSS after deployment.

## Delivery

Qualified signals enqueue a compact Telegram summary and an independent full HTML attachment atomically. The attachment preserves every reason, news item and source URL; HTML is escaped. Message acceptance is confirmed by Telegram. As with the previous outbox, a network failure after Telegram accepts a message can cause a duplicate on retry; the API does not provide exactly-once delivery.

Fresh deployment preserves `/etc/v57-fundamental.env` credentials, installs `config/resource-profile.env` separately and restarts the normal 24-hour observation plus 24-hour shadow clock. Qualification can take longer than 48 hours. Old data is not imported. Gold/oil public chart symbols are futures proxies (GC=F, CL=F, BZ=F), not interchangeable spot/broker executions. Unsupported or stale data must not be treated as precise tradable quotes.

## Sources checked

Python SQLite context-manager lifetime: https://docs.python.org/3.13/library/sqlite3.html
Binance public data API timestamp conventions: https://developers.binance.com/en/docs/products/spot/rest-api

See the separate validation record for actual test results and live service observations.

## Target-host validation (2026-09-16)

The final Debian host preflight passed as the dedicated `v57fund` account. Systemd reached `active/running`; all 29 configured services published ready heartbeats and the restart counter remained zero during acceptance. Telegram credentials/API startup validation passed without sending a synthetic trade alert. Binance, Bybit and OKX persisted live raw and semantic market records. Fresh intelligence tables include the seven-barrier outcomes, evaluation candles, exact signal outcomes and neural-bandit audit state.

The final packaged source tree passed the complete native Debian suite: 216 passed and 1 skipped in 75.76 seconds. The local Windows suite passed 210 tests with 2 platform skips.

The clean database produced 99 observation features/signals during initial validation and zero Telegram trade alerts, as required by the reset 24-hour Observation gate. Memory rose from 1.2 GiB during startup to about 1.8 GiB under live ingestion, below the 2.5 GiB soft threshold and 3 GiB hard cap; the cgroup is also limited to 150% CPU, 512 MiB swap and 192 tasks. This short acceptance window proves startup/resource enforcement and live ingestion, not future profitability or long-term accuracy.
