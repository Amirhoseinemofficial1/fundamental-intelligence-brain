# V50 Audit Remediation Map

This document maps the major V50 forensic findings to V52 design changes.

| V50 finding | V52 remediation |
|---|---|
| News-first quality gate failed while process lifecycle passed | Unified Quality Gate separates operational success from data/freshness/resource success. |
| Zero material evidence of strong sub-15s freshness | Critical 15s and 60s material-news ratios in full diagnostic. |
| RawNewsSpool `_seen` before queue insert could permanently drop overflowed items | Durable SQLite insert with DB uniqueness; no pre-persistence in-memory seen flag. |
| Raw news write failure had no durable retry contract | Storage errors propagate; processing uses durable leases/retries. |
| Raw-news shutdown could cancel queued work | Producer-first shutdown plus durable committed rows; downstream drain phases. |
| Enrichment QueueFull/drop and silent exceptions | Durable enrichment job table with lease/retry/dead observability. |
| Translation backlog grew unsustainably across per-process workers | Single central translation service, durable queue, shared cache/state, bounded global concurrency. |
| Discovery consumed hot-path CPU/network budget | Separate Discovery process/cadence/failure domain. |
| DNS/429/5xx/error classification mixed into generic failures | Explicit HTTP classifications, Retry-After, backoff, circuit state. |
| Preflight checked only a subset of requirements | Imports every runtime dependency declared by V52 and `pip check` in install/release workflow. |
| Duplicate/overridden versioned env knobs | One canonical V52 env template and exact schema parity gate. |
| Dozens of unreachable legacy supervisors/runtime files | No legacy runtime files in V52 package. |
| Runtime still advertised V40/V47 schemas | Canonical `51.0` application/status/run schemas. |
| Translation cache opened SQLite in fragile/silent ways | Durable central control-store path; no per-process hidden compatibility cache. |
| Fake Telegram queue gauge always returned zero | Quality reads durable queue counts from SQLite. |
| Alert claim lacked atomic lease | Atomic pending→leased control-store claim. |
| Unbounded in-memory story-id set | Canonical story uniqueness is DB-backed. |
| Archive DB/file divergence risk | Durable database is authoritative control plane; raw market journal has explicit sync contract. |
| Raw-news durability was not a quality invariant | Per-run raw insert accounting is a critical Quality Gate check. |
| Diagnostic shell could exit 0 even when quality failed | Script combines Supervisor + Quality + Bundle exit codes and fails closed. |
| Supervisor success meant only processes survived | Supervisor lifecycle and data-quality gates are separate and both required. |
| Shipped test suite contained stale version conditionals | V52 capability/contract tests only; no `startswith(V49)` logic. |
| Possible systemd root service | Installer creates dedicated `v52fund` user; unit explicitly sets User/Group. |
| Weak filesystem/systemd isolation | Hardened unit with read-only system, explicit write path, no capabilities. |
| Scheduler/nice failures could be silent | V52 does not depend on hidden nice manipulation for correctness. |
| Broad-news count could inflate product-quality metric | Full diagnostic has separate unique and material unique counts. |
| Market durability was a V50 strength | Preserved as a strict V52 contract and expanded with persisted sequence state. |
