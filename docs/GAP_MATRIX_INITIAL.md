# Source-Verified Initial Gap Matrix

This matrix was produced from the candidate source, not the previous delivery report.

| Gap | Verified source reality | Initial status |
|---|---|---|
| 1. Experts | `experts.py` creates rule-weighted votes; no per-expert dataset/artifact lifecycle | CONFIRMED |
| 2. Model factory | No candidate-model factory or serialized estimator implementation | CONFIRMED |
| 3. Champion inference | `record_decision` attaches champion ID after heuristic inference; artifact is not loaded | CONFIRMED |
| 4. Shadow challenger | Challenger is evaluated and promoted immediately from the training rows; no unseen shadow stream | CONFIRMED |
| 5. Warm-up | Uses `time.time() - deployment_started_at`; downtime incorrectly advances warm-up | CONFIRMED |
| 6. Multi-asset data | Taxonomy lists non-crypto assets, while response reader only scans crypto market semantic databases | CONFIRMED |
| 7. Structured bridge | Structured snapshots persist separately; Intelligence consumes only story change feed | CONFIRMED |
| 8. Feature experts | On-chain/derivatives/microstructure experts are names operating on generic event priors | CONFIRMED |
| 9. Regime | Regime is deterministic threshold classification, with no artifact lifecycle | CONFIRMED |
| 10. Entry/exit | Entry/exit are fixed text/rules; outcome-driven trainable artifacts absent | CONFIRMED |
| 11. Attribution/hypotheses | Missed moves are counters/audit rows; structured attribution and hypothesis validation absent | CONFIRMED |
| 12. Daily review | Daily report is aggregate counters plus drift/qualification | CONFIRMED |

Additional verified gaps: no canonical observation schema, no strict available-at-time feature store, hard-coded asset universe, no dataset/artifact registry, no true shadow predictions/outcomes, incomplete signal lifecycle states, no replay/backtest/importer, no ten-class Telegram renderer/test matrix, and no provider-manifest-to-adapter contract.

The existing durable acquisition plane and its regression tests are preserved as the non-blocking foundation.
