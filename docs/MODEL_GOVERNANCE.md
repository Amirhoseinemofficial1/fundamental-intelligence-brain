# Model Governance

Artifact states are `SHADOW`, `CHAMPION`, `RETIRED`, and `REJECTED`. A new candidate never replaces a Champion from its training holdout alone. It receives the same post-training live feature stream, records predictions, waits for real outcomes and must pass minimum sample, Brier, calibration and improvement gates.

Promotion is transactional; the prior Champion becomes recoverable `RETIRED`. Drift or an operator can roll back to the most recent eligible artifact with an audit record. Dataset and artifact hashes prevent re-training an identical rejected dataset. Improvement records report metric, before/after values, sample count and validation window—never an unsupported “percent smarter” claim.
