# V52 Release Validation

## True Brain Level-A gates

- Full suite in PowerShell: `103 passed, 1 skipped` (the only skip is the Bash-specific deployment test).
- Full suite through Git Bash release verifier: `104 passed`; `V52 release verification PASS`.
- All 20 specialist artifact lifecycles plus Entry/Exit policies: `22 passed`.
- Critical brain/artifact/control/E2E group: `21 passed`.
- Python compilation: PASS.
- Git Bash syntax for `bin/*.sh`: PASS.
- `pip check`: PASS.
- Offline preflight with Telegram disabled and an isolated writable data root: PASS; only documented optional enrichment accelerators were absent with deterministic fallbacks.
- V51/V52 four-round hot-path comparison: PASS; no duplicate loss and no throughput regression.

The authoritative final extracted-ZIP results and file digest are generated during the packaging stages; working-tree results are not substituted for extracted-artifact verification.

## Required gates

A release artifact is accepted only after all of the following pass on a clean extracted copy:

1. `MANIFEST.sha256` verification.
2. Python `compileall` for `src` and `tests`.
3. `bash -n` for every shipped shell script.
4. Full pytest suite.
5. Offline-safe Preflight (dependencies, config schema, reference assets, SQLite WAL, disk/fdatasync).
6. Isolated Supervisor smoke with external producers disabled.
7. Smoke Quality Gate and diagnostic bundle generation.

Target production deployment adds:

8. Strict network Preflight.
9. Full 20-minute diagnostic with real configured external services.

## Why the target-host diagnostic still exists

A release package cannot guarantee the state of third-party DNS, firewalls, upstream APIs, credentials, rate limits, or the target VPS CPU/memory. V52 therefore treats target-host validation as part of production acceptance rather than silently assuming the network is healthy.

## Current package test scope

Contract tests cover:

- package imports;
- canonical config registry;
- control transaction/lease behavior;
- HTTP client retry/classification;
- news durability/reclaim;
- pipeline integration;
- market raw/semantic durability;
- exchange protocol normalization;
- preflight contract;
- quality-gate contract;
- relevance/materiality behavior;
- runtime mapping for every canonical service;
- secret redaction;
- heartbeat watchdog identity/staleness;
- writer failure propagation.
