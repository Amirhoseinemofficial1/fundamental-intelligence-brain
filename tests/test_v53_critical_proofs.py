from __future__ import annotations

import asyncio
import json
import math
import time

import pytest

from v52.intelligence.active_signal_agent import ActiveSignalAgent
from v52.intelligence.decision_models import DecisionModelEngine
from v52.intelligence.experts import detect_regime
from v52.intelligence.features import FeatureStore
from v52.intelligence.hypothesis_engine import FeatureInteractionHypothesisEngine
from v52.intelligence.inference import RuntimeInferenceEngine
from v52.intelligence.model_factory import ModelFactory
from v52.intelligence.observation import DynamicAssetUniverse
from v52.intelligence.reference_market import ActiveAssetWatchlist,PublicChartMarketProvider
from v52.intelligence.research import ResearchService
from v52.intelligence.replay import BacktestEngine
from v52.intelligence.semantic_routing import route_observation
from v52.intelligence.signal_consolidation import SignalConsolidator
from v52.intelligence.service import IntelligenceService
from v52.intelligence.store import BrainStore
from v52.intelligence.telegram_producers import FINAL_INTELLIGENCE_KINDS,TelegramIntelligenceProducers
from v52.intelligence.temporal_models import TemporalModelFactory
from v52.intelligence.types import ExpertVote,SignalDecision
from v52.metrics import Metrics
from v52.storage.control import ControlStore
from v52.storage.sqlite import connect


def _rows(key:str,regression:bool=False,reverse:bool=False,count:int=40):
    result=[]
    for index in range(count):
        x=index/(count-1);label=(.002+.04*x) if regression else int(x>.5)
        if reverse:label=(.042-.04*x) if regression else 1-label
        result.append({'prediction_time':1000+index,'feature_version':'features-53.0.0','values':{key:x},'label':label})
    return result


def _champion(settings,result,expert):
    with connect(settings.data_dir/'brain'/'intelligence.sqlite3',settings) as con:
        con.execute("UPDATE model_artifacts SET state='RETIRED' WHERE expert=? AND state='CHAMPION'",(expert,))
        con.execute("UPDATE model_artifacts SET state='CHAMPION',promoted_at=? WHERE artifact_id=?",(time.time(),result['artifact_id']))


def _feature(settings,feature_id,values,horizon=300):
    with connect(settings.data_dir/'brain'/'intelligence.sqlite3',settings) as con:
        con.execute("INSERT INTO feature_vectors(feature_id,event_id,asset_id,horizon_seconds,feature_version,prediction_time,available_cutoff,quality,values_json,provenance_json,replay,created_at) VALUES(?,NULL,'XAUUSD',?,'features-53.0.0',2000,2000,.9,?,'[]',0,2000)",(feature_id,horizon,json.dumps(values)))
    return {'feature_id':feature_id,'feature_version':'features-53.0.0','prediction_time':2000.0,'available_cutoff':2000.0,'horizon_seconds':horizon,'quality':.9,'values':values,'provenance':[],'replay':False}


@pytest.mark.parametrize('record_type,expected',[
    ('CPI',{'macro','central_bank','gold'}),('REAL_YIELD',{'macro','central_bank','gold'}),('FED_RATE',{'central_bank','forex'}),('NFP',{'macro','forex'}),
    ('OPEC',{'oil','energy'}),('EIA_INVENTORY',{'oil','energy'}),('EARNINGS',{'equity'}),('SEC_FILING',{'equity','regulation'}),
    ('OPEN_INTEREST',{'derivatives','crypto'}),('FUNDING',{'derivatives','liquidity'}),('LIQUIDATION',{'derivatives','dump_risk'}),
    ('WHALE_TRANSFER',{'onchain','whale'}),('EXCHANGE_FLOW',{'onchain','liquidity'}),('DEX_LIQUIDITY',{'liquidity','token'}),
    ('TOKEN_UNLOCK',{'token','dump_risk'}),('LISTING',{'listing','pump_risk'}),('EXPLOIT',{'token','dump_risk'}),
])
def test_semantic_routing_triggers_required_experts(record_type,expected):
    route=route_observation('fixture-provider','structured',record_type)
    assert expected.issubset(set(route.experts)) and route.domain!='GENERAL'


