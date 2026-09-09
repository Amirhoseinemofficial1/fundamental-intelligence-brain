# V52 Configuration

`.env.example` is the canonical operator template. `config/settings.schema.json` contains the exact same key set; Preflight fails when those sets diverge or when a duplicate key exists.

## Core runtime

- `DATA_DIR`: persistent runtime root.
- `HTTP_USER_AGENT`: set to a real operator/contact string before production internet use.
- `SERVICE_STATUS_SECONDS`: heartbeat write cadence.
- `SERVICE_STALE_SECONDS`: watchdog threshold; must exceed status cadence.
- `SHUTDOWN_GRACE_SECONDS`: maximum dependency-aware shutdown window.
- `SQLITE_SYNCHRONOUS`: production allows `FULL` or `EXTRA` only.

## News

Hot and Coverage use different concurrency and adaptive poll bounds. Do not increase concurrency merely to improve latency; first inspect source health, 429s, useful-yield, CPU PSI, and duplicate rates.

- `NEWS_HOT_*`: Tier-0/priority polling budget.
- `NEWS_COVERAGE_*`: broader, slower source budget.
- `NEWS_RAW_BACKPRESSURE_ROWS`: durable backlog pressure guard.
- `NEWS_MATERIALITY_THRESHOLD`: threshold used for material-story/alert decisions.
- `NEWS_ALERT_MAX_AGE_SECONDS`: maximum publication age for new alerts; it is not overridden by a hidden bootstrap minimum.

## Translation

`TRANSLATION_ENABLED=1` sends only required translation jobs to the central service. Ingestion never waits for translation completion.

- `LIBRETRANSLATE_URL`
- `TRANSLATION_CONCURRENCY`
- `TRANSLATION_MIN_GAP_SECONDS`
- `TRANSLATION_LEASE_SECONDS`
- `TRANSLATION_MAX_ATTEMPTS`

## Telegram

When `TELEGRAM_ENABLED=1`, both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are mandatory at Preflight.

Transport exceptions and diagnostic logs are secret-redacted. Do not place real secrets in `.env.example` or source files.

## Market

Each `market_*` service is independent. `LIQUIDITY_DEPTH` is the emitted semantic depth. Venue subscription depths can have stricter protocol requirements; for example `BYBIT_ORDERBOOK_DEPTH` accepts only the validated protocol set exposed by Settings validation.

Market endpoint overrides are validated for scheme/hostname before Runtime starts.

## Supervisor

`SUPERVISOR_SERVICES` must contain only canonical V52 service names and cannot contain duplicates.

`SUPERVISOR_RESTART_LIMIT` and `SUPERVISOR_RESTART_WINDOW_SECONDS` bound crash/hang recovery. Repeated failures fail the run instead of looping forever.

## Quality gates

The `DIAGNOSTIC_*` variables are production acceptance thresholds, not cosmetic status metrics. The 20-minute diagnostic exits non-zero on a critical failure.

Tune thresholds only from measured target-host evidence; do not lower them merely to turn a red diagnostic green.

## Dynamic all-crypto universe (V57.1)

`MARKET_CRYPTO_UNIVERSE_ENABLED=1` discovers active crypto markets from the configured public venue directories instead of treating `BTC/ETH/SUI` as the complete universe. `MARKET_CRYPTO_STREAM_LIMIT` controls only the deep WebSocket hot tier (default 200 per venue; `0` requests the complete discovered venue list and can be very expensive). The full discovered universe is still registered for event resolution and on-demand price/outcome collection. `MARKET_CRYPTO_UNIVERSE_REFRESH_SECONDS` controls low-frequency directory refresh.

Cold/long-tail tokens do **not** receive a permanent order-book subscription. When news, on-chain data, a listing, funding/liquidation event, or the broad market anomaly scanner activates a token, `CexUniverseMarketProvider` collects public CEX quote/history for that asset and the normal prediction/outcome/learning loop applies. This is intentional resource isolation, not a coverage whitelist.

`MARKET_CRYPTO_ANOMALY_ENABLED=1` routes qualified market anomalies into the Brain story feed only; raw anomalies never bypass LIVE qualification into Telegram. Use `MARKET_CRYPTO_ANOMALY_CHANGE_PCT`, `MARKET_CRYPTO_ANOMALY_MIN_QUOTE_VOLUME_USD`, and `MARKET_CRYPTO_ANOMALY_MAX_PER_CYCLE` to bound noise.

- `MARKET_CRYPTO_STREAM_ROTATE_SECONDS` defaults to `3600`. Dynamic deep streams are intentionally recycled at this cadence so newly listed/high-liquidity tokens can enter the HOT tier without waiting for an accidental disconnect. Set `0` to disable planned rotation.
