# V52 Reliability and Failure Semantics

## No silent critical failures

Critical storage/network-worker paths either:

1. succeed and update durable/observable state,
2. schedule retry/backoff with error state, or
3. raise a fatal error that is visible to the service/Supervisor.

The release tests explicitly reject `TODO`/`FIXME` placeholders in runtime source and cover writer-failure propagation.

## News durability

Raw observations are inserted with a database uniqueness constraint. A duplicate insert is harmless; a queue-full condition does not mark an item seen before durable persistence because the architecture does not use V50's in-memory `_seen` gate.

Raw processing states use leases. A worker death before ACK leaves the row reclaimable after lease expiry.

## Atomic story/control work

Story recording and creation of downstream jobs are performed through the control store transaction boundary. This prevents a story from being committed while its required durable job silently disappears.

## Translation

Translation uses a central durable queue and central process-level concurrency. Failed jobs are retried up to the configured attempt policy; dead jobs are counted and production diagnostics gate new dead jobs.

## Telegram

Alerts are claimed atomically. A worker has a lease; retryable failures return to pending with delay, permanent/attempt-exhausted failures become observable dead rows, and stale alerts expire.

Telegram is inherently not a two-phase transactional system with the local database: a process crash after remote send but before local ACK can theoretically produce an at-least-once duplicate on retry. V52 minimizes that window and treats local state as durable, but it does not falsely claim exactly-once delivery from an external API that provides no transactional commit primitive.

## Market raw-wire durability

Raw messages are written before semantic interpretation. The writer uses a bounded queue, segmented append-only files, periodic byte/time sync, and final sync on graceful shutdown. Writer failure is fatal.

Final service metrics assert:

```text
raw_enqueued == raw_written == raw_synced
raw_queue == 0
semantic_enqueued == semantic_persisted
semantic_queue == 0
```

The Quality Gate treats mismatches as critical failures.

## Sequence/recovery state

Sequence-sensitive streams persist their latest sequence and recovery-pending state in the per-exchange semantic database. Restart therefore does not silently reset the last known stream state.

## Network handling

The HTTP layer provides bounded timeouts, Retry-After support, exponential retry/backoff, host circuit state, response-size limits, and error classification. Provider failure is isolated from unrelated sources.

## Security

- Dedicated systemd user.
- No ambient/capability bounding privileges.
- Read-only system tree with explicit runtime write path.
- `NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`, kernel/control-group protections.
- Secret-aware logging redaction.
- Diagnostic bundle never copies populated `.env`.
- Package install is staged cleanly so stale old release files cannot survive an upgrade.
