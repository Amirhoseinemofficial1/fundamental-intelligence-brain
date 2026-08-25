from __future__ import annotations
import asyncio,json,time
import pytest
from v52.intelligence.features import FeatureStore
from v52.intelligence.inference import RuntimeInferenceEngine
from v52.intelligence.model_factory import ArtifactRegistry,ModelFactory
from v52.intelligence.learning import TrainingCoordinator
from v52.intelligence.observation import CanonicalObservation,ObservationBridge
from v52.intelligence.provider_manifest import validate_manifest
from v52.intelligence.reference_market import BrainReferenceMarketProvider
from v52.intelligence.store import BrainStore
from v52.intelligence.warmup import RuntimeWarmup
from v52.storage.sqlite import connect

def rows(reverse:bool=False,key:str='driver',count:int=40):
    out=[]
    for i in range(count):
        x=-1+2*i/(count-1);label=int(x>0)
        if reverse:label=1-label
        out.append({'prediction_time':1000+i,'values':{key:x,'data_quality':.9},'label':label,'feature_version':'features-52.true.1'})
    return out
def champion(settings,artifact_id:str,expert:str):
    with connect(settings.data_dir/'brain'/'intelligence.sqlite3',settings) as con:
        con.execute("UPDATE model_artifacts SET state='RETIRED' WHERE expert=? AND state='CHAMPION'",(expert,))
        con.execute("UPDATE model_artifacts SET state='CHAMPION',promoted_at=? WHERE artifact_id=?",(time.time(),artifact_id))

@pytest.mark.parametrize('expert',('gold','oil','forex','equity','crypto','token'))
def test_six_asset_experts_train_serialize_load_infer(settings,expert):
    feature_name={'gold':'real_yield','oil':'inventory','forex':'rate_differential','equity':'earnings','crypto':'funding_rate','token':'net_exchange_flow'}[expert]
    BrainStore(settings);result=ModelFactory(settings).train(expert,rows(key=feature_name),state='SHADOW')
    artifact=ArtifactRegistry(settings).load(result['artifact_id'])
    low=artifact.predict({feature_name:-.9,'data_quality':.9});high=artifact.predict({feature_name:.9,'data_quality':.9})
    assert result['metrics']['validation_samples']>0 and result['dataset_id'] and low!=high

def test_gold_champion_artifact_actually_changes_runtime_prediction(settings):
    BrainStore(settings);factory=ModelFactory(settings)
    a=factory.train('gold',rows(False,key='real_yield'),state='SHADOW');b=factory.train('gold',rows(True,key='real_yield'),state='SHADOW')
    event={'id':None,'domain':'MACRO','event_type':'INFLATION_RELEASE','importance':.9,'novelty':.8,'source_credibility':.9}
    asset={'asset_id':'XAUUSD','asset_class':'COMMODITY','directional_bias':'UP','mapping_confidence':.9}
    feature={'feature_id':'f-artifact-test','prediction_time':2000.0,'quality':.9,'values':{'real_yield':.9,'data_quality':.9}}
    with connect(settings.data_dir/'brain'/'intelligence.sqlite3',settings) as con:
        con.execute("INSERT INTO feature_vectors(feature_id,event_id,asset_id,horizon_seconds,feature_version,prediction_time,available_cutoff,quality,values_json,provenance_json,replay,created_at) VALUES(?,NULL,'XAUUSD',300,'features-52.true.1',2000,2000,.9,?,'[]',0,2000)",(feature['feature_id'],json.dumps(feature['values'])))
    champion(settings,a['artifact_id'],'gold');p1=RuntimeInferenceEngine(settings).infer(event,asset,300,feature,'SHADOW',[],False)[0][0].direction_probability
    champion(settings,b['artifact_id'],'gold');p2=RuntimeInferenceEngine(settings).infer(event,asset,300,feature,'SHADOW',[],False)[0][0].direction_probability
    assert abs(p1-p2)>.15

