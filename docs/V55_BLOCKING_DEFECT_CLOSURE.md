# V55 Blocking Defect Closure

| Area | V54 residual behavior | V55 behavior | Principal source | Behavioral evidence | Status |
|---|---|---|---|---|---|
| Active exit | Fixed thresholds owned the action | Exit Champion owns action; emergency fallback is explicit | `active_signal_monitor.py` | no-news service-loop test | IMPLEMENTED_AND_RUNTIME_VERIFIED |
| Exit features | Current path was mislabeled as predicted MFE/MAE | Original decision-time forecasts and current path are separate | `active_signal_monitor.py` | leakage + Champion evidence tests | IMPLEMENTED_AND_RUNTIME_VERIFIED |
| Post-exit | Long-only return sign | Long/short direction-normalized return/gain/loss | `active_signal_monitor.py` | short post-exit test | IMPLEMENTED_AND_RUNTIME_VERIFIED |
| Exit learning | Post-exit rows stopped at `LEARNED` | Post-exit snapshot/outcome rows build a versioned Exit Shadow dataset | `learning.py` | 28-row Exit dataset test | IMPLEMENTED_AND_RUNTIME_VERIFIED |
| Partial exit | Could remain `PARTIAL_EXIT` | Position fraction/count persist; remainder returns to `ACTIVE` | `store.py`, `signal_state.py`, monitor | partial lifecycle test | IMPLEMENTED_AND_RUNTIME_VERIFIED |
| Temporal | Shadows trained but did not run | Runtime invokes Shadows, stores predictions, resolves outcomes and governs promotion/rejection | `inference.py`, `learning.py` | temporal runtime lifecycle test | IMPLEMENTED_AND_RUNTIME_VERIFIED |
| Regime | Transition model only | One-vs-rest current-regime Shadow/Champion lifecycle and runtime probabilities | `regime_governance.py`, market response | regime lifecycle test | IMPLEMENTED_AND_RUNTIME_VERIFIED |
| Drift | Research supplied constant source/regime arrays | Collector builds two real persisted runtime windows; actions are scoped | `drift.py`, `research.py` | drift collector/action test | IMPLEMENTED_AND_FIXTURE_VERIFIED |
| Attribution/RCA | Several caller facts were hard-coded true/false | Evidence is queried from events, routing, providers, freshness, models, regimes and snapshots | `attribution.py`, `research.py` | persisted-evidence test | IMPLEMENTED_AND_RUNTIME_VERIFIED |
| Global equities | First ticker identity had no provider map/collision handling | Venue-aware collision IDs and a multi-exchange provider symbol registry | `observation.py`, `reference_market.py` | LLM-off same-ticker test | IMPLEMENTED_AND_FIXTURE_VERIFIED |
| Public directory | Candidate payload only | No-key public issuer directory adapter feeds deterministic validation | `observation.py` | injected directory behavior; live call pending | LIVE_EXTERNAL_VALIDATION_PENDING |
| DEX | No contract market provider | Chain+contract → pair → persisted DEX quote → response provider chain | `reference_market.py`, `market_response.py` | contract pair/quote test | IMPLEMENTED_AND_FIXTURE_VERIFIED |
| Hypotheses | Single split pairs; triples only listed | Purged expanding OOS folds evaluate pairs and actual triples; one BH family | `hypothesis_engine.py` | triple multi-fold test | IMPLEMENTED_AND_RUNTIME_VERIFIED |
| Asset specialization | Profiles were present but not broad training inputs | Profile feature family enters every specialist and all decision targets/meta | `features.py`, `learning.py`, `inference.py` | feature/model matrix tests | IMPLEMENTED_AND_FIXTURE_VERIFIED |
| Daily learning | Entry/exit model state only | Realized precision, adverse excursion, exit quality, continuation and avoided loss | `research.py` | daily report E2E | IMPLEMENTED_AND_RUNTIME_VERIFIED |
| Source learning | Reliability/latency only in report | Corrections, duplicates, latency and outcome reliability are persisted and weighted | `store.py`, `features.py`, `research.py` | source/profile tests | IMPLEMENTED_AND_FIXTURE_VERIFIED |
| Replay | Replay could still write decisions/watchlist/alerts | Replay cannot write production predictions, signals, watchlist, or Telegram | `service.py`, `replay.py` | replay isolation test | IMPLEMENTED_AND_RUNTIME_VERIFIED |
| Operations | V54 names and no active signal view | V55 scripts, systemd unit, active-signals diagnostic, full install path | `scripts/`, `systemd/`, diagnostics | CLI/schema/shell checks | IMPLEMENTED_AND_FIXTURE_VERIFIED |
| Native Linux | Host has no native Linux runtime | Scripts supplied; native Debian/systemd execution remains external | operations files | host capability probe | LIVE_EXTERNAL_VALIDATION_PENDING |

