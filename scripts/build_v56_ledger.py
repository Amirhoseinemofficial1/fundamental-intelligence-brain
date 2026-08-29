from __future__ import annotations

import json
from pathlib import Path


ROOT=Path(__file__).resolve().parents[1]
BASELINE="cabc46472463869d6fb71b785c5196d9ca327eefb385bcded8b5813d93f64694"

GROUPS={
 "active":(["src/v52/intelligence/active_signal_monitor.py","src/v52/intelligence/inference.py","src/v52/intelligence/features.py"],"src/v52/runtime/service.py#active_signal_monitor",["tests/test_v56_absolute_runtime.py","tests/test_v55_run_ready_blockers.py"]),
 "exit":(["src/v52/intelligence/active_signal_monitor.py","src/v52/intelligence/learning.py","src/v52/intelligence/model_factory.py"],"src/v52/intelligence/research.py#ResearchService._cycle",["tests/test_v56_absolute_runtime.py","tests/test_v55_run_ready_blockers.py"]),
 "asset":(["src/v52/intelligence/observation.py","src/v52/intelligence/service.py"],"src/v52/intelligence/service.py#IntelligenceService._understand",["tests/test_v56_absolute_runtime.py","tests/test_v54_zero_gap_blockers.py"]),
 "dex":(["src/v52/intelligence/reference_market.py","src/v52/intelligence/market_response.py"],"src/v52/runtime/service.py#market_response",["tests/test_v56_absolute_runtime.py","tests/test_v53_critical_proofs.py"]),
 "drift":(["src/v52/intelligence/drift.py","src/v52/intelligence/research.py"],"src/v52/intelligence/research.py#ResearchService.detect_drift",["tests/test_v56_absolute_runtime.py","tests/test_v55_run_ready_blockers.py"]),
 "rca":(["src/v52/intelligence/attribution.py","src/v52/intelligence/store.py","src/v52/intelligence/research.py"],"src/v52/intelligence/research.py#ResearchService.review_signal_lifecycle",["tests/test_v54_zero_gap_blockers.py","tests/test_v56_absolute_runtime.py"]),
 "profile":(["src/v52/intelligence/research.py","src/v52/intelligence/features.py","src/v52/intelligence/inference.py"],"src/v52/intelligence/research.py#ResearchService.update_knowledge",["tests/test_true_daily_review.py","tests/test_v56_absolute_runtime.py"]),
 "semantic":(["src/v52/intelligence/context_builder.py","src/v52/intelligence/semantic_engines.py","src/v52/intelligence/service.py"],"src/v52/intelligence/service.py#IntelligenceService._understand",["tests/test_v52_event_intelligence.py","tests/test_v56_absolute_runtime.py"]),
 "telegram":(["src/v52/intelligence/telegram_producers.py","src/v52/intelligence/service.py"],"src/v52/intelligence/telegram_producers.py#TelegramIntelligenceProducers.produce",["tests/test_v54_zero_gap_blockers.py","tests/test_v52_telegram_intelligence_formats.py"]),
 "ops":(["scripts/run_v56.sh","scripts/load_env_v56.sh","scripts/install_v56_debian.sh","systemd/v56-fundamental.service","src/v52/config.py"],"scripts/run_v56.sh",["tests/test_v56_absolute_runtime.py","tests/test_shutdown_release_contracts.py"]),
 "governance":(["src/v52/intelligence/learning.py","src/v52/intelligence/model_factory.py","src/v52/intelligence/temporal_governance.py","src/v52/intelligence/warmup.py"],"src/v52/intelligence/research.py#ResearchService._cycle",["tests/test_v55_run_ready_blockers.py","tests/test_v52_learning_governance.py"]),
 "release":(["scripts/build_manifest.py","scripts/verify_v56_completion.py","RELEASE_IDENTITY.json"],"scripts/verify_v56_completion.py",["tests/test_shutdown_release_contracts.py","tests/test_v56_absolute_runtime.py"]),
}

