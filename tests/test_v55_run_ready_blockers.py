from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest

from v52.intelligence.active_signal_monitor import ActiveSignalMonitorService
from v52.intelligence.attribution import FailureAttributionEngine
from v52.intelligence.drift import DRIFT_ACTIONS, DRIFT_TYPES, DriftDataCollector
from v52.intelligence.hypothesis_engine import FeatureInteractionHypothesisEngine
from v52.intelligence.learning import TrainingCoordinator
from v52.intelligence.inference import RuntimeInferenceEngine
from v52.intelligence.temporal_governance import TemporalGovernance
from v52.intelligence.regime_governance import RegimeGovernance
from v52.intelligence.observation import DynamicAssetUniverse
from v52.intelligence.observation import CanonicalObservation
from v52.intelligence.service import IntelligenceService
from v52.metrics import Metrics
from v52.intelligence.reference_market import DexContractMarketProvider, PublicChartMarketProvider
from v52.intelligence.store import BrainStore, SCHEMA_VERSION
from v52.storage.sqlite import connect


class _ExitChampion:
    def __init__(self, probability: float):
        self.probability = probability
        self.metadata = {"artifact_id": "fixture:exit:champion"}

    def predict(self, values):
        assert "realized_mfe" not in values and "realized_mae" not in values
        return self.probability


def _signal(settings, *, action="BUY", state="ACTIVE", horizon=60, first_price=100.0):
    brain = BrainStore(settings);DynamicAssetUniverse(settings);now = time.time();key = uuid.uuid4().hex
    with connect(brain.path, settings) as con:
        event_id = con.execute("""INSERT INTO events(cluster_key,event_type,domain,title,summary,occurred_at,first_observed_at,last_observed_at,importance,novelty,surprise,source_credibility,priority,state,extraction_method,extraction_version,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(key,"TEST","GENERAL","V55","fixture","x","x","x",.9,.9,0,.9,"P1","INITIAL","fixture","55",now,now)).lastrowid
        prediction_id = con.execute("""INSERT INTO predictions(prediction_key,event_id,asset_id,horizon_seconds,direction_probability,calibrated_confidence,expected_magnitude,uncertainty,expected_value,risk_score,action,mode,model_version_id,feature_version,feature_cutoff_at,evidence_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(f"p:{key}",event_id,"XAUUSD",horizon,.8,.8,.03,.1,.02,.2,action,"LIVE","fixture","features-55.0.0","x","{}",now)).lastrowid
        signal_id = con.execute("""INSERT INTO signals(signal_key,prediction_id,state,action,entry_logic,invalidation_logic,target_logic,exit_logic,opened_at,telegram_state,last_updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,'ELIGIBLE',?)""",(f"s:{key}",prediction_id,state,action,"now","risk","target","model",now,now)).lastrowid
        con.execute("INSERT INTO signal_lifecycle(signal_id,state,reason,market_context_json,created_at) VALUES(?,?,?,?,?)",(signal_id,state,"fixture","{}",now))
        con.execute("INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('fixture','XAUUSD',?,?,?,?,.9,'{}')",(now,now,first_price,100))
    return int(signal_id), now


def test_schema_v55_tracks_partial_position(settings):
    brain = BrainStore(settings)
    with connect(brain.path, settings) as con:
        columns = {str(x["name"]) for x in con.execute("PRAGMA table_info(signals)").fetchall()}
    assert SCHEMA_VERSION >= 5 and {"position_fraction", "partial_exit_count"} <= columns


def test_no_news_runtime_service_uses_exit_champion(settings, monkeypatch):
    signal_id, opened = _signal(settings)
    monitor = ActiveSignalMonitorService(settings);monitor.poll_seconds = .01;monitor.exit_confirmation_seconds = 0
    monkeypatch.setattr(monitor.registry, "champion", lambda expert: _ExitChampion(.76) if expert == "exit" else None)
    with connect(monitor.path, settings) as con:
        con.execute("INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('fixture','XAUUSD',?,?,?,?,.9,'{}')",(opened + 1, opened + 1, 96, 90))
    async def run_service():
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run(stop))
        await asyncio.sleep(.25);stop.set();await task
    asyncio.run(run_service())
    with connect(monitor.path, settings) as con:
        signal = con.execute("SELECT state FROM signals WHERE id=?",(signal_id,)).fetchone()
        snapshot = json.loads(con.execute("SELECT evidence_json FROM active_signal_snapshots WHERE signal_id=? ORDER BY id DESC LIMIT 1",(signal_id,)).fetchone()["evidence_json"])
    assert signal["state"] == "EXITED"
    assert snapshot["decision_source"] == "EXIT_CHAMPION" and snapshot["exit_artifact"] == "fixture:exit:champion"


