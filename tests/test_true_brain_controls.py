from __future__ import annotations

import asyncio
import builtins
import json

import pytest

from v52.intelligence.features import FeatureStore
from v52.intelligence.inference import RuntimeInferenceEngine
from v52.intelligence.model_factory import ModelFactory
from v52.intelligence.observation import CanonicalObservation, ObservationBridge
from v52.intelligence.replay import HistoricalImporter, ReplayEngine, WalkForwardEngine
from v52.intelligence.signal_state import SignalStateMachine
from v52.intelligence.store import BrainStore
from v52.storage.sqlite import connect


def _rows(key: str):
    return [
        {
            "prediction_time": 1000 + i,
            "feature_version": "features-52.true.1",
            "values": {key: -1 + 2 * i / 39, "data_quality": .9},
            "label": int(i >= 20),
        }
        for i in range(40)
    ]


def _champion(settings, artifact_id: str, expert: str):
    path = settings.data_dir / "brain" / "intelligence.sqlite3"
    with connect(path, settings) as con:
        con.execute("UPDATE model_artifacts SET state='RETIRED' WHERE expert=? AND state='CHAMPION'", (expert,))
        con.execute("UPDATE model_artifacts SET state='CHAMPION' WHERE artifact_id=?", (artifact_id,))


def test_timestamp_contract_rejects_invalid_observations():
    with pytest.raises(ValueError, match="timestamp contract"):
        CanonicalObservation("bad", 200, 100, 300, "x", .5, (), (), "CPI", 1, "", "UP", .5, 0, .5, "x", {})


def test_source_conflict_and_cross_asset_features_are_point_in_time(settings):
    brain = BrainStore(settings)
    bridge = ObservationBridge(settings)
    for source, value in (("official-a", 2.0), ("official-b", -2.0)):
        bridge.persist_sync(CanonicalObservation(source, 100, 150, 160, source, .9, ("XAUUSD",), (), "REAL_YIELD", value, "%", "UP" if value > 0 else "DOWN", .9, 60, .9, source, {}))
    with connect(brain.path, settings) as con:
        for source, reliability in (("official-a", .95), ("official-b", .7)):
            con.execute("INSERT INTO source_profiles(source_id,reliability,updated_at) VALUES(?,?,1)", (source, reliability))
        con.execute("INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,quality,metadata_json) VALUES('fixture','DXY',100,100,100,.9,'{}')")
        con.execute("INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,quality,metadata_json) VALUES('fixture','DXY',200,200,102,.9,'{}')")
    feature = FeatureStore(settings).build_sync({"id": None, "importance": .8, "novelty": .7, "source_credibility": .9}, {"asset_id": "XAUUSD", "mapping_confidence": .9}, 300, prediction_time=250)
    assert feature["values"]["source_conflict"] > .5
    assert feature["values"]["cross_dxy_return"] == pytest.approx(.02)
    assert set(feature["provenance"]) == {"official-a", "official-b"}


def test_token_and_meta_artifacts_predict_with_bootstrap_disabled(settings):
    brain = BrainStore(settings)
    factory = ModelFactory(settings)
    token = factory.train("token", _rows("net_exchange_flow"))
    meta = factory.train("meta", _rows("expert_token"))
    _champion(settings, token["artifact_id"], "token")
    _champion(settings, meta["artifact_id"], "meta")
    values = {"net_exchange_flow": .9, "data_quality": .9}
    with connect(brain.path, settings) as con:
        con.execute("INSERT INTO feature_vectors(feature_id,event_id,asset_id,horizon_seconds,feature_version,prediction_time,available_cutoff,quality,values_json,provenance_json,replay,created_at) VALUES('token-no-bootstrap',NULL,'SOL',300,'features-52.true.1',2000,2000,.9,?,'[]',0,2000)", (json.dumps(values),))
    votes, decision, evidence = RuntimeInferenceEngine(settings).infer(
        {"id": None, "domain": "CRYPTO", "event_type": "TOKEN_UNLOCK", "importance": .9, "novelty": .8, "source_credibility": .9},
        {"asset_id": "SOL", "asset_class": "TOKEN", "directional_bias": "UP", "mapping_confidence": .9},
        300,
        {"feature_id": "token-no-bootstrap", "prediction_time": 2000, "quality": .9, "values": values},
        "SHADOW",
        [],
        allow_bootstrap=False,
    )
    assert any(v.expert == "token" for v in votes)
    assert evidence["source"] == "ARTIFACT_META"
    assert evidence["controlling_artifact_id"] == meta["artifact_id"]
    assert decision.probability != .5