def test_dynamic_sui_and_pltr_resolution_without_proxy(settings):
    BrainStore(settings);universe=DynamicAssetUniverse(settings)
    assert universe.discover({'headline':'Coinbase will list SUI'})==('SUI',)
    assert universe.discover({'headline':'Palantir earnings beat expectations'})==('equity:XNAS:PLTR',)


def test_independent_magnitude_and_horizon_champions_change_outputs(settings):
    BrainStore(settings);factory=ModelFactory(settings)
    magnitude=factory.train_regression('magnitude_return',_rows('driver',True));_champion(settings,magnitude,'magnitude_return')
    fast=factory.train_regression('horizon_score:60',_rows('driver',True));slow=factory.train_regression('horizon_score:3600',_rows('driver',True,True));_champion(settings,fast,'horizon_score:60');_champion(settings,slow,'horizon_score:3600')
    engine=DecisionModelEngine(settings);low=engine.forecast(_feature(settings,'decision-low',{'driver':.1}),[60,3600]);high=engine.forecast(_feature(settings,'decision-high',{'driver':.9}),[60,3600])
    assert high.expected_return>low.expected_return
    assert low.preferred_horizon==3600 and high.preferred_horizon==60
    assert all(source=='TRAINED_CHAMPION' for source in (low.sources['magnitude_return'],low.sources['horizon:60'],low.sources['horizon:3600']))


def test_asset_profile_is_in_feature_vector_and_changes_artifact_prediction(settings):
    BrainStore(settings);factory=ModelFactory(settings);model=factory.train('gold',_rows('asset_profile_average_absolute_response'));_champion(settings,model,'gold')
    store=FeatureStore(settings);event={'id':None,'domain':'MACRO','event_type':'REAL_YIELD','importance':.9,'novelty':.8,'source_credibility':.9};asset={'asset_id':'XAUUSD','asset_class':'COMMODITY','mapping_confidence':.9,'directional_bias':'MIXED'}
    with connect(settings.data_dir/'brain'/'intelligence.sqlite3',settings) as con:con.execute("INSERT INTO asset_profiles(asset_id,profile_json,sample_count,updated_at) VALUES('XAUUSD',?,40,?)",(json.dumps({'average_absolute_response':.1}),time.time()))
    first=store.build_sync(event,asset,300,prediction_time=3000)
    with connect(settings.data_dir/'brain'/'intelligence.sqlite3',settings) as con:con.execute("UPDATE asset_profiles SET profile_json=?,updated_at=? WHERE asset_id='XAUUSD'",(json.dumps({'average_absolute_response':.9}),time.time()))
    second=store.build_sync(event,asset,300,prediction_time=3001)
    runtime=RuntimeInferenceEngine(settings);p1=runtime.infer(event,asset,300,first,'SHADOW',[],False)[0][0].direction_probability;p2=runtime.infer(event,asset,300,second,'SHADOW',[],False)[0][0].direction_probability
    assert first['values']['asset_profile_average_absolute_response']==.1 and second['values']['asset_profile_average_absolute_response']==.9 and abs(p2-p1)>.15


def test_meta_required_gate_ignores_manual_signal_enable(settings,monkeypatch):
    async def run():
        monkeypatch.setenv('QUALIFICATION_MIN_EVENTS','1');monkeypatch.setenv('QUALIFICATION_MIN_RESPONSES','1');brain=BrainStore(settings);await brain.set_meta('signals_enabled','1');service=__import__('v52.intelligence.service',fromlist=['IntelligenceService']).IntelligenceService(settings,Metrics('gate-test',settings.state_dir))
        try:assert not service._live_artifact_gate_sync()
        finally:await service.semantics.close()
        result=await ResearchService(settings,Metrics('qualification-test',settings.state_dir)).qualification();assert not result['qualified'] and not result['checks']['meta_champion'] and await brain.get_meta('signals_enabled')=='0'
    asyncio.run(run())


