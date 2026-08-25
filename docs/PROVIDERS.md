# Provider Manifest

`provider_manifest.py` combines explicit runtime adapters with the structured provider registry and validates that every enabled manifest entry has a callable adapter path. The fixture gate currently discovers at least 80 provider definitions and reports zero enabled/adaptor mismatches.

Provider health is isolated by process/circuit state. The reference-market chain reads normalized non-crypto `reference_prices` first and crypto semantic stores as a fallback. Missing credentials do not remove implementation: status is `IMPLEMENTED_FIXTURE_VERIFIED` locally and `LIVE_CREDENTIAL_VALIDATION_PENDING` until the target server passes strict preflight and a timed diagnostic.

The packaged reference JSON files are capability catalogs, not proof that every unauthenticated endpoint is live forever. Runtime health and latency are observable.