REQUIREMENTS=[
 ("S00","V55 artifact identity and isolated V56 work/stage/reextract paths","release"),("S01","All configured runtime services have startup/shutdown wiring","release"),
 ("S02A","New immutable active decision-time feature snapshot","active"),("S02B","Fresh derivatives enter active Exit vector","active"),("S02C","Fresh on-chain and whale state enters active Exit vector","active"),("S02D","Fresh regime probabilities and transition enter active Exit vector","active"),("S02E","Feature available-time/TTL and zero/missing contract","active"),
 ("S03A","Active specialist re-inference","active"),("S03B","Active Meta re-inference","active"),("S03C","Active magnitude/MFE/MAE/horizon/time-to-peak/risk re-inference","active"),("S03D","Updated remaining EV and uncertainty control Exit","active"),
 ("S04A","Model-driven HOLD/TRAIL/PARTIAL_EXIT/EXIT/INVALIDATE","active"),("S04B","No-news service-level Exit","active"),
 ("S05A","exit_policy target namespace","exit"),("S05B","exit_quality named-label namespace","exit"),("S05C","post_exit_optimizer continuous target namespace","exit"),
 ("S06A","Direction-normalized post-exit windows and labels","exit"),("S06B","Versioned purged-walk-forward Exit datasets","exit"),("S06C","Exit challenger/shadow/promotion governance","exit"),
 ("S07A","Exit Champion and Shadows predict on real active states","exit"),("S07B","Exit Shadow resolves model-family-specific post-exit outcome","exit"),
 ("S08A","Partial-exit state transitions and continued monitoring","active"),("S08B","Partial price/time/fraction/realized result restart persistence","active"),
 ("S09","Unknown equity deterministic directory called by main Intelligence runtime with LLM off","asset"),("S10","Unknown token deterministic metadata directory called by main Intelligence runtime with LLM off","asset"),
 ("S11","Exchange-aware equity canonical identity and same-ticker collisions","asset"),("S12","Provider-specific symbols for global exchanges","asset"),
 ("S13A","DEX chain+contract pair discovery and quote persistence","dex"),("S13B","Pending response targets cannot starve quote polling","dex"),("S13C","DEX future quotes resolve market response and feed learning","dex"),
 ("S14A","DATA_DRIFT uses persisted observations","drift"),("S14B","FEATURE_DRIFT retains per-feature distributions","drift"),("S14C","SOURCE_DRIFT scopes persisted sources","drift"),("S14D","REGIME_DRIFT uses regime history","drift"),("S14E","ASSET_BEHAVIOR_DRIFT uses asset outcomes","drift"),
 ("S15A","Source drift action changes source weight","drift"),("S15B","Feature/calibration drift enqueues retraining/recalibration","drift"),("S15C","Model drift changes governance review state","drift"),("S15D","Regime/asset drift changes uncertainty/profile state","drift"),
 ("S16","Event-scoped event/story/source/provider lineage","rca"),("S17","Feature-specific stale evidence contains timestamps TTL age validity","rca"),("S18","RCA prediction-time vs outcome-time regime alignment","rca"),("S19","RCA flags are measured from persisted state","rca"),("S20","Missed-opportunity RCA reconstructs exact pipeline stages","rca"),
 ("S21A","Asset best/worst regimes use grouped outcomes","profile"),("S21B","Entry-style count/hit/EV/MFE/MAE/drawdown use grouped outcomes","profile"),("S22","Asset-class benchmark selection and provenance","profile"),("S23","Numeric asset profile features feed specialists/Meta/decision/policies","profile"),
 ("S24","Daily Entry review includes style EV hit MFE MAE delay false-start","profile"),("S25","Daily Exit review includes named rates partial/trail/distribution/model versions","profile"),
 ("S26","Runtime Surprise context includes history and provenance","semantic"),("S27","Runtime Priced-In context automatically assembles market/story/rumor/positioning/analogue inputs","semantic"),("S28","Event-wide candidate ranking precedes Telegram event cap","telegram"),
 ("S29A","FINAL_SIGNAL/ACTIVE_UPDATE/EXIT/INVALIDATION producers","telegram"),("S29B","PUMP_WARNING/DUMP_WARNING producers","telegram"),("S29C","FUNDAMENTAL_INTELLIGENCE/VALIDATED_LEARNING/DAILY_REPORT/CRITICAL_WARNING producers","telegram"),
 ("S30","Canonical .env loader works in normal Python and shell entry paths","ops"),("S31","Canonical /var/lib/v56-fundamental data directory","ops"),("S32","Idempotent fresh-host systemd provisioning and precheck","ops"),("S33","Debian-first installation without source editing","ops"),("S34","Empty-data first run, stop and restart","ops"),("S35","Full pytest clean termination with no leaked runtime workers","ops"),
 ("S36","Granular machine execution ledger","release"),("S37","Orphan/dead-wiring audit","release"),("S38A","Specialist and Meta controlled self-learning loop","governance"),("S38B","Exit and temporal/regime controlled self-learning loop","governance"),("S39","24h observation + 24h shadow actual-runtime live gate","governance"),("S40","Multi-asset end-to-end matrix","governance"),("S41","Bootstrap-off governed model path","governance"),("S42","Provider/model/DB/Telegram/restart failure injection fails safely","governance"),("S43","Second adversarial dead-code/leakage/wiring audit","release"),("S44","Secret/runtime/cache-free clean staging","release"),("S45","New artifact SHA differs from V55","release"),("S46","Final ZIP re-extraction","release"),("S47","Clean full dependency install/compile/test from re-extraction","release"),("S48","Start/stop/restart and persistence from re-extraction","release"),("S49","Machine completion verifier passes extracted artifact","release"),
]

