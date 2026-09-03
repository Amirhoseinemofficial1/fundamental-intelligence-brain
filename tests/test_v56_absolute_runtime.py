from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from v52.config import Settings
from v52.intelligence.active_signal_monitor import ActiveSignalMonitorService
from v52.intelligence.drift import DriftEngine
from v52.intelligence.learning import TrainingCoordinator
from v52.intelligence.market_response import MarketResponseService
from v52.intelligence.observation import DynamicAssetUniverse
from v52.intelligence.reference_market import ReferenceQuote
from v52.intelligence.service import IntelligenceService
from v52.intelligence.store import BrainStore
from v52.metrics import Metrics
from v52.storage.sqlite import connect


def _event_prediction_signal(settings, asset_id="XAUUSD", horizon=60):
    brain=BrainStore(settings);DynamicAssetUniverse(settings);now=time.time();key=uuid.uuid4().hex
    with connect(brain.path,settings) as con:
        event_id=con.execute("""INSERT INTO events(cluster_key,event_type,domain,title,summary,occurred_at,first_observed_at,last_observed_at,importance,novelty,surprise,source_credibility,priority,state,extraction_method,extraction_version,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(key,"TEST","GENERAL","active","fixture","x","x","x",.8,.8,0,.8,"P1","INITIAL","fixture","56",now,now)).lastrowid
        con.execute("INSERT OR IGNORE INTO event_assets(event_id,asset_id,impact_order,directional_bias,mapping_confidence,mapping_reason) VALUES(?,?,1,'MIXED',.9,'fixture')",(event_id,asset_id))
        prediction_id=con.execute("""INSERT INTO predictions(prediction_key,event_id,asset_id,horizon_seconds,direction_probability,calibrated_confidence,expected_magnitude,uncertainty,expected_value,risk_score,action,mode,model_version_id,feature_version,feature_cutoff_at,evidence_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(f"p:{key}",event_id,asset_id,horizon,.75,.75,.03,.2,.01,.2,"BUY","SHADOW","fixture","features-56.0.0","x","{}",now)).lastrowid
        signal_id=con.execute("""INSERT INTO signals(signal_key,prediction_id,state,action,entry_logic,invalidation_logic,target_logic,exit_logic,opened_at,telegram_state,last_updated_at)
            VALUES(?,?,'ACTIVE','BUY','IMMEDIATE: fixture','risk','target','model',?,'SUPPRESSED',?)""",(f"s:{key}",prediction_id,now,now)).lastrowid
        con.execute("INSERT INTO signal_lifecycle(signal_id,state,reason,market_context_json,created_at) VALUES(?,'ACTIVE','fixture','{}',?)",(signal_id,now))
        con.execute("INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('fixture',?,?,?,?,100,.9,'{}')",(asset_id,now,now,100.0))
    return brain,int(signal_id),int(event_id),now


class _FeatureSensitiveExit:
    def __init__(self):self.received=[];self.metadata={"artifact_id":"fixture:v56:exit-policy","expert_name":"exit_policy"}
    def predict(self,values):
        self.received.append(dict(values))
        adverse=(values.get('funding_rate')==-.04 and values.get('open_interest_change')==-12.0 and values.get('whale_flow')==-7.0 and values.get('regime_transition_probability')==.91)
        return .78 if adverse else .1


def test_active_service_fresh_state_reaches_exit_champion_and_changes_decision(settings,monkeypatch):
    brain,signal_id,_event_id,opened=_event_prediction_signal(settings);model=_FeatureSensitiveExit()
    monitor=ActiveSignalMonitorService(settings);monitor.poll_seconds=.01;monitor.exit_confirmation_seconds=0
    monkeypatch.setattr(monitor.registry,'champion',lambda name:model if name=='exit_policy' else None)
    now=opened+.01
    with connect(brain.path,settings) as con:
        for index,(name,value) in enumerate((('FUNDING_RATE',-.04),('OI_CHANGE',-12.0),('SMART_MONEY_FLOW',-7.0),('DEX_LIQUIDITY',.1))):
            con.execute("""INSERT INTO canonical_observations(observation_id,event_time,available_time,ingestion_time,source_id,source_quality,asset_ids_json,entity_ids_json,observation_type,value,unit,direction,confidence,freshness_seconds,quality,raw_reference,metadata_json,replay,provider_id,market,observation_family)
                VALUES(?,?,?,?,'.fixture',.9,'[\"XAUUSD\"]','[]',?,?, '', 'DOWN',.9,0,.9,'fixture','{}',0,'.fixture','XAUUSD','DERIVATIVES')""",(f"fresh-{index}",now,now,now,name,value))
        con.execute("INSERT INTO regimes(asset_id,observed_at,regime,trend_score,volatility_score,change_score,liquidity_score,confidence,features_json) VALUES('XAUUSD',?,'REGIME_TRANSITION',-.8,.9,.95,.1,.9,?)",(now,json.dumps({'transition_probability':.91,'uncertainty':.2,'probabilities':{'RISK_OFF':.9}})))
        con.execute("INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('fixture','XAUUSD',?,?,?,?,.9,'{}')",(now,now,96.0,80))
    async def scenario():
        stop=asyncio.Event();task=asyncio.create_task(monitor.run(stop));await asyncio.sleep(.18);stop.set();await task
    asyncio.run(scenario())
    assert model.received
    actual=next((item for item in model.received if item.get('funding_rate')==-.04),model.received[-1])
    assert actual['funding_rate']==-.04 and actual['open_interest_change']==-12.0 and actual['whale_flow']==-7.0 and actual['regime_transition_probability']==.91
    with connect(brain.path,settings) as con:
        signal=con.execute("SELECT state FROM signals WHERE id=?",(signal_id,)).fetchone();snapshot=con.execute("SELECT feature_id,inference_json FROM active_signal_snapshots WHERE signal_id=? ORDER BY id DESC LIMIT 1",(signal_id,)).fetchone()
    assert signal['state']=='EXITED' and snapshot['feature_id'] and json.loads(snapshot['inference_json'])['updated_probability']


class _Directory:
    def __init__(self,candidate):self.candidate=candidate;self.calls=[]
    def lookup(self,query):self.calls.append(query);return [self.candidate]


def test_unknown_equity_and_token_are_discovered_by_main_service_without_llm(settings,monkeypatch):
    monkeypatch.setenv('OPENAI_SEMANTICS_ENABLED','0')
    equity=_Directory({'ticker':'QZT','company':'Company QZeta','exchange':'NASDAQ','provider_symbols':{'public_chart':'QZT'},'aliases':['QZeta']})
    token=_Directory({'token_symbol':'NBL','token_name':'Nebula Protocol','chain':'base','contract':'0x991122','market_pairs':['0xpool']})
    async def scenario():
        service=IntelligenceService(settings,Metrics('v56-directory',settings.state_dir));service.equity_directory=equity;service.token_directory=token
        try:
            event1,_=await service.process_story({'id':901,'fingerprint':'qzeta','title':'Company QZeta unexpectedly raises guidance','summary':'guidance increase','category':'COMPANY','materiality':7,'source_name':'fixture','source_url':'fixture:q','published_at':'2026-08-29T00:00:00Z','first_observed_at':'2026-08-29T00:00:01Z'})
            event2,_=await service.process_story({'id':902,'fingerprint':'nebula','title':'Nebula Protocol token launches on DEX','summary':'new token pool','category':'TOKEN','materiality':7,'source_name':'fixture','source_url':'fixture:n','published_at':'2026-08-29T00:01:00Z','first_observed_at':'2026-08-29T00:01:01Z'})
            return event1,event2,service.brain.path
        finally:await service.semantics.close()
    event1,event2,path=asyncio.run(scenario())
    with connect(path,settings) as con:
        first={r['asset_id'] for r in con.execute('SELECT asset_id FROM event_assets WHERE event_id=?',(event1,)).fetchall()};second={r['asset_id'] for r in con.execute('SELECT asset_id FROM event_assets WHERE event_id=?',(event2,)).fetchall()}
    assert 'equity:XNAS:QZT' in first and 'base:0x991122' in second and equity.calls and token.calls


def test_exchange_identity_collision_and_provider_symbols(settings):
    BrainStore(settings);universe=DynamicAssetUniverse(settings);ids=[]
    fixtures=(('NASDAQ','QABC'),('ASX','QABC.AX'),('LSE','QABC.L'))
    for exchange,provider_symbol in fixtures:
        ids.extend(universe.discover({'ticker':'QABC','company':f'Issuer {exchange}','exchange':exchange,'provider_symbols':{'public_chart':provider_symbol}}))
    assert set(ids)=={'equity:XNAS:QABC','equity:XASX:QABC','equity:XLON:QABC'}
    assert universe.provider_symbol('equity:XASX:QABC')=='QABC.AX'


class _FixtureDex:
    def __init__(self,settings,asset,event_at,due_at):self.settings=settings;self.path=BrainStore(settings).path;self.asset=asset;self.event_at=event_at;self.due_at=due_at;self.polls=0
    def get_quote(self,asset_id,at=None):
        self.polls+=1
        with connect(self.path,self.settings) as con:
            for epoch,price in ((self.event_at,1.0),(self.event_at+30,1.1),(self.due_at,1.2)):
                con.execute("INSERT OR IGNORE INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('fixture_dex',?,?,?,?,100,.9,'{}')",(asset_id,epoch,epoch,price))
        return ReferenceQuote(asset_id,self.due_at,self.due_at,1.2,100,'fixture_dex',.9)
    def get_history(self,asset_id,start,end,limit=5000):
        with connect(self.path,self.settings) as con:rows=con.execute('SELECT * FROM reference_prices WHERE asset_id=? AND observed_at>=? AND observed_at<=? ORDER BY observed_at',(asset_id,start,end)).fetchall()
        return [ReferenceQuote(r['asset_id'],r['observed_at'],r['available_at'],r['price'],r['volume'],r['provider'],r['quality']) for r in rows]
    def get_ohlcv(self,*args,**kwargs):return self.get_history(*args,**kwargs)
    def get_volume(self,asset_id,at=None):return 100
    def get_orderbook(self,asset_id):return None
    def health(self):return {'ok':True}


def test_pending_dex_target_still_polls_future_quotes_resolves_and_reaches_learning(settings,monkeypatch):
    monkeypatch.setenv('ACTIVE_ASSET_POLL_SECONDS','1');brain=BrainStore(settings);universe=DynamicAssetUniverse(settings);asset=universe.discover({'token_symbol':'DXQ','token_name':'DEX Q','chain':'base','contract':'0xdedede','market_pairs':['0xpool']})[0]
    now=time.time();event_at=now-61;due=now-1
    with connect(brain.path,settings) as con:
        event_id=con.execute("""INSERT INTO events(cluster_key,event_type,domain,title,summary,occurred_at,first_observed_at,last_observed_at,importance,novelty,surprise,source_credibility,priority,state,extraction_method,extraction_version,created_at,updated_at) VALUES('dex-v56','TOKEN_LISTING','CRYPTO','dex','dex','x','x','x',.8,.8,0,.8,'P1','INITIAL','fixture','56',?,?)""",(event_at,event_at)).lastrowid
        con.execute("INSERT INTO event_assets VALUES(?,?,1,'UP',.9,'fixture')",(event_id,asset))
        target_id=con.execute("INSERT INTO response_targets(event_id,asset_id,horizon_seconds,event_epoch,due_at,state) VALUES(?,?,?,?,?,'pending')",(event_id,asset,60,event_at,due)).lastrowid
        con.execute("INSERT INTO feature_vectors(feature_id,event_id,asset_id,horizon_seconds,feature_version,prediction_time,available_cutoff,quality,values_json,provenance_json,replay,created_at,freshness_json) VALUES('dex-feature',?,?,60,'features-56.0.0',?,?,.9,'{\"funding\":0.0}','[]',0,?,'{}')",(event_id,asset,event_at,event_at,event_at))
    service=MarketResponseService(settings,Metrics('v56-dex',settings.state_dir));fixture=_FixtureDex(settings,asset,event_at,due);service.reader.reference=fixture;service.watchlist_provider=fixture;service.watchlist.activate(asset,'fixture',event_id)
    async def scenario():
        stop=asyncio.Event();task=asyncio.create_task(service.run(stop));await asyncio.sleep(.2);stop.set();await task
    asyncio.run(scenario())
    with connect(brain.path,settings) as con:response=con.execute('SELECT * FROM market_responses WHERE target_id=?',(target_id,)).fetchone()
    rows=TrainingCoordinator(settings).learning_rows()
    assert fixture.polls>=1 and response and int(response['observed_samples'])>=3 and any(r['feature_id']=='dex-feature' for r in rows)


def test_exit_quality_shadow_resolves_named_post_exit_label(settings):
    brain,signal_id,event_id,opened=_event_prediction_signal(settings)
    with connect(brain.path,settings) as con:
        con.execute("INSERT INTO feature_vectors(feature_id,event_id,asset_id,horizon_seconds,feature_version,prediction_time,available_cutoff,quality,values_json,provenance_json,replay,created_at,freshness_json) VALUES('exit-v56-f',?,'XAUUSD',60,'active-exit-56.0.0',?,?,.9,'{\"x\":1}','[]',0,?,'{}')",(event_id,opened,opened,opened))
        con.execute("INSERT INTO datasets VALUES('exit-v56-d','exit_quality','*',0,?,?,1,'sha','active-exit-56.0.0','exit-quality-enum-v1','{}',?)",(opened,opened,opened))
        con.execute("""INSERT INTO model_artifacts(artifact_id,expert,model_family,state,artifact_path,artifact_sha256,dataset_id,feature_version,code_version,seed,training_start,training_end,validation_start,validation_end,hyperparameters_json,metrics_json,calibration_json,created_at)
            VALUES('exit-v56-a','exit_quality','fixture','SHADOW','unused','sha','exit-v56-d','active-exit-56.0.0','56',56,?,?,?,?, '{}','{}','{}',?)""",(opened,opened,opened,opened,opened))
        con.execute("INSERT INTO shadow_predictions(feature_id,artifact_id,probability,prediction_time,signal_id,target_type) VALUES('exit-v56-f','exit-v56-a',.8,?,?,'exit_quality')",(opened,signal_id))
        con.execute("INSERT INTO post_exit(signal_id,horizon_seconds,classification,return_after_exit,evidence_json,created_at,max_gain_after_exit,max_loss_after_exit) VALUES(?,60,'GOOD_EXIT',-.02,'{}',?,.01,-.02)",(signal_id,opened+60))
    assert TrainingCoordinator(settings).resolve_shadow_outcomes()==1
    with connect(brain.path,settings) as con:row=con.execute("SELECT label,label_text,outcome FROM shadow_predictions WHERE artifact_id='exit-v56-a'").fetchone()
    assert row['label']==1 and row['label_text']=='GOOD_EXIT' and row['outcome']==-.02


def test_drift_action_dispatch_changes_affected_source(settings):
    brain=BrainStore(settings);engine=DriftEngine(settings);now=time.time()
    with connect(brain.path,settings) as con:con.execute("INSERT INTO source_profiles(source_id,observations,reliability,updated_at) VALUES('drift-source',10,.8,?)",(now,))
    base={'data':[1,1],'features':{'funding':[0,0]},'labels':[0,1],'predictions':[.2,.8],'source_quality':[.9,.9],'source_latency':[1,1],'regimes':['A','A'],'asset_returns':[0,0]}
    recent={**base,'source_quality':[.1,.1]}
    result=engine.evaluate(base,recent,True,{'sources':['drift-source'],'assets':[]});actions=engine.execute_actions(result)
    with connect(brain.path,settings) as con:reliability=con.execute("SELECT reliability FROM source_profiles WHERE source_id='drift-source'").fetchone()['reliability'];persisted=con.execute("SELECT execution_status FROM drift_actions WHERE drift_type='SOURCE_DRIFT'").fetchone()
    assert any(x['drift_type']=='SOURCE_DRIFT' for x in actions) and reliability<.8 and persisted['execution_status']=='COMPLETED'


def test_python_normal_entry_loads_dotenv(settings,tmp_path,monkeypatch):
    env_file=tmp_path/'.env';env_file.write_text("TEST_V56_ENV_VALUE=known-value\n",encoding='utf-8')
    monkeypatch.delenv('TEST_V56_ENV_VALUE',raising=False);monkeypatch.setenv('V56_ENV_FILE',str(env_file))
    Settings.from_env()
    assert os.environ['TEST_V56_ENV_VALUE']=='known-value'
