from __future__ import annotations

import asyncio
import time

import pytest

from v52.intelligence.market_response import MarketResponseService
from v52.intelligence.model_factory import ModelFactory
from v52.intelligence.research import ResearchService
from v52.intelligence.service import IntelligenceService
from v52.intelligence.store import BrainStore
from v52.metrics import Metrics
from v52.storage.sqlite import connect


CASES=(
    ('gold','XAUUSD','US CPI inflation report surprises markets and gold XAU reacts'),
    ('oil','WTI','OPEC announces a production cut for WTI crude oil'),
    ('forex','EURUSD','ECB interest rate decision moves EUR/USD euro'),
    ('equity','equity:XNAS:PLTR','Palantir earnings report beats estimates'),
    ('crypto','BTC','Bitcoin spot ETF approved by regulator'),
)


def _training(key):
    return [{'prediction_time':1000+i,'feature_version':'features-53.0.0','values':{key:-1+2*i/39},'label':int(i>=20)} for i in range(40)]


def _champion(con,result,expert):
    con.execute("UPDATE model_artifacts SET state='CHAMPION',promoted_at=? WHERE artifact_id=?",(time.time(),result['artifact_id']))


@pytest.mark.parametrize('expert,asset_id,title',CASES,ids=[item[0] for item in CASES])
def test_independent_asset_pipeline_to_real_outcome_and_profile(settings,monkeypatch,expert,asset_id,title):
    async def run():
        monkeypatch.setenv('OPENAI_SEMANTICS_ENABLED','0');brain=BrainStore(settings);factory=ModelFactory(settings)
        specialist=factory.train(expert,_training('event_importance'));meta=factory.train('meta',_training(f'expert_{expert}'))
        with connect(brain.path,settings) as con:_champion(con,specialist,expert);_champion(con,meta,'meta')
        now=time.time()-1000;story={'id':abs(hash((expert,asset_id)))%1_000_000+1,'fingerprint':f'e2e-{expert}','title':title,'summary':title,'category':'fundamental','materiality':9,'source_name':'official-fixture','source_url':'fixture://e2e','published_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(now)),'first_observed_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(now+1))}
        service=IntelligenceService(settings,Metrics(f'e2e-{expert}',settings.state_dir))
        try:event_id,signal_ids=await service.process_story(story)
        finally:await service.semantics.close()
        assert signal_ids
        with connect(brain.path,settings) as con:
            assert con.execute('SELECT 1 FROM event_assets WHERE event_id=? AND asset_id=?',(event_id,asset_id)).fetchone()
            output=con.execute("SELECT 1 FROM expert_outputs eo JOIN feature_vectors fv ON fv.feature_id=eo.feature_id WHERE fv.event_id=? AND eo.expert=? AND eo.model_state='CHAMPION'",(event_id,expert)).fetchone();assert output
            prediction=con.execute("SELECT * FROM predictions WHERE event_id=? AND asset_id=? AND model_version_id=? ORDER BY id LIMIT 1",(event_id,asset_id,meta['artifact_id'])).fetchone();assert prediction
            target=con.execute('SELECT * FROM response_targets WHERE event_id=? AND asset_id=? AND horizon_seconds=?',(event_id,asset_id,prediction['horizon_seconds'])).fetchone();assert target
            for index,price in enumerate((100.0,100.3,100.8,101.0)):
                epoch=float(target['event_epoch'])+index*float(target['horizon_seconds'])/3
                con.execute("INSERT INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('fixture',?,?,?,?,1000,.95,'{}')",(asset_id,epoch,epoch,price))
        await MarketResponseService(settings,Metrics(f'response-{expert}',settings.state_dir))._process_target(dict(target))
        research=ResearchService(settings,Metrics(f'research-{expert}',settings.state_dir));await research.update_knowledge()
        with connect(brain.path,settings) as con:
            response=con.execute('SELECT * FROM market_responses WHERE event_id=? AND asset_id=?',(event_id,asset_id)).fetchone();profile=con.execute('SELECT * FROM asset_profiles WHERE asset_id=?',(asset_id,)).fetchone()
        assert response and response['observed_samples']>=2 and profile and profile['sample_count']>=1
    asyncio.run(run())