def test_meta_champion_actually_controls_final_runtime_probability(settings):
    BrainStore(settings);factory=ModelFactory(settings)
    gold=factory.train('gold',rows(False,key='real_yield'),state='SHADOW');champion(settings,gold['artifact_id'],'gold')
    meta_a=factory.train('meta',rows(False,key='expert_gold'),state='SHADOW');meta_b=factory.train('meta',rows(True,key='expert_gold'),state='SHADOW')
    event={'id':None,'domain':'MACRO','event_type':'INFLATION_RELEASE','importance':.9,'novelty':.8,'source_credibility':.9}
    asset={'asset_id':'XAUUSD','asset_class':'COMMODITY','directional_bias':'UP','mapping_confidence':.9}
    feature={'feature_id':'f-meta-test','prediction_time':3000.0,'quality':.9,'values':{'real_yield':.9,'data_quality':.9}}
    with connect(settings.data_dir/'brain'/'intelligence.sqlite3',settings) as con:con.execute("INSERT INTO feature_vectors(feature_id,event_id,asset_id,horizon_seconds,feature_version,prediction_time,available_cutoff,quality,values_json,provenance_json,replay,created_at) VALUES(?,NULL,'XAUUSD',300,'features-52.true.1',3000,3000,.9,?,'[]',0,3000)",(feature['feature_id'],json.dumps(feature['values'])))
    champion(settings,meta_a['artifact_id'],'meta');p1=RuntimeInferenceEngine(settings).infer(event,asset,300,feature,'SHADOW',[],False)[1].probability
    champion(settings,meta_b['artifact_id'],'meta');p2=RuntimeInferenceEngine(settings).infer(event,asset,300,feature,'SHADOW',[],False)[1].probability
    assert abs(p1-p2)>.15

def test_available_at_contract_and_structured_feature_changes(settings):
    BrainStore(settings);bridge=ObservationBridge(settings);store=FeatureStore(settings)
    event={'id':None,'importance':.8,'novelty':.7,'source_credibility':.9,'surprise':0};asset={'asset_id':'XAUUSD','mapping_confidence':.9}
    future=CanonicalObservation('future',100,200,210,'macro',.9,('XAUUSD',),(),'REAL_YIELD',4.5,'percent','UP',.9,110,.9,'fixture',{})
    bridge.persist_sync(future)
    before=store.build_sync(event,asset,300,prediction_time=150);after=store.build_sync(event,asset,300,prediction_time=250)
    assert before['values']['real_yield']==0 and after['values']['real_yield']==4.5
    assert 'future' not in before['provenance'] and 'future' in after['provenance']
    model=ModelFactory(settings).train('gold',rows(False,key='real_yield'),state='SHADOW');champion(settings,model['artifact_id'],'gold')
    runtime=RuntimeInferenceEngine(settings);model_event={**event,'domain':'MACRO','event_type':'INFLATION_RELEASE'};model_asset={**asset,'asset_class':'COMMODITY','directional_bias':'UP'}
    p_before=runtime.infer(model_event,model_asset,300,before,'SHADOW',[],False)[0][0].direction_probability
    p_after=runtime.infer(model_event,model_asset,300,after,'SHADOW',[],False)[0][0].direction_probability
    assert abs(p_before-p_after)>.05

def test_noncrypto_reference_market_resolves_history(settings):
    BrainStore(settings);path=settings.data_dir/'brain'/'intelligence.sqlite3'
    with connect(path,settings) as con:
        for i,p in enumerate((2400,2405,2410)):con.execute("INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('fixture','XAUUSD',?,?,?,?,.9,'{}')",(100+i*60,100+i*60,p,1000+i))
    history=BrainReferenceMarketProvider(settings).get_history('XAUUSD',100,220)
    assert [x.price for x in history]==[2400,2405,2410]

def test_runtime_warmup_ignores_downtime_and_replay(settings):
    BrainStore(settings);warm=RuntimeWarmup(settings,max_tick_gap=60)
    with connect(settings.data_dir/'brain'/'intelligence.sqlite3',settings) as con:con.execute("UPDATE metadata SET value='1000' WHERE key='last_runtime_tick'")
    first=warm.tick_sync(2800);after_downtime=warm.tick_sync(2800+3*86400);after_replay=warm.tick_sync(2800+3*86400+100,replay=True)
    assert first['observation_runtime_seconds']==60
    assert after_downtime['observation_runtime_seconds']==120
    assert after_replay['observation_runtime_seconds']==120

