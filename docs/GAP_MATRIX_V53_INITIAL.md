# V53 Source-First Gap Matrix

Baseline: verified V52 ZIP SHA-256 `207b0791c8f47296f74b89dd08a1332d64d8ffeb6c7daf446fe568d934473128`; its release verifier passed 104 tests. This audit treats that report only as historical context and traces current source/runtime consumers.

| Capability | Source reality at V53 start | Disposition |
|---|---|---|
| Canonical observation contract | timestamps/assets/value exist; provider/market/family are not first-class | EXTEND |
| Semantic routing | structured rows are converted to generic story taxonomy; provider/lane/unit-aware multi-expert routes do not exist | CONFIRMED GAP |
| Dynamic asset/entity resolution | controlled ticker/contract discovery exists but metadata/aliases and PLTR/SUI ambiguity behavior are incomplete | CONFIRMED GAP |
| Non-crypto runtime reference data | abstraction and persisted fixture store exist; no automatic public runtime quote/OHLC provider or active watchlist | CONFIRMED GAP |
| Multi-asset market response | generic reader works once prices exist; automatic Gold/Oil/FX/Equity/Index acquisition is absent | CONFIRMED GAP |
| Specialist artifacts | 20 specialists are trainable and runtime-loaded; Listing specialist is absent | EXTEND |
| Optional boosting models | dependencies are declared but XGBoost/LightGBM/CatBoost adapters are not candidates | CONFIRMED GAP |
| Optional temporal models | no GRU/LSTM/TCN/Temporal Transformer adapter/fallback interface | CONFIRMED GAP |
| Regime | trainable directional `regime` expert plus statistical bootstrap context exist; probabilistic regime-state artifact/output is absent | CONFIRMED GAP |
| Meta context | expert probabilities and limited quality/context feed Meta; expert uncertainty/performance, asset profile, analogue and regime context are incomplete | CONFIRMED GAP |
| Magnitude/time-to-peak | outcome columns exist; no independent trainable runtime models | CONFIRMED GAP |
| Horizon selection | predictions are emitted for every taxonomy horizon; no learned horizon selector | CONFIRMED GAP |
| Risk | formula exists; no independent trainable risk artifact | CONFIRMED GAP |
| Entry/Exit | trainable policies exist; active adverse-evidence re-evaluation agent is absent | CONFIRMED GAP |
| Qualification | runtime/sample/Brier/ECE/drift checks exist; real Meta and minimum specialist Champions are not mandatory | CRITICAL GAP |
| Signal consolidation/ranking | every asset/horizon creates a signal; no event/asset consolidation or Telegram spam guard | CONFIRMED GAP |
| Pump/Dump runtime producers | render classes exist and directional experts exist; dedicated runtime producers are missing | CONFIRMED GAP |
| Ten Telegram producers | formatters exist; several classes have no originating runtime path | CONFIRMED GAP |
| Asset specialization | profiles are learned but not included in feature/meta inference, so profile changes cannot alter prediction | CRITICAL GAP |
| Hypothesis interactions | simple event/asset effect hypotheses exist; feature-combination experiment and failed-memory gates are incomplete | CONFIRMED GAP |
| Backtest realism | leakage-safe metrics exist; fee/spread/slippage/delay/liquidity costs are absent | CONFIRMED GAP |
| Diagnostics | core status commands exist; asset/feature/shadow status commands are absent | CONFIRMED GAP |
| New behavior proofs | V52 proves artifact/Meta/structured/warmup/shadow; V53-required routing, PLTR/SUI, magnitude/horizon, exit reaction, consolidation and qualification proofs are absent | CONFIRMED GAP |

All acquisition, durability, supervisor, queue, existing artifact, replay and V52 regression capabilities are PRESERVE items.
