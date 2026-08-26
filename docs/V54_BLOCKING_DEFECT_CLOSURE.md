# V54 Blocking Defect Closure

Evidence suite: `tests/test_v54_zero_gap_blockers.py`. All entries below are Level-A and use production behavior, not name/existence assertions.

| # | Defect | Old behavior | New behavior | Main source files | Proof | Status |
|---:|---|---|---|---|---|---|
| 1 | Entry/exit future leakage | Realized MFE/MAE were model inputs | Prediction-time forecast/context only; availability contract enforced; realized values remain labels | `learning.py`, `model_factory.py` | leakage/availability test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 2 | Continuous active monitor | Exit ran only during story processing | Independent supervisor service consumes market/fundamental state without news | `active_signal_monitor.py`, `runtime/service.py`, `constants.py` | no-news deterioration test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 3 | Stuck EXIT_PENDING | No completion consumer | Confirmation and timeout recovery consumer completes EXITED | `active_signal_monitor.py`, `signal_state.py` | no-news lifecycle test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 4 | Post-exit tracking | Horizon outcome was reused as post-exit | Scheduled price windows persist return/gain/loss/peak/trough and feed lifecycle learning | `active_signal_monitor.py`, `store.py` | post-exit persistence test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 5 | Dynamic equities | Static examples dominated resolution | Structured company/ticker/exchange/sector/industry registration | `observation.py`, `store.py` | randomized never-seen ticker | IMPLEMENTED_FIXTURE_VERIFIED |
| 6 | Dynamic tokens | Static token seeds | Structured token/chain/contract/pairs/aliases registration | `observation.py` | randomized never-seen token | IMPLEMENTED_FIXTURE_VERIFIED |
| 7 | Token primary identity | Symbol was primary key | Canonical `chain:contract`; same symbol never overwrites another chain | `observation.py` | Ethereum/Solana/Base collision test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 8 | Chain false asset | Chain metadata could infer ETH/SOL | Identity metadata excluded from affected-asset text inference | `observation.py` | chain-only false-positive test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 9 | Dynamic LLM candidates | Static asset enum rejected unknowns | Open schema emits candidates; deterministic resolver validates registration | `openai_semantics.py`, `observation.py` | dynamic resolver/schema regression | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 10 | Non-crypto latest quote | Head slicing could return an old quote | Timestamps sort before tail selection; quote is max timestamp | `reference_market.py` | 150-timestamp test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 11 | Zero versus missing | `0.0` was treated as absent | Explicit missing/valid flags; zero remains a valid latest value | `features.py` | new zero/old nonzero test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 12 | Feature freshness | One implicit 30-day window | Feature-specific TTL plus value/observed/available/expires/source/valid metadata | `features.py`, `store.py` | stale OI/funding/orderbook tests | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 13 | Full requirements | Core install looked complete | `requirements-full.txt` and `.[full]`; README production path uses full | requirements, `pyproject.toml`, README | release identity/install test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 14 | Temporal governance | Standalone adapters only | GRU/LSTM/TCN/Transformer traverse dataset, OOS Candidate, Challenger, Shadow, promotion and runtime load; insufficient data is NOT_QUALIFIED | `temporal_governance.py`, `temporal_models.py`, `inference.py` | qualified + not-qualified governance tests | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 15 | Model selection split | One 70/30 ranking split | Multiple expanding purged folds with embargo and aggregated OOS metrics | `model_factory.py`, `learning.py` | OOS fold-ranking test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 16 | Pump/dump labels | Generic positive-return label | Separate upside and downside thresholds with specialist feature families | `learning.py` | pump/dump target test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 17 | Regime transition label | High volatility proxy | Actual current/future regime inequality label | `learning.py` | unchanged/changed regime test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 18 | Regime ensemble | Partial ensemble | Trend, volatility, change point, clustering, correlation, breadth, liquidity, risk-on/off probabilities and uncertainty | `experts.py` | component/output test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 19 | Asset profiles | Two shallow aggregates | Betas, sensitivities, horizons, experts, fakeout/reversal, MFE/MAE and entry/exit/source performance enter future features | `research.py`, `features.py` | existing profile-to-inference E2E plus full suite | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 20 | Dynamic hypotheses | Four fixed pairs | Registry-derived effects, pair/triple candidates, correlation/sample/complexity controls | `hypothesis_engine.py`, `research.py` | unseen feature interaction discovery | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 21 | Multiple testing | Proxy threshold only | Benjamini-Hochberg q-values gate knowledge promotion | `hypothesis_engine.py` | BH FDR test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 22 | Drift engine | One combined concept/calibration flag | Eight independent drift metrics and mapped actions | `drift.py`, `research.py` | all-eight drift test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 23 | Failure attribution | Three shallow labels | Full required vocabulary with persisted supporting evidence | `attribution.py`, `research.py` | evidence/vocabulary test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 24 | Missed opportunity cause | Threshold-or-mapping binary | Eleven-stage root-cause decision with evidence | `attribution.py`, `research.py` | missed-cause test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 25 | Fundamental direction | Absolute numeric sign | Macro/derivatives/on-chain/corporate/energy contextual semantics | `observation.py` | CPI/OI/energy semantic test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 26 | Surprise/priced-in | Text proxy | Actual/forecast/consensus/previous/revision distribution plus anticipation context | `semantic_engines.py`, `service.py` | contextual engine test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 27 | Telegram producers | Kinds and formatters | Ten business-condition producers route to durable queue and renderer with quality/rate/event gates | `telegram_producers.py`, services, `alerts/telegram.py` | all-ten condition-to-render test | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |
| 28 | Linux clean exit | No dedicated process diagnostic | Bounded monitor loop, no default executor leak, POSIX shell diagnostic and syntax validation | `active_signal_monitor.py`, `bin/verify_linux_clean_exit.sh` | clean task/thread and bash execution tests | IMPLEMENTED_AND_BEHAVIOR_VERIFIED |

`LEVEL_A_FAILED = 0`.