def test_enabled_provider_manifest_has_real_adapters(settings):
    result=validate_manifest(settings)
    assert result['ok'] and result['providers']>=80 and result['invalid_enabled']==[]

def test_true_shadow_sees_champion_stream_and_promotes_on_unseen_outcomes(settings,monkeypatch):
    monkeypatch.setenv('MODEL_MIN_SHADOW_OUTCOMES','10');brain=BrainStore(settings);factory=ModelFactory(settings)
    current=factory.train('gold',rows(False,key='real_yield'),state='SHADOW');challenger=factory.train('gold',rows(True,key='real_yield'),state='SHADOW');champion(settings,current['artifact_id'],'gold')
    path=brain.path;runtime=RuntimeInferenceEngine(settings);event_template={'domain':'MACRO','event_type':'INFLATION_RELEASE','importance':.9,'novelty':.8,'source_credibility':.9}
    asset={'asset_id':'XAUUSD','asset_class':'COMMODITY','directional_bias':'UP','mapping_confidence':.9}
    with connect(path,settings) as con:con.execute("UPDATE model_artifacts SET state='SHADOW' WHERE artifact_id=?",(challenger['artifact_id'],))
    for i in range(12):
        x=-.9+1.8*i/11;now=5000+i
        with connect(path,settings) as con:
            cur=con.execute("""INSERT INTO events(cluster_key,event_type,domain,title,summary,occurred_at,first_observed_at,last_observed_at,importance,novelty,surprise,source_credibility,priority,state,extraction_method,extraction_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(f'shadow-{i}','INFLATION_RELEASE','MACRO',f'event {i}','fixture','x','x','x',.9,.8,0,.9,'P1','INITIAL','fixture','1',now,now));event_id=int(cur.lastrowid)
            feature_id=f'shadow-feature-{i}';values={'real_yield':x,'data_quality':.9}
            con.execute("INSERT INTO feature_vectors(feature_id,event_id,asset_id,horizon_seconds,feature_version,prediction_time,available_cutoff,quality,values_json,provenance_json,replay,created_at) VALUES(?,?,?,?,?,?,?,?,?,'[]',0,?)",(feature_id,event_id,'XAUUSD',300,'features-52.true.1',now,now,.9,json.dumps(values),now))
        feature={'feature_id':feature_id,'prediction_time':now,'quality':.9,'values':values};event={**event_template,'id':event_id}
        runtime.infer(event,asset,300,feature,'SHADOW',[],False)
        label=int(x<=0);ret=.01 if label else -.01
        with connect(path,settings) as con:
            target=con.execute("INSERT INTO response_targets(event_id,asset_id,horizon_seconds,event_epoch,due_at,state) VALUES(?,?,?,?,?,'complete')",(event_id,'XAUUSD',300,now,now+300)).lastrowid
            con.execute("""INSERT INTO market_responses(target_id,event_id,asset_id,horizon_seconds,start_price,end_price,return_value,volatility,volume,mfe,mae,direction,reversal,continuation,time_to_peak_seconds,observed_samples,measured_at) VALUES(?,?,?,?,100,?,?,?,?,?,?,?,?,?,?,?,?)""",(target,event_id,'XAUUSD',300,100*(1+ret),ret,.01,1000,max(ret,0),min(ret,0),'UP' if ret>0 else 'DOWN',0,1,300,2,now+300))
    coordinator=TrainingCoordinator(settings);assert coordinator.resolve_shadow_outcomes()>=12
    result=next(x for x in coordinator.evaluate_shadows() if x['artifact_id']==challenger['artifact_id'])
    assert result['promoted'] and ArtifactRegistry(settings).champion('gold').metadata['artifact_id']==challenger['artifact_id']
