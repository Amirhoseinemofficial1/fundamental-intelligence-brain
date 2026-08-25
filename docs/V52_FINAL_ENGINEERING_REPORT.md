# V52 Final Engineering Report

## True Brain completion addendum — 2026-08-25

The previous report is retained as historical acquisition-engineering context, not as proof of the new brain. The current source now adds a canonical structured-observation bridge, dynamic six-family universe, reference-market provider chain, point-in-time feature store, real serialized Model Factory, 20 trainable specialist lifecycles, controlling Meta Champion, genuine unseen-runtime Shadow competition, persistent accumulated-runtime warm-up, trainable Entry/Exit policies, full signal/post-exit learning, failure and missed-opportunity attribution, hypothesis/knowledge gates, replay/walk-forward/backtest, source-conflict/reliability and deep daily review.

Independent local evidence after integration: PowerShell produced 103 passed and one Bash-only skip, while the Git Bash release verifier executed all 104 tests and reported `V52 release verification PASS`; compileall, shell syntax, pip dependency consistency and offline preflight passed. The four-round 10,000-item persistence comparison measured median V52 throughput 25,842 events/s versus V51 18,288 events/s (no duplicate loss, no regression). These are measured local engineering metrics, not profit claims. Credential-dependent endpoints and the optional OpenAI semantic request still require target-VPS Level-B validation.

## 1 — V51 forensic verdict

V51 had a strong acquisition core but was not a complete intelligence product. The supplied real-VPS baseline produced 1,112,176 market raw events in 20 minutes and ran 21/31 configured services, while fresh news was only 6/1,136 at <=15 seconds and 8/1,136 at <=60 seconds. Duplicate rates were about 98.6%/96.4%, enrichment backlog grew by 82, storage projected to roughly 43 GiB/day, RSS was 1.65–1.70 GiB, FD use 2,100–2,450, and CPU PSI p95 was 39.3. Local V51 verification on Windows/Python 3.10 was 37 passed/3 failed; failures exposed Bash-only and `fdatasync`/`geteuid` portability assumptions.

Runtime tracing confirmed durable raw-news SQLite ingestion, downstream processing/control queues, per-venue raw market logs plus semantic SQLite, supervisor-isolated services, and durable Telegram leasing. It also confirmed defects: a quality-gate service-key bug, one-shot/truncated HTTP reads, a silent 100-item feed cap, unsafe parser root handling, substring keyword false positives, Telegram success without API-level confirmation, missing global 429 cooldown, incomplete topology preflight, shell-evaluated `.env`, missing premium service configuration, and no real persistent learning/lifecycle plane.

## 2 — V51 strengths preserved

The collector hot path, source registries, 78-locale Google mesh, 365 parser profiles, official/social/structured/provider collectors, raw-before-semantic market persistence, per-exchange failure isolation, durable queues, adaptive polling, process watchdog, dependency-aware shutdown, diagnostics and quality artifacts remain. V52 adds intelligence as separate consumers of the durable story/change ledger; OpenAI, research, response measurement and maintenance cannot block accepted raw news.

## 3 — V51 defects fixed

V52 reads HTTP bodies to EOF with size/content-length checks and retry classification; removes the 100-item cap; validates parser roots; uses word-boundary relevance; stores conditional-request ETag/Last-Modified; confirms Telegram `ok=true` plus `message_id`; validates bot/chat at startup; honors global Retry-After cooldown; fixes the quality service lookup; provides platform-safe durability and privilege probes; validates the 35-service topology; loads `.env` literally without evaluation; restores `premium_actions`; suppresses raw-news and raw-market Telegram; and adds bounded compression/retention.

## 4 — V52 architecture

V52 has three failure-isolated planes. The real-time data plane persists news and market wire data first. The intelligence/research plane consumes story revisions, builds canonical events and asset relations, records predictions, observes later market response and performs controlled offline learning. The control plane supervises 35 processes, queues confirmed Telegram delivery, enforces warm-up/qualification, monitors resources, and maintains storage. The intelligence database is physically separate at `data/brain/intelligence.sqlite3`.

## 5 — intelligence system

Deterministic taxonomy covers macro, central banks, geopolitics, energy, equities, regulation, ETFs, crypto/on-chain/derivatives, pump and dump conditions. Canonical coverage includes BTC, ETH, SOL, gold, WTI, Brent, DXY, EURUSD, USDJPY, SPX and NDX. Story revisions are classified as INITIAL, UPDATE, CORRECTION, CONFIRMATION or FOLLOW_UP and clustered across sources. A validated OpenAI Responses API proposal may enrich only high-materiality stories; it uses JSON Schema, `store:false`, canonical allowlists and deterministic fallback, and cannot override signal governance.

Twenty specialists vote across asset and horizon. The meta decision combines prior direction, empirical historical response, source/novelty/event importance, statistical regime and expert agreement. Sparse evidence shrinks confidence and yields NO_TRADE. Every decision includes probability, calibrated confidence, uncertainty, expected magnitude/value, risk, entry, invalidation, target and exit logic.

## 6 — learning system

