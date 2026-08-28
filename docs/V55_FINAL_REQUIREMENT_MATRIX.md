# V55 Final Requirement Matrix

The release gate distinguishes code/fixture validation from claims that require
credentials, market time, 48 actual runtime hours, or a native Debian host.

| Level | Requirement family | Status | Evidence |
|---|---|---|---|
| A | Immutable V54 baseline and new-source separation | IMPLEMENTED_AND_RUNTIME_VERIFIED | SHA gate and distinct roots |
| A | Leakage-safe features, labels, walk-forward and replay isolation | IMPLEMENTED_AND_RUNTIME_VERIFIED | V53/V54/V55 behavior suites |
| A | Model-driven active exit, partial exit, exit completion, post-exit learning | IMPLEMENTED_AND_RUNTIME_VERIFIED | V55 service-loop and dataset tests |
| A | Temporal and trainable regime governance | IMPLEMENTED_AND_RUNTIME_VERIFIED | real Shadow prediction/outcome/governance tests |
| A | Dynamic equity/token identity, exchange collision and DEX path | IMPLEMENTED_AND_FIXTURE_VERIFIED | unknown/collision/contract tests |
| A | Drift, attribution, missed opportunity, profiles and daily research | IMPLEMENTED_AND_RUNTIME_VERIFIED | persisted-data tests and system E2E |
| A | Hypothesis pair/triple walk-forward and full-family BH FDR | IMPLEMENTED_AND_RUNTIME_VERIFIED | interaction regression tests |
| A | Ten Telegram business producers and signal consolidation | IMPLEMENTED_AND_RUNTIME_VERIFIED | durable queue/renderer tests |
| A | Supervisor failure isolation, clean process exit and diagnostics | IMPLEMENTED_AND_RUNTIME_VERIFIED | full suite, CLI, orphan audit |
| A | Clean manifest, archive/re-extraction pipeline and checksum inequality gate | IMPLEMENTED_AND_RUNTIME_VERIFIED | 223-file manifest; candidate archive SHA differs from V54; separate extraction manifest/compile/13-test battery PASS; immutable final seal is verified externally after creation |
| B | Live credentialed providers and public directory | LIVE_EXTERNAL_VALIDATION_PENDING | requires external availability/credentials |
| B | 48-hour accumulated warmup and live qualification | LIVE_EXTERNAL_VALIDATION_PENDING | cannot be compressed or simulated |
| B | Native Debian/systemd validation | LIVE_EXTERNAL_VALIDATION_PENDING | no native Linux runtime on build host |
| B | 20-minute live network diagnostic | LIVE_EXTERNAL_VALIDATION_PENDING | requires deployment network and credentials |

`LEVEL_A_FAILED = 0`. The immutable final ZIP's SHA and final re-extraction report
are produced after sealing and delivered alongside it. Pending Level-B items do
not enable user-facing signals; the live gate remains fail-closed.
