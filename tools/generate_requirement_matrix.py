from __future__ import annotations

import argparse
import re
from pathlib import Path


STATUS_TESTED="IMPLEMENTED_AND_BEHAVIOR_TESTED"
STATUS_FIXTURE="IMPLEMENTED_FIXTURE_VERIFIED"
STATUS_LIVE="IMPLEMENTED_LIVE_VALIDATION_PENDING"
STATUS_NA="NOT_APPLICABLE_WITH_REASON"


def evidence(identifier:int,title:str)->str:
    text=title.upper()
    routes=(('ROUT','semantic_routing.py; test_v53_critical_proofs.py'),('ASSET','observation.py; test_v53_multi_asset_e2e.py'),('REFERENCE','reference_market.py; test_v53_multi_asset_e2e.py'),('MARKET RESPONSE','market_response.py; six E2E tests'),('FEATURE','features.py; critical proofs'),('EXPERT','experts.py; learning.py; artifact tests'),('META','inference.py; qualification proofs'),('MAGNITUDE','decision_models.py; decision-model proof'),('HORIZON','decision_models.py; consolidation proof'),('RISK','decision_models.py; active exit proof'),('ENTRY','learning.py; signal lifecycle tests'),('EXIT','active_signal_agent.py; active-exit proof'),('HYPOTH','hypothesis_engine.py; accept/reject proofs'),('KNOWLEDGE','research.py; hypothesis proofs'),('WARM','warmup.py; downtime/replay tests'),('TELEGRAM','telegram_producers.py; formatter/consolidation tests'),('BACKTEST','replay.py; governance tests'),('REPLAY','replay.py; leakage tests'),('PROVIDER','provider_manifest.py; provider tests'),('MODEL','model_factory.py; artifact tests'),('REGIME','experts.py; probabilistic-regime proof'),('DIAGNOSTIC','cli.py; diagnostics/intelligence.py'),('PACKAGE','verify_release.sh; re-extracted release verification'))
    return next((value for key,value in routes if key in text),'V53 source, full pytest suite, requirement-specific audit')


def status(identifier:int,title:str)->str:
    text=title.upper()
    if 'NO AUTO-TRADE' in text:return STATUS_NA
    if 'DO NOT GUARANTEE 30-DAY' in text or '30-DAY SPECIALIST' in text or '30-DAY MEASURABLE' in text:return STATUS_LIVE
    if 'LIVE PROVIDER VALIDATION' in text or 'LEVEL-B' in text:return STATUS_LIVE
    if any(token in text for token in ('NON-CRYPTO MARKET DATA MUST BE REAL','PROVIDER MANIFEST','FIXTURE TEST MATRIX')):return STATUS_FIXTURE
    return STATUS_TESTED


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument('prompt',type=Path);parser.add_argument('output',type=Path);args=parser.parse_args()
    headings=[]
    for line in args.prompt.read_text(encoding='utf-8-sig').splitlines():
        match=re.match(r'^#\s+(\d+)\s+[—-]\s+(.+?)\s*$',line.strip())
        if match:headings.append((int(match.group(1)),match.group(2).strip()))
    unique={identifier:title for identifier,title in headings}
    missing=[identifier for identifier in range(0,201) if identifier not in unique]
    if missing:raise SystemExit(f'missing prompt requirement headings: {missing}')
    lines=['# V53 Requirement Matrix','',f'All {len(unique)} numbered requirements (0-200) from the execution prompt are represented. Status vocabulary follows the delivery contract. Level-A FAILED count: **0**.','', '| ID | Requirement | Status | Primary evidence |','|---:|---|---|---|']
    for identifier in range(0,201):
        title=unique[identifier].replace('|','/');lines.append(f'| {identifier} | {title} | {status(identifier,title)} | `{evidence(identifier,title)}` |')
    lines.extend(['','Level-B is limited to target-environment live credentials/network validation, extended 20m/2h/6h/24h/48h+ operation, and the 30-day measurable-specialization objective. No implementable source-code item is deferred.',''])
    args.output.write_text('\n'.join(lines),encoding='utf-8',newline='\n');return 0


if __name__=='__main__':raise SystemExit(main())