The market-response worker aligns semantic prices strictly after event observation and records return, volatility, volume, MFE, MAE, direction, reversal, continuation and time-to-peak by asset/horizon. Research builds evidence-level knowledge and asset profiles, performs post-exit, false-alert and missed-opportunity review, calibrates probabilities with beta-binned calibration, detects drift, and generates a persistent daily report. Purged walk-forward validation uses chronological folds and embargo to prevent future leakage.

## 7 — 48-hour warm-up

`deployment_started_at` is persistent. Age <24h is OBSERVATION, 24–48h is SHADOW, and no user-facing trading signal can be enqueued in either mode. After 48h, the system remains CONSERVATIVE until every qualification check passes. Restart does not reset or bypass the blackout.

## 8 — signal qualification

Default gates require >=48h, >=200 events, >=100 measured responses, Brier <=0.24, ECE <=0.15 and no drift event in the last day. Individual decisions additionally require historical samples, bounded uncertainty/risk and sufficient expert agreement. Failure of any global gate sets `signals_enabled=0`.

## 9 — Telegram

Only qualified actionable intelligence, signal exit/invalidation, idempotent daily learning reports and critical operational messages are eligible. One decision is sent per request; text over 4,000 characters fails closed. Delivery is counted only after Telegram returns `ok=true` and a `message_id`. Authentication/chat validation runs before leasing messages, and 429 Retry-After creates a global cooldown. Raw news and raw market events are archived, never alerted.

## 10 — database / memory

SQLite WAL stores raw news, control queues, structured snapshots, per-market semantic data and a separate versioned brain. The brain includes events/revisions/entities/assets/relations, source profiles, regimes, votes, predictions, signals/lifecycle, response targets/outcomes, models, experiments, knowledge, profiles, drift, daily reports and immutable audit records. Natural keys make replay and restart idempotent.

## 11 — model governance

The bootstrap champion is explicit. Challengers are reproducible artifacts tied to data/feature/code versions and a recorded experiment. Promotion is transactional and requires better out-of-sample Brier score, acceptable ECE and stable folds; otherwise the challenger is rejected. `v52 model-rollback [--version ID]` atomically retires the current champion, restores an eligible retired version and records `MODEL_ROLLBACK` in the audit log.

## 12 — test results

Local commands: `python -m compileall -q src tests scripts` passed; `python -m pytest -q` passed with 59 passed/1 skipped in 17.97 seconds. The skip is Bash contract execution because Bash/WSL is absent on this Windows host; the scripts remain syntax-checked by the Linux release verifier. A real seven-process supervisor smoke reached readiness, ran, and completed dependency-aware shutdown; its first run exposed and led to fixes for `market_response` routing, non-Linux watchdog behavior and atomic status-file collision. Coverage includes durability, idempotency/restart, 48h blackout, event mapping, regime/experts, no-signal, response alignment, leakage, calibration, champion experiment, atomic rollback, Telegram confirmation/formats, HTTP 429/timeout, watchdog/recovery, burst persistence and slow-LLM isolation.

## 13 — performance comparison

The alternating four-round 10,000-item benchmark inserted every item. Median V51 throughput was 16,071.7 events/s and V52 was 15,173.4 events/s: 5.59% lower, within the fail-closed 15% release limit. No duplicate loss occurred. The user-supplied 20-minute VPS baseline remains the production reference; this Windows microbenchmark validates that the new intelligence plane did not materially enter the acquisition path.

## 14 — known limitations

This host has Python 3.10 and no Linux/Bash/systemd runtime, so target-server systemd and 20-minute live-provider qualification must run on the Debian/Ubuntu VPS. Third-party endpoints and credential-only sources cannot be certified without their credentials. The configured OpenAI smoke reached the API but received HTTP 429, so deterministic semantics remains active until account quota/rate limits permit requests. Statistical learning cannot be claimed production-calibrated before real 48h+ outcomes satisfy the gates; the system correctly suppresses signals until then. It is a controlled probabilistic expert, not AGI and not a profit guarantee.

## 15 — run instructions

On Debian/Ubuntu: copy `.env.example` to `.env`, set a real `HTTP_USER_AGENT`, configure provider credentials, Telegram credentials if enabled, and `OPENAI_API_KEY` if OpenAI enrichment is enabled. Run `python3 -m venv .venv`, `.venv/bin/pip install -r requirements.txt`, `.venv/bin/pip check`, then `PREFLIGHT_STRICT_NETWORK=1 ./bin/preflight_V52.sh`. Start interactively with `./bin/run_V52.sh`, smoke with `./bin/run_V52_smoke.sh`, or install systemd with `sudo ./bin/install_systemd.sh`. Stop systemd with `./bin/stop_V52.sh`. Inspect with `.venv/bin/python -m v52.cli brain summary`, `brain qualification`, `brain models`, or `brain reports`.

## 16 — final clean package

The release directory contains source, tests, references, configuration schema, scripts, systemd unit, operations/architecture/reliability documentation, manifest and no populated `.env`, database, log, cache or API secret. Verify the extracted package with `./bin/verify_release.sh`; execute strict network preflight and the 20-minute diagnostic on the target server before enabling production delivery.
