# Actual-Runtime Warm-up

Warm-up uses persistent `observation_runtime_seconds` and `shadow_runtime_seconds`, not deployment wall clock. `last_runtime_tick` contributes only a bounded interval while the service is actually ticking; long downtime is capped and replay contributes zero.

- First 24 accumulated observation hours: observe/train only.
- Next 24 accumulated Shadow hours: artifact competition and qualification, no unqualified signals.
- After both counters reach 24 hours: signals remain gated by event/response samples, Brier, calibration and drift checks.

The restart test simulates a three-day outage and proves it adds only one bounded tick, while replay adds nothing.