def test_partial_exit_persists_remainder_and_returns_active(settings, monkeypatch):
    signal_id, opened = _signal(settings)
    monitor = ActiveSignalMonitorService(settings)
    monkeypatch.setattr(monitor.registry, "champion", lambda expert: _ExitChampion(.60) if expert == "exit" else None)
    with connect(monitor.path, settings) as con:
        con.execute("INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('fixture','XAUUSD',?,?,?,?,.9,'{}')",(opened + 1, opened + 1, 104, 100))
    monitor.cycle(opened + 2)
    with connect(monitor.path, settings) as con:
        row = con.execute("SELECT state,position_fraction,partial_exit_count FROM signals WHERE id=?",(signal_id,)).fetchone()
        states = [x["state"] for x in con.execute("SELECT state FROM signal_lifecycle WHERE signal_id=? ORDER BY id",(signal_id,)).fetchall()]
    assert dict(row) == {"state":"ACTIVE","position_fraction":.5,"partial_exit_count":1}
    assert "PARTIAL_EXIT" in states and states[-1] == "ACTIVE"


def test_short_post_exit_is_direction_normalized(settings):
    signal_id, opened = _signal(settings, action="SELL_SHORT", state="EXIT_PENDING", horizon=1)
    monitor = ActiveSignalMonitorService(settings);monitor.exit_confirmation_seconds = 0
    monitor.cycle(opened + .1)
    with connect(monitor.path, settings) as con:
        closed = float(con.execute("SELECT closed_at FROM signals WHERE id=?",(signal_id,)).fetchone()["closed_at"])
        con.execute("INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('fixture','XAUUSD',?,?,?,?,.9,'{}')",(closed + 1, closed + 1, 110, 100))
    monitor.cycle(closed + 2)
    with connect(monitor.path, settings) as con:post = con.execute("SELECT return_after_exit,evidence_json FROM post_exit WHERE signal_id=?",(signal_id,)).fetchone()
    assert float(post["return_after_exit"]) == pytest.approx(-.10)
    assert json.loads(post["evidence_json"])["direction_normalized"] is True


def test_exchange_collision_provider_mapping_and_llm_off_directory(settings):
    BrainStore(settings);universe = DynamicAssetUniverse(settings);symbol = "Q" + uuid.uuid4().hex[:7].upper()
    class Directory:
        def lookup(self, query):return {"ticker":symbol,"company":query,"exchange":"LSE","country":"GB","sector":"Technology"}
    first = universe.resolve_directory("Global Fixture", Directory())
    second = universe.discover({"ticker":symbol,"company":"Other Fixture","exchange":"TSX","country":"CA"})
    assert len(first) == len(second) == 1 and first[0] != second[0]
    assert universe.provider_symbol(first[0]) == f"{symbol}.L" and universe.provider_symbol(second[0]) == f"{symbol}.TO"
    assert PublicChartMarketProvider(settings)._symbol(second[0]) == f"{symbol}.TO"