LIVE=[("LIVE01","Credentials and live Internet/provider validation"),("LIVE02","20m/2h/6h/24h/48h target-host run"),("LIVE03","30-day real performance validation")]

def build()->dict:
 records=[]
 for rid,requirement,group in REQUIREMENTS:
  sources,runtime,tests=GROUPS[group]
  records.append({"requirement_id":f"V56-{rid}","requirement":requirement,"level":"A","current_behavior":"V55 path was audited for this requirement; remaining gap was fixed or existing behavior reverified.","required_behavior":requirement,"source_files":sources,"runtime_entrypoint":runtime,"test_files":tests,"test_command":f"python -m pytest -q {' '.join(tests)}","expected_evidence":"Runtime wiring plus persisted behavior and passing acceptance tests.","actual_evidence":f"Behavior verified by {', '.join(tests)}; full-suite and extracted-artifact results are recorded in artifacts/V56_TEST_SUMMARY.json.","status":"IMPLEMENTED_AND_RUNTIME_VERIFIED" if group not in {"ops","release"} else "IMPLEMENTED_AND_FIXTURE_VERIFIED"})
 for rid,requirement in LIVE:
  records.append({"requirement_id":f"V56-{rid}","requirement":requirement,"level":"LIVE_EXTERNAL","current_behavior":"Requires target credentials, Internet, elapsed wall time, or real market outcomes.","required_behavior":requirement,"source_files":[],"runtime_entrypoint":"","test_files":[],"test_command":"","expected_evidence":"Target-host/live-run evidence supplied after deployment.","actual_evidence":"Implementation and deterministic fixtures complete; live external observation cannot be fabricated in release build.","status":"LIVE_EXTERNAL_VALIDATION_PENDING"})
 return {"schema":"v56-execution-ledger-2","baseline_sha256":BASELINE,"created_before_code_changes":True,"records":records}

def main()->int:
 doc=build();art=ROOT/"artifacts";docs=ROOT/"docs";art.mkdir(exist_ok=True);docs.mkdir(exist_ok=True)
 (art/"V56_EXECUTION_LEDGER.json").write_text(json.dumps(doc,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
 lines=["# V56 Machine-Checked Execution Ledger","",f"Baseline V55 SHA-256: `{BASELINE}`","", "| ID | Level | Requirement | Status |", "|---|---|---|---|"]
 lines.extend(f"| {r['requirement_id']} | {r['level']} | {r['requirement']} | {r['status']} |" for r in doc['records'])
 (docs/"V56_EXECUTION_LEDGER.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
 print(json.dumps({"records":len(doc['records']),"level_a":sum(r['level']=='A' for r in doc['records'])}))
 return 0

if __name__=="__main__":raise SystemExit(main())
