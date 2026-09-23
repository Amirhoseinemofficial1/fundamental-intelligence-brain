import asyncio
import json
import sqlite3
import pytest
from v52.storage.sqlite import connect,transaction
from v52.intelligence.barrier_evaluation import Candle,evaluate,LEVELS,SignalEvaluator
from v52.intelligence.neural_bandit import NeuralBandit,vector
from v52.alerts.telegram import render_batch,full_signal_report
from v52.storage.control import ControlStore
from v52.constants import MARKET_SERVICES
from v52.diagnostics.preflight import _required_services


def test_connections_close_and_rollback(settings):
    path=settings.data_dir/'test.sqlite3'
    with connect(path,settings) as con:con.execute('CREATE TABLE x(v)')
    with pytest.raises(sqlite3.ProgrammingError):con.execute('SELECT 1')
    with pytest.raises(RuntimeError):
        with connect(path,settings) as con:
            transaction(con);con.execute('INSERT INTO x VALUES(1)');raise RuntimeError('abort')
    with connect(path,settings) as con:assert con.execute('SELECT COUNT(*) FROM x').fetchone()[0]==0


def test_preflight_allows_bounded_market_collector_subset(settings):
    required=_required_services(settings)
    assert not required.intersection(MARKET_SERVICES)
    assert {'market_response','news_hot','intelligence'}.issubset(required)


@pytest.mark.parametrize('side',[-1,1])
def test_every_symmetric_level_first_touch_and_cost(side):
    for level in LEVELS:
        price=100*(1+side*level)
        candles=[Candle(60,120,100,max(100,price),min(100,price),price),Candle(120,180,price,max(price,100),min(price,100),100)]
        item=next(r for r in evaluate(candles,59,180,side,2) if r['level']==level)
        assert item['state']=='WIN'
        assert item['reward_r']==pytest.approx(1-.0002/level)
        assert abs(item['take_profit']-100)==pytest.approx(abs(item['stop_loss']-100))


def test_stop_before_later_profit_and_full_excursions():
    candles=[Candle(60,120,100,100,99,99),Candle(120,180,99,112,99,112)]
    result=evaluate(candles,60,180,1,0)
    assert result[3]['state']=='LOSS'
    assert result[3]['mfe']==pytest.approx(.12)
    assert result[3]['mae']==pytest.approx(-.01)


def test_ambiguous_candle_is_never_training_win():
    result=evaluate([Candle(60,120,100,111,89,100)],60,120,1)
    assert all(r['state']=='AMBIGUOUS' and r['reward_r'] is None for r in result)


def test_gaps_and_predecision_highs_are_rejected():
    assert evaluate([Candle(60,120,100,101,99,100),Candle(180,240,100,101,99,100)],60,240,1) is None
    assert evaluate([Candle(0,60,100,999,1,100)],1,60,1) is None
    assert evaluate([Candle(60,120,100,float('nan'),90,100)],60,120,1) is None


def test_timeout_and_zero_cost():
    result=evaluate([Candle(60,120,100,100.01,99.99,100)],60,120,1,0)
    assert all(r['state']=='TIMEOUT' and r['reward_r']==0 for r in result)


def test_neural_shared_representation_learns_and_roundtrips():
    model=NeuralBandit();x=vector({'importance':.8,'surprise':2},1,300)
    before=model.forward(x)[1][2]
    for _ in range(250):model.update(x,2,.9)
    after=model.forward(x)[1][2]
    assert abs(after-.9)<abs(before-.9)/4
    assert NeuralBandit(json.loads(json.dumps(model.state))).forward(x)[1]==model.forward(x)[1]
    assert model.state['version']==250
    with pytest.raises(ValueError):model.update(x,0,float('nan'))


def test_full_report_retains_all_links_and_escapes_html():
    sources=[dict(source_url=f'https://example.com/{i}',title='<script>bad</script>',summary='detail '*300) for i in range(50)]
    payload=dict(kind='intelligence_signal',signal_id=1,asset_id='ETH',sources=sources,reasons=['reason '*1000],probability=0,uncertainty=0,risk_score=0)
    report=full_signal_report(payload).decode()
    assert 'https://example.com/49' in report and '<script>' not in report
    text=render_batch([({},payload)])
    assert len(text)<4000 and 'سیگنال فاندامنتال' in text and 'منابع و لینک بررسی' in text


def test_provisional_signal_renders_exact_reference_scenario():
    payload={'kind':'provisional_signal','signal_id':7,'asset_id':'BTC','action':'WATCH_BUY','provisional_gate':True,
             'probability':.72,'calibrated_confidence':.63,'uncertainty':1,'risk_score':.8,
             'direction':'UP','event_type':'ETF_DECISION','event_title':'SEC approves spot Bitcoin ETF',
             'event_summary':'The official decision permits the product.','market':'BTCUSDT','horizon_seconds':3600,
             'reference_price':100,'reference_price_provider':'cex:binance','reference_price_observed_at':'2026-09-23T00:00:00Z',
             'reference_price_age_seconds':12,'reference_entry':100,'reference_target':100.5,'reference_invalidation':99.5,
             'reference_move_pct':.5,'qualification_status':'GLOBAL_GATE_FAILED',
             'provisional_basis':'DETERMINISTIC_EVENT_MECHANISM:EXCHANGE_LISTING:dynamic_universe',
             'model_probability_unqualified':.2,'sources':[{'source_name':'SEC','title':'Official ETF order',
             'source_url':'https://www.sec.gov/example','observed_at':'2026-09-23T00:00:00Z'}]}
    text=render_batch([({},payload)])
    assert 'سیگنال فاندامنتال' in text and '100.5' in text and '99.5' in text
    assert 'خبر مبنای سیگنال' in text and 'SEC approves spot Bitcoin ETF' in text
    assert 'https://www.sec.gov/example' in text and 'فایل' not in text
    delayed_link={**payload,'sources':[{'title':f'no-url-{i}'} for i in range(8)]+payload['sources']}
    assert 'https://www.sec.gov/example' in render_batch([({},delayed_link)])