def test_contract_dex_pair_quote_persistence(settings, monkeypatch):
    BrainStore(settings);universe = DynamicAssetUniverse(settings);asset_id = universe.discover({"token_symbol":"NEWX","token_name":"New X","chain":"base","contract":"0x987abc"})[0]
    provider = DexContractMarketProvider(settings)
    monkeypatch.setattr(provider,"_download",lambda _url:{"pairs":[{"chainId":"base","pairAddress":"0xpair","baseToken":{"address":"0x987abc"},"quoteToken":{"symbol":"USDC"},"priceUsd":"1.25","liquidity":{"usd":500000},"volume":{"h24":25000},"url":"https://dex/pair"}]})
    quote = provider.get_quote(asset_id, time.time())
    with connect(provider.brain.path, settings) as con:pair = con.execute("SELECT * FROM dex_pairs WHERE asset_id=?",(asset_id,)).fetchone()
    assert quote and quote.price == 1.25 and pair["pair_address"] == "0xpair" and pair["contract"] == "0x987abc"


def test_drift_actions_are_scoped_and_runtime_collector_has_no_fixtures(settings):
    BrainStore(settings);older,newer,scope = DriftDataCollector(settings).windows()
    assert set(DRIFT_TYPES) == set(DRIFT_ACTIONS)
    assert DRIFT_ACTIONS["SOURCE_DRIFT"] == "DEGRADE_AFFECTED_SOURCE_WEIGHT"
    assert scope == {"assets":[],"sources":[]} and older["regimes"] == newer["regimes"] == []


def test_failure_and_missed_rca_collect_persisted_evidence(settings):
    signal_id, _ = _signal(settings)
    engine = FailureAttributionEngine(settings)
    with connect(engine.path, settings) as con:prediction_id = int(con.execute("SELECT prediction_id FROM signals WHERE id=?",(signal_id,)).fetchone()["prediction_id"])
    evidence = engine.prediction_evidence(prediction_id)
    assert evidence["provider_available"] and evidence["provider_price_samples"] == 1, evidence
    assert engine.attribute(evidence)["evidence"]["prediction_id"] == prediction_id


