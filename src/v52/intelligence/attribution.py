from __future__ import annotations

from typing import Any
import json

from ..config import Settings
from ..storage.sqlite import connect

FAILURE_CATEGORIES=("STALE_INFORMATION","PRICED_IN","WRONG_ASSET","WRONG_HORIZON","REGIME_CHANGE","BAD_SOURCE","DATA_QUALITY","LIQUIDITY_SHOCK","FALSE_RELATION","EXPERT_OVERCONFIDENCE","CALIBRATION_FAILURE","TIMING_FAILURE","ENTRY_FAILURE","EXIT_FAILURE","MISSING_PROVIDER")
MISSED_CAUSES=("EVENT_NOT_CAPTURED","ASSET_RESOLUTION_FAILED","PROVIDER_MISSING","ROUTING_FAILED","DATA_STALE","EXPERT_WEAK","META_REJECTED","WRONG_HORIZON","THRESHOLD_TOO_HIGH","REGIME_WRONG","UNCERTAINTY_TOO_HIGH")

class FailureAttributionEngine:
    def __init__(self,settings:Settings|None=None):
        self.settings=settings;self.path=settings.data_dir/'brain'/'intelligence.sqlite3' if settings else None

    def prediction_evidence(self,prediction_id:int)->dict[str,Any]:
        if not self.settings or not self.path:raise RuntimeError('settings required for evidence collection')
        with connect(self.path,self.settings) as con:
            row=con.execute("""SELECT p.*,e.priced_in_score,a.asset_id,ea.mapping_confidence,
                mr.return_value,mr.mfe,mr.mae,mr.measured_at FROM predictions p JOIN events e ON e.id=p.event_id
                JOIN assets a ON a.asset_id=p.asset_id
                LEFT JOIN event_assets ea ON ea.event_id=p.event_id AND ea.asset_id=p.asset_id
                LEFT JOIN market_responses mr ON mr.event_id=p.event_id AND mr.asset_id=p.asset_id AND mr.horizon_seconds=p.horizon_seconds
                WHERE p.id=?""",(prediction_id,)).fetchone()
            if not row:return {'prediction_missing':True,'provider_available':False,'asset_valid':False}
            evidence=json.loads(row['evidence_json'] or '{}');feature_id=str(evidence.get('feature_id') or '')
            fv=con.execute("SELECT quality,freshness_json,prediction_time FROM feature_vectors WHERE feature_id=?",(feature_id,)).fetchone()
            prediction_time=float(fv['prediction_time'] if fv else row['created_at'])
            prices=int(con.execute("SELECT COUNT(*) count FROM reference_prices WHERE asset_id=?",(row['asset_id'],)).fetchone()['count'])
            prediction_regime=con.execute("SELECT regime,observed_at FROM regimes WHERE asset_id=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",(row['asset_id'],prediction_time)).fetchone()
            outcome_time=float(row['measured_at'] or prediction_time+int(row['horizon_seconds']))
            outcome_regime=con.execute("SELECT regime,observed_at FROM regimes WHERE asset_id=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",(row['asset_id'],outcome_time)).fetchone()
            source_rows=con.execute("""SELECT esl.source_id,esl.provider_id,esl.story_id,sp.reliability
                FROM event_source_lineage esl LEFT JOIN source_profiles sp ON sp.source_id=esl.source_id WHERE esl.event_id=?""",(row['event_id'],)).fetchall()
            snapshot=con.execute("SELECT ass.liquidity,ass.action FROM active_signal_snapshots ass JOIN signals s ON s.id=ass.signal_id WHERE s.prediction_id=? ORDER BY ass.observed_at DESC LIMIT 1",(prediction_id,)).fetchone()
        freshness=json.loads(fv['freshness_json'] or '{}') if fv else {};stale_evidence=[]
        for name,item in freshness.items():
            observed=float(item.get('observed_at') or item.get('available_at') or prediction_time);available=float(item.get('available_at') or observed);ttl=float(item.get('ttl_seconds') or max(0.0,float(item.get('expires_at') or available)-available));age=max(0.0,prediction_time-available);valid=bool(item.get('valid')) and age<=ttl
            if not valid:stale_evidence.append({'feature_name':name,'observed_at':observed,'available_at':available,'prediction_time':prediction_time,'ttl_seconds':ttl,'actual_age_seconds':age,'valid':valid,'source':item.get('source')})
        source_evidence=[{'event_id':int(row['event_id']),'story_id':int(item['story_id']),'source_id':str(item['source_id']),'provider_id':str(item['provider_id']),'reliability':float(item['reliability']) if item['reliability'] is not None else None} for item in source_rows]
        reliabilities=[float(item['reliability']) for item in source_rows if item['reliability'] is not None]
        return {'prediction_id':prediction_id,'asset_id':row['asset_id'],'asset_valid':True,'provider_available':prices>0,'provider_price_samples':prices,
            'priced_in_score':float(row['priced_in_score']),'data_quality':float(fv['quality'] if fv else 0),'stale_features':[x['feature_name'] for x in stale_evidence],'stale_feature_evidence':stale_evidence,
            'source_reliability':min(reliabilities) if reliabilities else 1.0,'event_source_evidence':source_evidence,
            'prediction_time_regime':dict(prediction_regime) if prediction_regime else None,'outcome_time_regime':dict(outcome_regime) if outcome_regime else None,
            'regime_changed':bool(prediction_regime and outcome_regime and prediction_regime['regime']!=outcome_regime['regime']),'liquidity_shock':bool(snapshot and snapshot['liquidity'] is not None and float(snapshot['liquidity'])<.2),
            'confidence':float(row['calibrated_confidence']),'correct':(float(row['return_value'] or 0)>0)==(float(row['direction_probability'])>.5),
            'calibration_error':abs(float(row['direction_probability'])-int(float(row['return_value'] or 0)>0)),
            'relation_invalid':float(row['mapping_confidence'] or 0)<.45,
            'entry_failure':abs(float(row['mae'] or 0))>max(.005,abs(float(row['mfe'] or 0))),
            'peak_after_horizon':abs(float(row['mfe'] or 0))>abs(float(row['return_value'] or 0))*2,'exit_failure':bool(snapshot and str(snapshot['action']) in {'EXIT','INVALIDATE'} and float(row['return_value'] or 0)>0)}

    def missed_evidence(self,response_id:int)->dict[str,Any]:
        if not self.settings or not self.path:raise RuntimeError('settings required for evidence collection')
        with connect(self.path,self.settings) as con:
            row=con.execute("SELECT * FROM market_responses WHERE id=?",(response_id,)).fetchone()
            if not row:return {'event_captured':False,'asset_resolved':False,'provider_available':False,'routed':False}
            event=bool(con.execute("SELECT 1 FROM events WHERE id=?",(row['event_id'],)).fetchone());asset=bool(con.execute("SELECT 1 FROM assets WHERE asset_id=?",(row['asset_id'],)).fetchone());routed=bool(con.execute("SELECT 1 FROM event_assets WHERE event_id=? AND asset_id=?",(row['event_id'],row['asset_id'])).fetchone());provider=int(con.execute("SELECT COUNT(*) count FROM reference_prices WHERE asset_id=?",(row['asset_id'],)).fetchone()['count']);prediction=con.execute("SELECT action,uncertainty,horizon_seconds,direction_probability,created_at FROM predictions WHERE event_id=? AND asset_id=? ORDER BY created_at DESC LIMIT 1",(row['event_id'],row['asset_id'])).fetchone();features=con.execute("SELECT quality,freshness_json,prediction_time FROM feature_vectors WHERE event_id=? AND asset_id=? ORDER BY prediction_time DESC LIMIT 1",(row['event_id'],row['asset_id'])).fetchone();experts=int(con.execute("SELECT COUNT(*) count FROM expert_outputs eo JOIN feature_vectors fv ON fv.feature_id=eo.feature_id WHERE fv.event_id=? AND fv.asset_id=?",(row['event_id'],row['asset_id'])).fetchone()['count'])
            sources=[dict(x) for x in con.execute("SELECT source_id,provider_id,story_id FROM event_source_lineage WHERE event_id=?",(row['event_id'],)).fetchall()]
            prediction_regime=con.execute("SELECT regime FROM regimes WHERE asset_id=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",(row['asset_id'],float(features['prediction_time']) if features else float(row['measured_at'])-int(row['horizon_seconds']))).fetchone()
            outcome_regime=con.execute("SELECT regime FROM regimes WHERE asset_id=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",(row['asset_id'],float(row['measured_at']))).fetchone()
        freshness=json.loads(features['freshness_json'] or '{}') if features else {};stale=bool(features and any(not bool(v.get('valid')) for v in freshness.values()))
        prediction_action=str(prediction['action']) if prediction else ''
        threshold_block=bool(prediction and prediction_action in {'NO_TRADE','WAIT'} and abs(float(prediction['direction_probability'])-.5)<.12)
        return {'response_id':response_id,'event_captured':event,'asset_resolved':asset,'provider_available':provider>0,'provider_price_samples':provider,'routed':routed,'data_stale':stale,'feature_freshness':freshness,'expert_weak':experts==0,'meta_rejected':bool(prediction and prediction_action in {'NO_TRADE','WAIT'}),'wrong_horizon':bool(prediction and int(prediction['horizon_seconds'])!=int(row['horizon_seconds'])),'threshold_too_high':threshold_block,'regime_wrong':bool(prediction_regime and outcome_regime and prediction_regime['regime']!=outcome_regime['regime']),'prediction_regime':dict(prediction_regime) if prediction_regime else None,'outcome_regime':dict(outcome_regime) if outcome_regime else None,'uncertainty_too_high':bool(prediction and float(prediction['uncertainty'])>.5),'return_value':float(row['return_value']),'event_sources':sources,'expert_output_count':experts,'evidence_sources':['events','event_source_lineage','event_assets','reference_prices','feature_vectors','expert_outputs','predictions','regimes']}
    @staticmethod
    def attribute(context:dict[str,Any])->dict[str,Any]:
        checks=[
            ('STALE_INFORMATION',bool(context.get('stale_feature_evidence') or context.get('stale_features')) or float(context.get('data_age_seconds',0))>float(context.get('ttl_seconds',1e99))),
            ('PRICED_IN',float(context.get('priced_in_score',0))>=.7),('WRONG_ASSET',not bool(context.get('asset_valid',True))),
            ('WRONG_HORIZON',bool(context.get('peak_after_horizon'))),('REGIME_CHANGE',bool(context.get('regime_changed'))),
            ('BAD_SOURCE',float(context.get('source_reliability',1))<.4),('DATA_QUALITY',float(context.get('data_quality',1))<.45),
            ('LIQUIDITY_SHOCK',bool(context.get('liquidity_shock'))),('FALSE_RELATION',bool(context.get('relation_invalid'))),
            ('EXPERT_OVERCONFIDENCE',float(context.get('confidence',0))>=.7 and not bool(context.get('correct'))),
            ('CALIBRATION_FAILURE',float(context.get('calibration_error',0))>.15),('ENTRY_FAILURE',bool(context.get('entry_failure'))),
            ('EXIT_FAILURE',bool(context.get('exit_failure'))),('MISSING_PROVIDER',not bool(context.get('provider_available',True))),
        ]
        category=next((name for name,matched in checks if matched),'TIMING_FAILURE')
        return {'category':category,'evidence':{key:value for key,value in context.items() if value is not None},'evaluated_categories':list(FAILURE_CATEGORIES)}

    @staticmethod
    def missed(context:dict[str,Any])->dict[str,Any]:
        checks=[('EVENT_NOT_CAPTURED',not context.get('event_captured')),('ASSET_RESOLUTION_FAILED',not context.get('asset_resolved')),('PROVIDER_MISSING',not context.get('provider_available')),('ROUTING_FAILED',not context.get('routed')),('DATA_STALE',bool(context.get('data_stale'))),('EXPERT_WEAK',bool(context.get('expert_weak'))),('META_REJECTED',bool(context.get('meta_rejected'))),('WRONG_HORIZON',bool(context.get('wrong_horizon'))),('THRESHOLD_TOO_HIGH',bool(context.get('threshold_too_high'))),('REGIME_WRONG',bool(context.get('regime_wrong'))),('UNCERTAINTY_TOO_HIGH',bool(context.get('uncertainty_too_high')))]
        cause=next((name for name,matched in checks if matched),'EVENT_NOT_CAPTURED')
        return {'cause':cause,'evidence':{key:value for key,value in context.items() if value is not None},'evaluated_causes':list(MISSED_CAUSES)}