def test_adverse_new_evidence_moves_active_signal_to_exit_pending(settings):
    brain=BrainStore(settings);now=time.time()
    with connect(brain.path,settings) as con:
        event_id=con.execute("INSERT INTO events(cluster_key,event_type,domain,title,summary,occurred_at,first_observed_at,last_observed_at,importance,novelty,surprise,source_credibility,priority,state,extraction_method,extraction_version,created_at,updated_at) VALUES('active-exit','REAL_YIELD','MACRO','event','event','x','x','x',.9,.9,0,.9,'P1','INITIAL','fixture','53',?,?)",(now,now)).lastrowid
        prediction_id=con.execute("INSERT INTO predictions(prediction_key,event_id,asset_id,horizon_seconds,direction_probability,calibrated_confidence,expected_magnitude,uncertainty,expected_value,risk_score,action,mode,model_version_id,feature_version,feature_cutoff_at,evidence_json,created_at) VALUES('active-exit',?,'XAUUSD',300,.8,.8,.01,.1,.01,.2,'BUY','LIVE','artifact:meta:test','features-53.0.0','x','{}',?)",(event_id,now)).lastrowid
        signal_id=con.execute("INSERT INTO signals(signal_key,prediction_id,state,action,entry_logic,invalidation_logic,target_logic,exit_logic,opened_at,telegram_state,last_updated_at) VALUES('active-exit',?,'ACTIVE','BUY','entry','invalid','target','exit',?,'ELIGIBLE',?)",(prediction_id,now,now)).lastrowid
    actions=ActiveSignalAgent(settings).reevaluate('XAUUSD',.2,.4,None,{'new_event':'adverse'})
    with connect(brain.path,settings) as con:state=con.execute('SELECT state FROM signals WHERE id=?',(signal_id,)).fetchone()['state']
    assert actions and actions[0]['action']=='EXIT' and state=='EXIT_PENDING'


def test_consolidator_retains_one_best_horizon():
    def item(horizon,confidence,risk,ev):
        decision=SignalDecision('BUY','BTC',horizon,.8,confidence,.01,.1,ev,risk,'e','i','t','x',.8,40,'LIVE',())
        return {'decision':decision,'feature':{'quality':.9}}
    result=SignalConsolidator().consolidate([item(30,.6,.5,.001),item(300,.8,.2,.01),item(3600,.7,.3,.005)])
    assert result and result.item['decision'].horizon_seconds==300 and result.suppressed==2


def test_probabilistic_regime_outputs_normalized_uncertainty():
    prices=[100*math.exp(.003*index+(.002 if index%3 else -.001)) for index in range(80)];regime=detect_regime(prices,[1000+index*5 for index in range(80)])
    assert abs(sum(regime['probabilities'].values())-1)<1e-9 and 0<=regime['uncertainty']<=1 and 0<=regime['transition_probability']<=1 and regime['regime'] in regime['probabilities']


def test_live_public_adapter_and_watchlist_poll_with_fixture(settings,monkeypatch):
    BrainStore(settings);provider=PublicChartMarketProvider(settings);now=int(time.time())
    monkeypatch.setattr(provider,'_download',lambda url:{'chart':{'result':[{'timestamp':[now-60,now],'indicators':{'quote':[{'close':[100,101],'volume':[10,12]}]}}]}})
    watch=ActiveAssetWatchlist(settings);watch.activate('equity:XNAS:PLTR','earnings',1);result=watch.poll(provider,1)
    with connect(settings.data_dir/'brain'/'intelligence.sqlite3',settings) as con:count=con.execute("SELECT COUNT(*) count FROM reference_prices WHERE asset_id='equity:XNAS:PLTR' AND provider='public_chart'").fetchone()['count']
    assert result['successful']==1 and count==1  # The current, unfinished candle is excluded.


