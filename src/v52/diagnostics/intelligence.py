from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from typing import Any

from ..config import Settings
from ..intelligence.research import ResearchService
from ..intelligence.store import BrainStore
from ..metrics import Metrics
from ..intelligence.provider_manifest import validate_manifest
from ..intelligence.warmup import RuntimeWarmup


def _rows(settings: Settings, section: str, limit: int) -> list[dict[str, Any]]:
    path = settings.data_dir / "brain" / "intelligence.sqlite3"
    if not path.exists():
        return []
    queries = {
        "models": "SELECT model_version_id,model_name,role,status,parent_version_id,metrics_json,created_at,promoted_at,retired_at FROM model_versions ORDER BY created_at DESC LIMIT ?",
        "signals": "SELECT s.*,p.asset_id,p.horizon_seconds,p.calibrated_confidence,p.uncertainty FROM signals s JOIN predictions p ON p.id=s.prediction_id ORDER BY s.id DESC LIMIT ?",
        "events": "SELECT id,event_type,domain,title,importance,novelty,priority,state,update_count,created_at FROM events ORDER BY id DESC LIMIT ?",
        "learning": "SELECT * FROM knowledge ORDER BY sample_count DESC,updated_at DESC LIMIT ?",
        "drift": "SELECT * FROM drift_events ORDER BY id DESC LIMIT ?",
        "reports": "SELECT report_date,mode,report_json,created_at FROM daily_reports ORDER BY report_date DESC LIMIT ?",
        "data-status": "SELECT 'canonical_observations' kind,COUNT(*) count FROM canonical_observations UNION ALL SELECT 'feature_vectors',COUNT(*) FROM feature_vectors UNION ALL SELECT 'reference_prices',COUNT(*) FROM reference_prices",
        "asset-status": "SELECT a.asset_id,a.symbol,a.name,a.asset_class,a.market_symbol,ap.sample_count,ap.updated_at,w.state watchlist_state,w.expires_at FROM assets a LEFT JOIN asset_profiles ap USING(asset_id) LEFT JOIN active_asset_watchlist w USING(asset_id) ORDER BY COALESCE(ap.sample_count,0) DESC,a.asset_id LIMIT ?",
        "feature-status": "SELECT feature_version,asset_id,horizon_seconds,COUNT(*) snapshots,MAX(prediction_time) latest,AVG(quality) mean_quality FROM feature_vectors GROUP BY feature_version,asset_id,horizon_seconds ORDER BY latest DESC LIMIT ?",
        "expert-status": "SELECT expert,state,model_family,artifact_id,metrics_json FROM model_artifacts ORDER BY expert,state,created_at DESC LIMIT ?",
        "model-status": "SELECT * FROM model_artifacts ORDER BY created_at DESC LIMIT ?",
        "shadow-status": "SELECT ma.expert,ma.artifact_id,ma.model_family,COUNT(sp.id) predictions,SUM(CASE WHEN sp.resolved_at IS NOT NULL THEN 1 ELSE 0 END) resolved,MAX(sp.resolved_at) latest_resolution FROM model_artifacts ma LEFT JOIN shadow_predictions sp USING(artifact_id) WHERE ma.state='SHADOW' GROUP BY ma.artifact_id ORDER BY ma.created_at DESC LIMIT ?",
        "signal-status": "SELECT s.*,p.asset_id,p.model_version_id FROM signals s JOIN predictions p ON p.id=s.prediction_id ORDER BY s.id DESC LIMIT ?",
        "active-signals": "SELECT s.id,s.state,s.action,s.position_fraction,s.partial_exit_count,p.asset_id,p.horizon_seconds,ass.observed_at,ass.price,ass.return_since_entry,ass.mfe_so_far,ass.mae_so_far,ass.remaining_ev,ass.action monitor_action,ass.evidence_json FROM signals s JOIN predictions p ON p.id=s.prediction_id LEFT JOIN active_signal_snapshots ass ON ass.id=(SELECT id FROM active_signal_snapshots x WHERE x.signal_id=s.id ORDER BY x.observed_at DESC LIMIT 1) WHERE s.state IN ('ACTIVE','UPDATED','PARTIAL_EXIT','EXIT_PENDING') ORDER BY s.last_updated_at DESC LIMIT ?",
        "learning-status": "SELECT hypothesis_id,status,statement,sample_count,effect_size,confidence_low,confidence_high FROM hypotheses ORDER BY updated_at DESC LIMIT ?",
    }
    if section not in queries:
        return []
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    query=queries[section]
    rows = [dict(row) for row in con.execute(query, (limit,) if '?' in query else ()).fetchall()]
    con.close()
    for row in rows:
        for key in tuple(row):
            if key.endswith("_json") and isinstance(row[key], str):
                try:
                    row[key] = json.loads(row[key])
                except (TypeError, ValueError, json.JSONDecodeError):
                    row[key + "_decode_error"] = True
    return rows


async def inspect(section: str, limit: int) -> dict[str, Any]:
    settings = Settings.from_env()
    settings.ensure_directories()
    brain = BrainStore(settings)
    result: dict[str, Any] = {"section": section, "summary": await brain.summary(), "mode": await brain.mode()}
    if section == "qualification":
        research = ResearchService(settings, Metrics("brain-diagnostic", settings.state_dir))
        result["qualification"] = await research.qualification()
    elif section == 'provider-status':
        result['providers']=validate_manifest(settings)
    elif section == 'brain-status':
        result['warmup']=await RuntimeWarmup(settings).status()
    elif section != "summary":
        result["rows"] = _rows(settings, section, limit)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="V57 intelligence/model/signal/learning diagnostics")
    parser.add_argument("section", choices=("summary", "qualification", "models", "signals", "events", "learning", "drift", "reports","brain-status","data-status","provider-status","asset-status","feature-status","expert-status","model-status","shadow-status","signal-status","active-signals","learning-status"), default="summary", nargs="?")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(inspect(args.section, max(1, min(1000, args.limit)))), indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