def test_core_model_dependency_fallback_is_trainable(settings, monkeypatch):
    BrainStore(settings)
    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("sklearn"):
            raise ImportError("fixture: optional ML tier unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    result = ModelFactory(settings).train("gold", _rows("real_yield"))
    assert result["family"] in {"bayesian", "logistic", "elastic_net", "sgd"}
    assert ModelFactory(settings).registry.load(result["artifact_id"]).predict({"real_yield": .8}) != .5


def test_replay_order_walk_forward_and_warmup_marker(settings, tmp_path):
    BrainStore(settings)
    path = tmp_path / "observations.jsonl"
    path.write_text("\n".join(json.dumps({"observation_id": f"r{i}", "event_time": i, "available_time": i + 1, "ingestion_time": i + 2, "source_id": "fixture", "observation_type": "CPI", "value": i}) for i in range(30)), encoding="utf-8")
    assert HistoricalImporter(settings).import_observations_jsonl(path) == 30
    seen = []

    async def consume(row):
        seen.append((row["available_time"], row["_replay"]))

    assert asyncio.run(ReplayEngine(settings).replay(consume)) == 30
    assert seen == sorted(seen) and all(marker for _, marker in seen)
    rows = [{"prediction_time": i} for i in range(30)]
    splits = WalkForwardEngine.splits(rows, folds=3, embargo_rows=2)
    assert splits and all(max(rows[i]["prediction_time"] for i in train) < min(rows[i]["prediction_time"] for i in valid) for train, valid in splits)


def test_signal_lifecycle_is_validated_and_restart_persistent(settings):
    brain = BrainStore(settings)
    with connect(brain.path, settings) as con:
        event_id = con.execute("INSERT INTO events(cluster_key,event_type,domain,title,summary,occurred_at,first_observed_at,last_observed_at,importance,novelty,surprise,source_credibility,priority,state,extraction_method,extraction_version,created_at,updated_at) VALUES('lifecycle','GENERAL','GENERAL','x','x','x','x','x',.8,.8,0,.9,'P1','INITIAL','fixture','1',1,1)").lastrowid
        prediction_id = con.execute("INSERT INTO predictions(prediction_key,event_id,asset_id,horizon_seconds,direction_probability,calibrated_confidence,expected_magnitude,uncertainty,expected_value,risk_score,action,mode,model_version_id,feature_version,feature_cutoff_at,evidence_json,created_at) VALUES('lifecycle',?,'XAUUSD',300,.7,.7,.01,.2,.01,.2,'BUY','QUALIFIED','artifact:test','features-52.true.1','1','{}',1)", (event_id,)).lastrowid
        signal_id = con.execute("INSERT INTO signals(signal_key,prediction_id,state,action,entry_logic,invalidation_logic,target_logic,exit_logic,opened_at,telegram_state,last_updated_at) VALUES('lifecycle',?,'CREATED','BUY','x','x','x','x',1,'SUPPRESSED',1)", (prediction_id,)).lastrowid
    first = SignalStateMachine(settings)
    assert first.transition_sync(signal_id, "QUALIFIED", "fixture") == "QUALIFIED"
    with pytest.raises(ValueError, match="invalid signal transition"):
        first.transition_sync(signal_id, "ACTIVE", "skip forbidden")
    restarted = SignalStateMachine(settings)
    for state in ("SENT", "ENTRY_PENDING", "ACTIVE", "EXIT_PENDING", "EXITED", "POST_EXIT", "EVALUATED", "LEARNED"):
        restarted.transition_sync(signal_id, state, "restart fixture")
    with connect(brain.path, settings) as con:
        assert con.execute("SELECT state FROM signals WHERE id=?", (signal_id,)).fetchone()["state"] == "LEARNED"
        assert con.execute("SELECT COUNT(*) count FROM signal_lifecycle WHERE signal_id=?", (signal_id,)).fetchone()["count"] == 9
