from __future__ import annotations

import asyncio
import json
import time

from v52.intelligence.context_builder import RuntimeSemanticContextBuilder
from v52.intelligence.drift import DriftEngine
from v52.intelligence.observation import DynamicAssetUniverse, ObservationBridge
from v52.intelligence.research import ResearchService, _benjamini_hochberg
from v52.intelligence.store import BrainStore
from v52.metrics import Metrics
from v52.storage.sqlite import connect


def _init(settings):
    BrainStore(settings)
    return settings.data_dir / "brain" / "intelligence.sqlite3"


def test_structured_batch_expands_rows_without_cross_asset_collapse(settings):
    _init(settings)
    bridge = ObservationBridge(settings)
    now = time.time()
    payload = {"unit": "USD", "data": [{"symbol": "BTC", "price": 100.0}, {"symbol": "ETH", "price": 200.0}]}
    row = {"id": 1, "payload_json": json.dumps(payload), "payload_hash": "batch", "observed_at": now, "provider": "fixture", "lane": "MARKET", "record_type": "QUOTE_BATCH"}
    observations = bridge.normalize_many(row)
    assert len(observations) == 2
    assert {o.asset_ids for o in observations} == {("BTC",), ("ETH",)}
    assert {o.value for o in observations} == {100.0, 200.0}
    assert [o.metadata["batch_index"] for o in observations] == [0, 1]
    try:
        bridge.normalize(row)
    except ValueError as exc:
        assert "normalize_many" in str(exc)
    else:
        raise AssertionError("batch normalize() must refuse silent row collapse")


def test_semantic_context_is_strictly_anchored_before_event(settings):
    path = _init(settings)
    builder = RuntimeSemanticContextBuilder(settings)
    now = time.time()
    anchor = now - 100
    with connect(path, settings) as con:
        con.execute("INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('fixture','BTC',?,?,100,NULL,.9,'{}')", (anchor - 20, anchor - 20))
        con.execute("INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('fixture','BTC',?,?,105,NULL,.9,'{}')", (anchor - 10, anchor - 10))
        con.execute("INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('fixture','BTC',?,?,150,NULL,.9,'{}')", (anchor + 50, anchor + 50))
        con.execute("INSERT INTO canonical_observations(observation_id,event_time,available_time,ingestion_time,source_id,source_quality,asset_ids_json,entity_ids_json,observation_type,value,unit,direction,confidence,freshness_seconds,quality,raw_reference,metadata_json,replay,provider_id,market,observation_family) VALUES('pre-pos',?,?,?,'.fixture',.9,'[\"BTC\"]','[]','POSITIONING_ZSCORE',1.2,'','NEUTRAL',.9,0,.9,'x','{}',0,'fixture','BTC','DERIVATIVES')", (anchor - 10, anchor - 10, anchor - 10))
        con.execute("INSERT INTO canonical_observations(observation_id,event_time,available_time,ingestion_time,source_id,source_quality,asset_ids_json,entity_ids_json,observation_type,value,unit,direction,confidence,freshness_seconds,quality,raw_reference,metadata_json,replay,provider_id,market,observation_family) VALUES('post-pos',?,?,?,'.fixture',.9,'[\"BTC\"]','[]','POSITIONING_ZSCORE',9.9,'','NEUTRAL',.9,0,.9,'x','{}',0,'fixture','BTC','DERIVATIVES')", (anchor + 50, anchor + 50, anchor + 50))
    context = builder.build("TEST", ["BTC"], {}, [], event_anchor=anchor, decision_cutoff=now)
    assert abs(context["pre_event_price_move"] - .05) < 1e-12
    assert context["positioning_zscore"] == 1.2
    price_prov = next(x for x in context["context_provenance"] if x["field"] == "pre_event_price_move")
    assert max(price_prov["timestamps"]) <= anchor
    assert context["event_anchor"] == anchor