def test_fundamental_signal_scope_is_exact():
    from v52.intelligence.service import IntelligenceService
    allowed=[{'asset_id':'BTC','symbol':'BTC','asset_class':'CRYPTO'},
             {'asset_id':'ETH','symbol':'ETH','asset_class':'CRYPTO'},
             {'asset_id':'XAUUSD','symbol':'XAUUSD','asset_class':'COMMODITY'},
             {'asset_id':'WTI','symbol':'WTI','asset_class':'COMMODITY'},
             {'asset_id':'EURUSD','symbol':'EURUSD','asset_class':'FOREX'},
             {'asset_id':'SPX','symbol':'SPX','asset_class':'INDEX'},
             {'asset_id':'equity:XNAS:AAPL','symbol':'AAPL','asset_class':'EQUITY'}]
    blocked=[{'asset_id':'SOL','symbol':'SOL','asset_class':'CRYPTO'},
             {'asset_id':'crypto:DOGE','symbol':'DOGE','asset_class':'CRYPTO'},
             {'asset_id':'SILVER','symbol':'XAGUSD','asset_class':'COMMODITY'},
             {'asset_id':'FTSE','symbol':'FTSE','asset_class':'INDEX'},
             {'asset_id':'equity:XLON:VOD','symbol':'VOD','asset_class':'EQUITY'},
             {'asset_id':'base:0x1','symbol':'ABC','asset_class':'TOKEN'}]
    assert all(IntelligenceService._signal_asset_allowed(x) for x in allowed)
    assert not any(IntelligenceService._signal_asset_allowed(x) for x in blocked)


def test_signal_and_full_report_outbox_atomic_idempotent(settings):
    store=ControlStore(settings)
    for _ in range(2):asyncio.run(store.enqueue_alert('test','intelligence_signal',10,{'signal_id':9}))
    with connect(store.path,settings) as con:
        assert con.execute('SELECT COUNT(*) FROM alerts').fetchone()[0]==1
        assert con.execute("SELECT COUNT(*) FROM alerts WHERE kind='intelligence_report'").fetchone()[0]==0

def test_evaluator_persists_once_and_training_uses_exact_feature(settings,monkeypatch):
    from v52.intelligence.service import IntelligenceService
    from v52.intelligence.learning import TrainingCoordinator
    from v52.metrics import Metrics
    from test_v52_brain_persistence import _candidate
    monkeypatch.setenv('OPENAI_SEMANTICS_ENABLED','0')
    monkeypatch.setenv('MODEL_SIGNAL_LABELS_ONLY','1')
    async def setup():
        control=ControlStore(settings)
        candidate=_candidate('Federal Reserve cuts interest rate','Policy decision affects gold and yields')
        await control.record_story(candidate,candidate.canonical_url,False,None,0,None,False)
        service=IntelligenceService(settings,Metrics('test',settings.state_dir))
        try:return await service.process_story((await control.story_changes_after(0,10))[0])
        finally:await service.semantics.close()
    event_id,signals=asyncio.run(setup())
    evaluator=SignalEvaluator(settings)
    with connect(evaluator.path,settings) as con:
        signal=con.execute('SELECT prediction_id FROM signals WHERE id=?',(signals[0],)).fetchone()
        pred=con.execute('SELECT * FROM predictions WHERE id=?',(signal['prediction_id'],)).fetchone()
        fid=json.loads(pred['evidence_json'])['feature_id']
        con.execute('UPDATE predictions SET created_at=60,horizon_seconds=300,direction_probability=.8 WHERE id=?',(pred['id'],))
        con.execute('UPDATE feature_vectors SET prediction_time=59,available_cutoff=59,horizon_seconds=300 WHERE feature_id=?',(fid,))
    candles=[Candle(60+i*60,120+i*60,100+i,101+i,100+i,101+i) for i in range(5)]
    evaluator.reader.history=lambda *args:(candles,'fixture')
    assert evaluator.cycle(now=500)['completed']==1
    assert evaluator.cycle(now=500)['completed']==0
    with connect(evaluator.path,settings) as con:
        assert con.execute('SELECT COUNT(*) FROM signal_barriers').fetchone()[0]==7
        assert con.execute('SELECT COUNT(*) FROM neural_bandit_updates').fetchone()[0]==7
        assert json.loads(con.execute('SELECT state_json FROM neural_bandit_state').fetchone()[0])['version']==7
    rows=TrainingCoordinator(settings).learning_rows()
    assert len(rows)==1 and rows[0]['feature_id']==fid and rows[0]['label_available_at']==500
    assert rows[0]['return_value']==pytest.approx(.05)