def _seed_interaction(settings,interaction:bool):
    brain=BrainStore(settings);now=1000.0
    with connect(brain.path,settings) as con:
        for index in range(60):
            a=-1+2*(index%10)/9;b=-1+2*(index//10)/5;label=.02*a*b if interaction else .02*a
            event_id=con.execute("INSERT INTO events(cluster_key,event_type,domain,title,summary,occurred_at,first_observed_at,last_observed_at,importance,novelty,surprise,source_credibility,priority,state,extraction_method,extraction_version,created_at,updated_at) VALUES(?,?,?,?,?,'x','x','x',.8,.8,0,.8,'P1','INITIAL','fixture','53',?,?)",(f'interaction-{index}','TEST','GENERAL','test','test',now+index,now+index)).lastrowid
            feature_id=f'interaction-feature-{index}';con.execute("INSERT INTO feature_vectors(feature_id,event_id,asset_id,horizon_seconds,feature_version,prediction_time,available_cutoff,quality,values_json,provenance_json,replay,created_at) VALUES(?,?, 'BTC',300,'features-53.0.0',?,?,.9,?,'[]',0,?)",(feature_id,event_id,now+index,now+index,json.dumps({'a':a,'b':b}),now+index))
            target=con.execute("INSERT INTO response_targets(event_id,asset_id,horizon_seconds,event_epoch,due_at,state) VALUES(?,'BTC',300,?,?,'complete')",(event_id,now+index,now+index+300)).lastrowid
            con.execute("INSERT INTO market_responses(target_id,event_id,asset_id,horizon_seconds,start_price,end_price,return_value,volatility,volume,mfe,mae,direction,reversal,continuation,time_to_peak_seconds,observed_samples,measured_at) VALUES(?,?, 'BTC',300,100,?,?,.01,1000,?,?,?,0,1,150,3,?)",(target,event_id,100*(1+label),label,max(label,0),min(label,0),'UP' if label>0 else 'DOWN',now+index+300))


@pytest.mark.parametrize('interaction,expected',[ (True,True),(False,False) ])
def test_feature_combination_hypothesis_is_experimented_and_failed_not_knowledge(settings,interaction,expected):
    _seed_interaction(settings,interaction);result=FeatureInteractionHypothesisEngine(settings).experiment('a','b')
    with connect(settings.data_dir/'brain'/'intelligence.sqlite3',settings) as con:knowledge=con.execute("SELECT 1 FROM knowledge WHERE subject='a' AND predicate='INTERACTS_WITH' AND object='b'").fetchone();status=con.execute("SELECT status FROM hypotheses WHERE statement LIKE 'a x b%'").fetchone()['status']
    assert result['executed'] and result['validated'] is expected and bool(knowledge) is expected and status==('VALIDATED' if expected else 'REJECTED')


def test_all_typed_telegram_producers_exist_and_reject_raw(settings):
    assert len(FINAL_INTELLIGENCE_KINDS)==11
    producer=TelegramIntelligenceProducers(ControlStore(settings))
    with pytest.raises(ValueError):asyncio.run(producer.emit('raw_news','bad',{'raw_news':{}}))


def test_pump_risk_runtime_output_builds_pump_warning():
    votes=[ExpertVote('pump_risk','SUI',300,.88,.04,.2,1.0,('artifact',))]
    warnings=IntelligenceService._risk_warning_payloads({'title':'Coinbase listing SUI'},{'asset_id':'SUI'},votes)
    assert len(warnings)==1 and warnings[0]['kind']=='pump_warning' and warnings[0]['confidence']==.88


def test_backtest_deducts_spread_fee_slippage_delay_and_liquidity(settings):
    _seed_interaction(settings,True)
    class Artifact:
        @staticmethod
        def predict(values):return .9
    result=BacktestEngine(settings).evaluate_artifact(Artifact(),spread_bps=2,fee_bps=3,slippage_bps=4,delay_bps=5,liquidity_penalty_bps=6)
    assert result['samples']==60 and result['total_cost_bps_per_trade']==20 and result['gross_expected_value']>result['net_expected_value']


@pytest.mark.parametrize('family',TemporalModelFactory.FAMILIES)
def test_optional_temporal_adapters_execute_or_fail_gracefully(family):
    status=TemporalModelFactory.availability()
    if not status['available']:
        assert status['reason']=='optional_dependency_missing' and family in status['families'];return
    sequences=[[[index/20,step/5] for step in range(5)] for index in range(20)];labels=[int(index>=10) for index in range(20)]
    artifact=TemporalModelFactory().train(family,sequences,labels,epochs=1);predictions=artifact.predict(sequences[:2])
    assert len(predictions)==2 and all(0<=value<=1 for value in predictions)