def test_surprise_history_is_raw_unit_matched_and_pre_anchor(settings):
    path = _init(settings)
    builder = RuntimeSemanticContextBuilder(settings)
    now = time.time(); anchor = now - 100
    with connect(path, settings) as con:
        base = """INSERT INTO events(cluster_key,event_type,domain,title,summary,occurred_at,first_observed_at,last_observed_at,importance,novelty,surprise,source_credibility,priority,state,extraction_method,extraction_version,created_at,updated_at,raw_surprise,standardized_surprise,surprise_unit,surprise_family) VALUES(?,?,?,?,?,'x','x','x',.8,.8,?,.8,'P1','INITIAL','fixture','57',?,?,?,?,?,?)"""
        con.execute(base, ("old-same","CPI","MACRO","a","a",2.5,anchor-20,anchor-20,.2,2.5,"pct","CPI"))
        con.execute(base, ("old-other-unit","CPI","MACRO","b","b",2.5,anchor-15,anchor-15,99.0,2.5,"bps","CPI"))
        con.execute(base, ("future-same","CPI","MACRO","c","c",2.5,anchor+10,anchor+10,77.0,2.5,"pct","CPI"))
    context = builder.build("CPI", [], {"surprise_unit":"pct","surprise_family":"CPI"}, [], event_anchor=anchor, decision_cutoff=now)
    assert context["historical_surprise_distribution"] == [0.2]


def test_seed_equities_are_exchange_aware_and_aliases_resolve_canonical(settings):
    path = _init(settings)
    universe = DynamicAssetUniverse(settings)
    with connect(path, settings) as con:
        rows = con.execute("SELECT asset_id,symbol,metadata_json FROM assets WHERE asset_class='EQUITY'").fetchall()
    assert rows
    assert all(str(r["asset_id"]).startswith("equity:") for r in rows)
    assert universe.discover({"headline": "Apple raises guidance"}) == ("equity:XNAS:AAPL",)


def test_bh_fdr_is_monotone_and_controls_family():
    q = _benjamini_hochberg([.001, .02, .04, .5])
    assert q[0] <= q[1] <= q[2] <= q[3]
    assert q[0] <= .01 and q[-1] == .5


def test_drift_heavy_action_is_queued_then_really_consumed(settings, monkeypatch):
    path = _init(settings)
    engine = DriftEngine(settings)
    result = {"scope":{"assets":["BTC"],"sources":[]},"types":{name:{"detected":False,"action":"NONE"} for name in ("DATA_DRIFT","FEATURE_DRIFT","CONCEPT_DRIFT","SOURCE_DRIFT","PREDICTION_DRIFT","CALIBRATION_DRIFT","REGIME_DRIFT","ASSET_BEHAVIOR_DRIFT")}}
    result["types"]["FEATURE_DRIFT"] = {"detected":True,"action":"RETRAIN","per_feature_scores":{"funding_rate":1.0}}
    requested = engine.execute_actions(result)
    item = next(x for x in requested if x["drift_type"] == "FEATURE_DRIFT")
    assert item["execution_status"] == "QUEUED"
    research = ResearchService(settings, Metrics("v57-test", settings.state_dir))
    monkeypatch.setattr(research.training, "train_eligible", lambda: [{"created": True, "expert":"crypto"}])
    monkeypatch.setattr(research.training, "train_decision_eligible", lambda: [{"created": True, "expert":"risk"}])
    monkeypatch.setattr(research.training, "train_meta_eligible", lambda: {"created": True, "expert":"meta"})
    completed = asyncio.run(research._consume_drift_remediations(requested))
    done = next(x for x in completed if x["drift_type"] == "FEATURE_DRIFT")
    assert done["execution_status"] == "COMPLETED"
    with connect(path, settings) as con:
        q = con.execute("SELECT state,attempts FROM drift_remediation_queue WHERE id=?", (item["queue_id"],)).fetchone()
        action = con.execute("SELECT execution_status FROM drift_actions WHERE id=?", (item["drift_action_id"],)).fetchone()
    assert q["state"] == "COMPLETED" and q["attempts"] == 1
    assert action["execution_status"] == "COMPLETED"