def test_hypothesis_triple_is_an_actual_multi_fold_experiment(settings):
    brain = BrainStore(settings);now=time.time()
    with connect(brain.path, settings) as con:
        for index in range(80):
            event_id=con.execute("""INSERT INTO events(cluster_key,event_type,domain,title,summary,occurred_at,first_observed_at,last_observed_at,importance,novelty,surprise,source_credibility,priority,state,extraction_method,extraction_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(f"h{index}","TEST","GENERAL","h","h","x","x","x",.5,.5,0,.5,"P2","INITIAL","fixture","55",now+index,now+index)).lastrowid
            a=(index%7)/7;b=((index*3)%11)/11;c=((index*5)%13)/13;values={"new_a":a,"new_b":b,"new_c":c}
            fid=f"f{index}";con.execute("INSERT INTO feature_vectors(feature_id,event_id,asset_id,horizon_seconds,feature_version,prediction_time,available_cutoff,quality,values_json,provenance_json,replay,created_at,freshness_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(fid,event_id,"BTC",60,"features-55.0.0",now+index,now+index,.9,json.dumps(values),"[]",0,now+index,"{}"))
            target=con.execute("INSERT INTO response_targets(event_id,asset_id,horizon_seconds,event_epoch,due_at,state) VALUES(?,?,?,?,?,'complete')",(event_id,"BTC",60,now+index,now+index+60)).lastrowid
            label=.01*a*b*c+.001*(a+b+c);con.execute("""INSERT INTO market_responses(target_id,event_id,asset_id,horizon_seconds,start_price,end_price,return_value,volatility,mfe,mae,direction,reversal,continuation,time_to_peak_seconds,observed_samples,measured_at) VALUES(?,?,?,?,100,101,?,?,?,?,?,0,1,1,10,?)""",(target,event_id,"BTC",60,label,.01,max(label,0),min(label,0),"UP",now+index+60))
    result=FeatureInteractionHypothesisEngine(settings).experiment_triple("new_a","new_b","new_c",40,False)
    assert result["executed"] and result["purged_walk_forward"] and len(result["folds"]) >= 2
    assert result["features"] == ["new_a","new_b","new_c"]


def test_replay_cannot_write_production_predictions_signals_or_watchlist(settings, monkeypatch):
    monkeypatch.setenv("OPENAI_SEMANTICS_ENABLED","0");brain=BrainStore(settings);now=time.time()
    observation=CanonicalObservation("replay-v55",now,now,now,"fixture",.9,("XAUUSD",),(),"REAL_YIELD",1.2,"percent","DOWN",.9,0,.9,"fixture",{"payload":{"symbol":"XAUUSD","value":1.2}},True)
    async def execute():
        service=IntelligenceService(settings,Metrics("replay-v55",settings.state_dir))
        try:return await service.process_observation(observation)
        finally:await service.semantics.close()
    _event_id,signal_ids=asyncio.run(execute())
    with connect(brain.path,settings) as con:
        predictions=int(con.execute("SELECT COUNT(*) count FROM predictions").fetchone()["count"]);signals=int(con.execute("SELECT COUNT(*) count FROM signals").fetchone()["count"]);watch=int(con.execute("SELECT COUNT(*) count FROM active_asset_watchlist").fetchone()["count"]);replay_features=int(con.execute("SELECT COUNT(*) count FROM feature_vectors WHERE replay=1").fetchone()["count"])
    assert signal_ids==[] and predictions==signals==watch==0 and replay_features>0


def test_post_exit_outcomes_build_versioned_exit_shadow_dataset(settings, monkeypatch):
    monkeypatch.setenv("MODEL_MIN_TRAIN_ROWS","20");brain=BrainStore(settings);DynamicAssetUniverse(settings);base=time.time()
    with connect(brain.path,settings) as con:
        for index in range(28):
            at=base+index*10;key=f"exit-train-{index}";event_id=con.execute("""INSERT INTO events(cluster_key,event_type,domain,title,summary,occurred_at,first_observed_at,last_observed_at,importance,novelty,surprise,source_credibility,priority,state,extraction_method,extraction_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(key,"TEST","GENERAL","exit","fixture","x","x","x",.7,.7,0,.8,"P2","INITIAL","fixture","55",at,at)).lastrowid
            prediction_id=con.execute("""INSERT INTO predictions(prediction_key,event_id,asset_id,horizon_seconds,direction_probability,calibrated_confidence,expected_magnitude,uncertainty,expected_value,risk_score,action,mode,model_version_id,feature_version,feature_cutoff_at,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(f"p:{key}",event_id,"XAUUSD",60,.55+index%2*.2,.7,.02,.2,.01,.2,"BUY","SHADOW","fixture","features-55.0.0","x","{}",at)).lastrowid
            signal_id=con.execute("""INSERT INTO signals(signal_key,prediction_id,state,action,entry_logic,invalidation_logic,target_logic,exit_logic,opened_at,closed_at,telegram_state,last_updated_at) VALUES(?,?,'EXITED','BUY','now','risk','target','model',?,?,'SUPPRESSED',?)""",(f"s:{key}",prediction_id,at,at+1,at+1)).lastrowid
            exit_features={"prediction_probability":.55+index%2*.2,"predicted_magnitude":.02,"predicted_mfe":.03,"predicted_mae":.01,"current_volatility":.1+index%3*.1,"current_liquidity":.8,"event_age":1.0,"current_price_path":(-1 if index%2 else 1)*.01,"data_quality":.9}
            evidence=json.dumps({"exit_features":exit_features})
            con.execute("""INSERT INTO active_signal_snapshots(signal_id,observed_at,price,return_since_entry,mfe_so_far,mae_so_far,derivatives_json,onchain_json,liquidity,microstructure_json,volatility,regime,fundamental_json,remaining_ev,action,evidence_json) VALUES(?,?,100,0,.02,-.01,'{}','{}',.8,'{}',.2,'RANGE','{}',0,'EXIT',?)""",(signal_id,at+.5,evidence))
            classification="GOOD_EXIT" if index%2 else "MISSED_CONTINUATION"
            con.execute("INSERT INTO post_exit(signal_id,horizon_seconds,classification,return_after_exit,evidence_json,created_at,max_gain_after_exit,max_loss_after_exit) VALUES(?,?,?,?,?,?,?,?)",(signal_id,60,classification,-.02 if index%2 else .03,"{}",at+2,.01,-.02))
    result=TrainingCoordinator(settings).train_exit_from_post_exit()
    assert result and result.get("state")=="SHADOW"
    with connect(brain.path,settings) as con:
        artifact=con.execute("SELECT dataset_id,feature_version FROM model_artifacts WHERE expert='exit' AND state='SHADOW'").fetchone();dataset=con.execute("SELECT row_count,label_version FROM datasets WHERE dataset_id=?",(artifact["dataset_id"],)).fetchone()
    assert int(dataset["row_count"])==28 and str(artifact["feature_version"])=="active-exit-55.0.0"


def test_temporal_shadow_runs_resolves_and_receives_governed_decision(settings, monkeypatch):
    pytest.importorskip("torch");monkeypatch.setenv("TEMPORAL_GOVERNANCE_EPOCHS","1");monkeypatch.setenv("MODEL_MIN_SHADOW_OUTCOMES","10")
    brain=BrainStore(settings);DynamicAssetUniverse(settings);base=time.time()-1000
    training=[{"prediction_time":base+i,"available_cutoff":base+i,"label_available_at":base+i+.5,"feature_version":"features-55.0.0","values":{"x":(i%7)/7,"y":((i*3)%11)/11},"label":int(i%4>=2)} for i in range(80)]
    created=TemporalGovernance(settings).train(training);assert any(x.get("status")=="SHADOW" for x in created)
    engine=RuntimeInferenceEngine(settings)
    with connect(brain.path,settings) as con:
        fixtures=[]
        for i in range(10):
            at=base+2000+i*10;event_id=con.execute("""INSERT INTO events(cluster_key,event_type,domain,title,summary,occurred_at,first_observed_at,last_observed_at,importance,novelty,surprise,source_credibility,priority,state,extraction_method,extraction_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(f"temporal-{i}","INFLATION_RELEASE","MACRO","temporal","fixture","x","x","x",.8,.7,0,.8,"P1","INITIAL","fixture","55",at,at)).lastrowid
            fid=f"temporal-f-{i}";values={"x":(i%7)/7,"y":((i*3)%11)/11};con.execute("INSERT INTO feature_vectors(feature_id,event_id,asset_id,horizon_seconds,feature_version,prediction_time,available_cutoff,quality,values_json,provenance_json,replay,created_at,freshness_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(fid,event_id,"XAUUSD",60,"features-55.0.0",at,at,.9,json.dumps(values),"[]",0,at,"{}"))
            target=con.execute("INSERT INTO response_targets(event_id,asset_id,horizon_seconds,event_epoch,due_at,state) VALUES(?,?,?,?,?,'complete')",(event_id,"XAUUSD",60,at,at+60)).lastrowid;ret=.01 if i%2 else -.01;con.execute("""INSERT INTO market_responses(target_id,event_id,asset_id,horizon_seconds,start_price,end_price,return_value,volatility,mfe,mae,direction,reversal,continuation,time_to_peak_seconds,observed_samples,measured_at) VALUES(?,?,?,?,100,?,?,?,?,?,?,0,1,30,3,?)""",(target,event_id,"XAUUSD",60,100*(1+ret),ret,.01,max(ret,0),min(ret,0),"UP" if ret>0 else "DOWN",at+60));fixtures.append((event_id,fid,at,values))
    for event_id,fid,at,values in fixtures:
        event={"id":event_id,"event_type":"INFLATION_RELEASE","domain":"MACRO","importance":.8,"novelty":.7,"source_credibility":.8,"priced_in_score":.5,"historical_analogues":[]};asset={"asset_id":"XAUUSD","asset_class":"COMMODITY"};feature={"feature_id":fid,"feature_version":"features-55.0.0","prediction_time":at,"available_cutoff":at,"horizon_seconds":60,"quality":.9,"values":values}
        engine.infer(event,asset,60,feature,"SHADOW",[],True,(60,))
    coordinator=TrainingCoordinator(settings);resolved=coordinator.resolve_shadow_outcomes();decisions=coordinator.evaluate_shadows()
    assert resolved>=10 and decisions and all(x["expert"].startswith("temporal_meta:") for x in decisions)
    with connect(brain.path,settings) as con:states={x["state"] for x in con.execute("SELECT state FROM model_artifacts WHERE expert LIKE 'temporal_meta:%'").fetchall()}
    assert states <= {"CHAMPION","REJECTED"} and states


def test_trainable_regime_shadow_runtime_outcome_governance(settings, monkeypatch):
    monkeypatch.setenv("MODEL_MIN_SHADOW_OUTCOMES","10");brain=BrainStore(settings);DynamicAssetUniverse(settings);base=time.time()-2000
    feature_rows=[]
    with connect(brain.path,settings) as con:
        for i in range(50):
            at=base+i*10;event_id=con.execute("""INSERT INTO events(cluster_key,event_type,domain,title,summary,occurred_at,first_observed_at,last_observed_at,importance,novelty,surprise,source_credibility,priority,state,extraction_method,extraction_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(f"regime-{i}","TEST","GENERAL","regime","fixture","x","x","x",.7,.7,0,.8,"P2","INITIAL","fixture","55",at,at)).lastrowid
            fid=f"regime-f-{i}";values={"trend_score":1.0 if i%2 else -1.0,"volatility_score":.1+(i%3)*.1,"liquidity_score":.8};con.execute("INSERT INTO feature_vectors(feature_id,event_id,asset_id,horizon_seconds,feature_version,prediction_time,available_cutoff,quality,values_json,provenance_json,replay,created_at,freshness_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(fid,event_id,"XAUUSD",60,"features-55.0.0",at,at,.9,json.dumps(values),"[]",0,at,"{}"))
            regime="TRENDING_UP" if i%2 else "TRENDING_DOWN";con.execute("INSERT INTO regimes(asset_id,observed_at,regime,trend_score,volatility_score,change_score,liquidity_score,confidence,features_json) VALUES(?,?,?,?,?,?,?,?,?)",("XAUUSD",at+1,regime,values["trend_score"],values["volatility_score"],.1,.8,.8,"{}"))
            target=con.execute("INSERT INTO response_targets(event_id,asset_id,horizon_seconds,event_epoch,due_at,state) VALUES(?,?,?,?,?,'complete')",(event_id,"XAUUSD",60,at,at+60)).lastrowid;ret=.01 if i%2 else -.01;con.execute("""INSERT INTO market_responses(target_id,event_id,asset_id,horizon_seconds,start_price,end_price,return_value,volatility,mfe,mae,direction,reversal,continuation,time_to_peak_seconds,observed_samples,measured_at) VALUES(?,?,?,?,100,?,?,?,?,?,?,0,1,30,3,?)""",(target,event_id,"XAUUSD",60,100*(1+ret),ret,.01,max(ret,0),min(ret,0),"UP" if ret>0 else "DOWN",at+60));feature_rows.append((fid,at,values))
    governance=RegimeGovernance(settings);created=governance.train(40);assert any(x.get("state")=="SHADOW" for x in created)
    for fid,at,values in feature_rows[-10:]:governance.infer(fid,values,at)
    coordinator=TrainingCoordinator(settings);assert coordinator.resolve_shadow_outcomes()>=10;decisions=coordinator.evaluate_shadows()
    assert decisions and all(x["expert"].startswith("regime_classifier:") for x in decisions)
