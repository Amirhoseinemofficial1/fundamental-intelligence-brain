# Live Validation

Level A means source, schemas, fixtures, artifacts, local E2E, regression and packaging are complete. Level B is environment-specific and must be run on the target VPS with real network routes and authorized credentials.

Recommended progression is 20 minutes, 2 hours, 6 hours, 24 hours and 48+ accumulated runtime. At each gate inspect service heartbeats, queue lag, provider success/latency, raw/semantic persistence, canonical observation volume, feature quality, Shadow sample growth, response resolution, Brier/ECE, drift, alert delivery and resource use.

Run strict preflight and the timed diagnostic:

```bash
PREFLIGHT_STRICT_NETWORK=1 ./bin/preflight_V52.sh
./bin/run_V52_20min_diagnostic.sh
```

Credential-dependent connectors and the optional OpenAI semantic layer remain `IMPLEMENTED_LIVE_VALIDATION_PENDING` until that deployment evidence exists. LLM or provider failure must degrade cleanly and never bypass governance.
