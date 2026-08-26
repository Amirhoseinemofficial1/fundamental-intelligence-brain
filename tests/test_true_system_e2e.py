from __future__ import annotations
import asyncio,time
from v52.intelligence.market_response import MarketResponseService
from v52.intelligence.model_factory import ModelFactory
from v52.intelligence.research import ResearchService
from v52.intelligence.service import IntelligenceService
from v52.intelligence.signal_state import SignalStateMachine
from v52.intelligence.store import BrainStore
from v52.metrics import Metrics
from v52.storage.sqlite import connect
from v52.storage.structured import StructuredStore

def _training(key):
    return [{'prediction_time':1000+i,'feature_version':'features-52.true.1','values':{key:-1+2*i/39,'data_quality':.9},'label':int(i>=20)} for i in range(40)]

def test_fundamental_observation_to_artifact_prediction_outcome_learning_report(settings,monkeypatch):
    async def run():
        monkeypatch.setenv('OPENAI_SEMANTICS_ENABLED','0');monkeypatch.setenv('MODEL_MIN_SHADOW_OUTCOMES','10');monkeypatch.setenv('QUALIFICATION_MIN_SPECIALIST_CHAMPIONS','1');brain=BrainStore(settings);factory=ModelFactory(settings)
        gold=factory.train('gold',_training('real_yield'),state='SHADOW');meta=factory.train('meta',_training('expert_gold'),state='SHADOW')
        with connect(brain.path,settings) as con:
            con.execute("UPDATE model_artifacts SET state='CHAMPION',promoted_at=? WHERE artifact_id=?",(time.time(),gold['artifact_id']));con.execute("UPDATE model_artifacts SET state='CHAMPION',promoted_at=? WHERE artifact_id=?",(time.time(),meta['artifact_id']))
            con.execute("INSERT INTO improvement_records(metric,before_value,after_value,sample_size,validation_window,model_version,created_at) VALUES('shadow_brier',.25,.20,10,'unseen_runtime_shadow',?,?)",(meta['artifact_id'],time.time()))
            con.execute("UPDATE metadata SET value='86400' WHERE key IN ('observation_runtime_seconds','shadow_runtime_seconds')");con.execute("UPDATE metadata SET value='1' WHERE key='signals_enabled'")
        structured=StructuredStore(settings);assert await structured.insert_snapshot('official_macro','MACRO','REAL_YIELD',{'symbol':'XAUUSD','value':.9,'unit':'percent','event_time':time.time()})
        service=IntelligenceService(settings,Metrics('true-e2e',settings.state_dir));cursor,observations=await service.observations.drain(0,10);assert observations
        event_id,signal_ids=await service.process_observation(observations[0]);assert event_id and signal_ids
        with connect(brain.path,settings) as con:
            predictions=con.execute('SELECT * FROM predictions WHERE event_id=? ORDER BY id',(event_id,)).fetchall()
            prediction=next((p for p in predictions if str(p['model_version_id']).startswith('artifact:meta:')),None)
            assert prediction is not None
            signal=con.execute("SELECT * FROM signals WHERE prediction_id=? AND state='QUALIFIED'",(prediction['id'],)).fetchone()
            assert str(prediction['model_version_id']).startswith('artifact:meta:')
        machine=SignalStateMachine(settings)
        for state in ('SENT','ENTRY_PENDING','ACTIVE'):await machine.transition(int(signal['id']),state,'fixture-confirmed')
        with connect(brain.path,settings) as con:
            target=con.execute('SELECT * FROM response_targets WHERE event_id=? AND horizon_seconds=? LIMIT 1',(event_id,prediction['horizon_seconds'])).fetchone();assert target
            for i,price in enumerate((2400,2402,2406,2410)):
                ts=float(target['event_epoch'])+i*float(target['horizon_seconds'])/3
                con.execute("INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('fixture','XAUUSD',?,?,?,?,.9,'{}')",(ts,ts,price,1000+i))
        await MarketResponseService(settings,Metrics('response-e2e',settings.state_dir))._process_target(dict(target))
        research=ResearchService(settings,Metrics('research-e2e',settings.state_dir));lifecycle=await research.review_signal_lifecycle();assert lifecycle['closed']>=1
        report=await research.daily_report();assert report['performance']['resolved_outcomes']>=1 and report['per_asset']
        with connect(brain.path,settings) as con:
            assert con.execute("SELECT 1 FROM signals WHERE id=? AND state='LEARNED'",(signal['id'],)).fetchone()
            assert con.execute('SELECT 1 FROM post_exit WHERE signal_id=?',(signal['id'],)).fetchone()
        await service.semantics.close()
    asyncio.run(run())
