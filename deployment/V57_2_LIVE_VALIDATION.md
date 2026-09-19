# V57.2 live deployment validation

Validation date: 2026-09-19

## Immutable release

- Artifact: `V57_2_BOUNDED_SEMANTIC_ADAPTIVE_FINAL.rar`
- SHA-256: `bb400d5bbbbb56cc2c1bdc50fdb2abe007fa1fb88209918baf90d97e11ef7bf4`
- Manifest payload files: 279
- Manifest hash: `7c0674aac85170029d4d7f533d1c795ee77a27ccfad1917398a3a65a9c95d5a5`
- Local full suite: 225 passed, 3 skipped in 86.15 seconds
- Exact extracted RAR on Debian: 227 passed, 1 skipped in 26.96 seconds

## Fresh deployment

The exact data and application paths were checked before deletion. The final
deployment stopped `v57-fundamental.service`, deleted the existing contents of
`/var/lib/v57-fundamental` and `/opt/v57-fundamental`, installed the verified
staging tree, ran preflight, and started the systemd service. The fresh service
entered active/running state at 2026-09-19 02:54:52 CEST.

Observed post-start evidence:

- All 29 configured child services reported ready.
- `NRestarts=0`; no `database is locked`, traceback, fatal startup, or child
  exit entries were found from the final start.
- Memory current/peak sample: 1,471,528,960 / 1,472,663,552 bytes.
- Resource limits: CPU 150%, `MemoryHigh=2,621,440,000`,
  `MemoryMax=3,221,225,472`, swap 512 MiB, tasks 192.
- Root filesystem had about 41 GiB free. Manual disk-guard execution returned
  `Result=success`, exit status 0; its five-minute timer is active and enabled.
- Active application manifest verification passed with 279 files and the hash
  above. The server RAR hash matched the local artifact.
- Raw websocket journal file count was zero.

## Bounded-data evidence

The server profile sets a 20,000-row cap for pending plus leased raw news. Under
the real initial concurrent backfill, the final sample contained 19,894 pending
and 79 leased rows, totaling 19,973. There were 1,436 completed rows; their
transport payload averaged and maxed at 35 bytes after processing. Duplicate
raw keys were zero.

Structured data had 9,951 rows with zero duplicate provider/type/entity/bucket
keys. Binance, Bybit, and OKX semantic market databases also had zero duplicate
venue/symbol/type/time buckets. The production profile retains semantic market
data for three days, operational queues and control-plane story copies for two
days, reference prices for seven days, and runtime artifacts for three days.
The hard disk guard stops ingestion before V57 exceeds 12 GiB or root free
space falls below 15 GiB.

## Semantic learning and signal gate

The final live sample persisted 39 semantic memories using
`local-signed-hash-64-v1` at 64 dimensions. Intelligence metrics recorded 31
completed local embeddings in an earlier status sample and one OpenAI semantic
fallback. The external OpenAI Responses smoke request was rate-limited with
HTTP 429, so successful external semantic inference is not claimed. Local
multilingual lexical embeddings kept semantic feature learning operational.

The fresh database reported schema version 9. The warm-up was progressing in
`OBSERVATION`, `signals_enabled=0`, with shadow time still zero. This validation
does not claim completion of the 48-running-hour gate: the service requires 24
actual running hours of observation, then 24 actual running hours of shadow,
and still must pass sample, calibration, drift, and champion qualification
before user-facing signals open. No profitability claim is made.
