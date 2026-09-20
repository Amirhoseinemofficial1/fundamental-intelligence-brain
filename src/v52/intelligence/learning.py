from __future__ import annotations
import hashlib,json,math,os,statistics,time
from collections import defaultdict
from typing import Any
from ..config import Settings
from ..storage.sqlite import connect,transaction
from .experts import SPECIALISTS,brier_score,calibration_error
from .model_factory import ModelFactory
from .features import ONCHAIN,DERIVATIVES,MICROSTRUCTURE,FUNDAMENTAL,FEATURE_VERSION

PROFILE_FEATURES=('asset_profile_average_absolute_response','asset_profile_market_beta','asset_profile_btc_beta','asset_profile_macro_sensitivity','asset_profile_listing_sensitivity','asset_profile_unlock_sensitivity','asset_profile_whale_sensitivity','asset_profile_news_sensitivity','asset_profile_liquidity_sensitivity','asset_profile_best_horizon','asset_profile_worst_horizon','asset_profile_fakeout_rate','asset_profile_reversal_rate','asset_profile_reversal_probability','asset_profile_mfe_mean','asset_profile_mfe_p90','asset_profile_mae_mean','asset_profile_mae_p90','asset_profile_entry_style_performance','asset_profile_exit_style_performance','asset_profile_source_sensitivity','asset_profile_sample_count','asset_profile_age_days')
STATIC=('event_importance','event_novelty','source_quality','surprise','horizon_log')+PROFILE_FEATURES
EXPERT_FEATURES={
 'macro':STATIC+FUNDAMENTAL,'central_bank':STATIC+('cpi','ppi','employment','nominal_yield','real_yield','rate_differential','risk_sentiment'),
 'gold':STATIC+('real_yield','nominal_yield','cpi','etf_flow','risk_sentiment','funding_rate','dex_liquidity'),
 'oil':STATIC+('inventory','production','refinery_utilization','risk_sentiment','net_exchange_flow'),
 'forex':STATIC+('cpi','ppi','gdp','pmi','employment','nominal_yield','rate_differential','risk_sentiment'),
 'equity':STATIC+('earnings','eps','revenue','guidance','margin','nominal_yield','risk_sentiment')+MICROSTRUCTURE,
 'crypto':STATIC+ONCHAIN+DERIVATIVES+MICROSTRUCTURE,'token':STATIC+ONCHAIN+DERIVATIVES+MICROSTRUCTURE,
 'listing':STATIC+ONCHAIN+DERIVATIVES+MICROSTRUCTURE,
 'etf':STATIC+('etf_flow','open_interest','basis','risk_sentiment'),'regulation':STATIC+('risk_sentiment',),
 'geopolitical':STATIC+('risk_sentiment','inventory','production'),'energy':STATIC+('inventory','production','refinery_utilization'),
 'news_novelty':STATIC,'onchain':STATIC+ONCHAIN,'derivatives':STATIC+DERIVATIVES,'whale':STATIC+ONCHAIN,
 'liquidity':STATIC+MICROSTRUCTURE+('dex_liquidity','stablecoin_flow'),'regime':STATIC+MICROSTRUCTURE+DERIVATIVES,
 'pump_risk':STATIC+ONCHAIN+DERIVATIVES+MICROSTRUCTURE,'dump_risk':STATIC+ONCHAIN+DERIVATIVES+MICROSTRUCTURE,
 'entry':STATIC+MICROSTRUCTURE+DERIVATIVES+('prediction_probability','predicted_magnitude','predicted_mfe','predicted_mae','current_volatility','current_liquidity','current_regime','event_age','expert_disagreement','current_price_path','historical_analogue_mean','data_quality','asset_profile_fakeout_rate','asset_profile_reversal_rate'),
 'exit':STATIC+MICROSTRUCTURE+DERIVATIVES+('prediction_probability','predicted_magnitude','predicted_mfe','predicted_mae','current_volatility','current_liquidity','current_regime','event_age','expert_disagreement','current_price_path','historical_analogue_mean','data_quality','asset_profile_fakeout_rate','asset_profile_reversal_rate'),
}
EXPERT_FEATURES['exit_policy']=EXPERT_FEATURES['exit']
EXPERT_FEATURES['exit_quality']=EXPERT_FEATURES['exit']
EXPERT_FEATURES['post_exit_optimizer']=EXPERT_FEATURES['exit']

REALIZED_OUTCOME_FIELDS=frozenset({'realized_mfe','realized_mae','realized_return','future_peak','future_exit_quality','mfe','mae','return_value'})

def assert_prediction_time_contract(row:dict[str,Any],values:dict[str,Any])->None:
    leaked=REALIZED_OUTCOME_FIELDS.intersection(values)
    if leaked:raise ValueError(f'future leakage in model features: {sorted(leaked)}')
    available=float(row.get('available_cutoff') or row['prediction_time'])
    decision=float(row['prediction_time'])
    if available>decision+1e-9:raise ValueError(f'feature.available_time {available} exceeds decision_time {decision}')

def specialist_label(expert:str,row:dict[str,Any])->int:
    if expert=='entry':return int(float(row['mfe'])>abs(float(row['mae'])))
    if expert=='exit':return int(abs(float(row['mae']))>abs(float(row['mfe']))*.75)
    if expert=='pump_risk':return int(float(row['return_value'])>=float(os.getenv('PUMP_RETURN_THRESHOLD','.03')))
    if expert=='dump_risk':return int(float(row['return_value'])<=-float(os.getenv('DUMP_RETURN_THRESHOLD','.03')))
    return int(row['label'])

def regime_transition_label(current:str,future:str)->int:return int(str(current)!=str(future))

class TrainingCoordinator:
    """Outcome -> versioned dataset -> OOS candidate -> unseen shadow -> promotion."""
    def __init__(self,settings:Settings):
        self.settings=settings;self.path=settings.data_dir/'brain'/'intelligence.sqlite3';self.factory=ModelFactory(settings)
        self.minimum_rows=max(20,int(os.getenv('MODEL_MIN_TRAIN_ROWS','60')));self.minimum_shadow=max(10,int(os.getenv('MODEL_MIN_SHADOW_OUTCOMES','30')))
    def signal_learning_rows(self):
        limit=max(100,min(20000,int(os.getenv('MODEL_MAX_TRAIN_ROWS','2000'))))
        with connect(self.path,self.settings) as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='signal_outcomes'").fetchone():return []
            rows=con.execute("""SELECT fv.*,a.asset_class,e.domain,e.event_type,e.created_at event_created_at,
                e.historical_analogues_json,mr.return_value,mr.mfe,mr.mae,mr.volatility,
                mr.time_to_peak_seconds,mr.time_to_mfe_seconds,mr.time_to_mae_seconds,mr.measured_at label_available_at,
                p.direction_probability,p.expected_magnitude,p.uncertainty,p.evidence_json prediction_evidence,
                CASE WHEN mr.return_value>0 THEN 1 ELSE 0 END label
                FROM signal_outcomes mr JOIN feature_vectors fv USING(feature_id)
                JOIN predictions p ON p.id=mr.prediction_id JOIN assets a ON a.asset_id=fv.asset_id JOIN events e ON e.id=fv.event_id
                WHERE fv.replay=0 AND fv.available_cutoff<=fv.prediction_time AND mr.measured_at<=?
                  AND fv.feature_version=?
                  AND e.title NOT LIKE 'PREDICTION MARKET %'
                  AND e.title!='SEC CURRENT FILING'
                ORDER BY fv.prediction_time DESC LIMIT ?""",(time.time(),FEATURE_VERSION,limit)).fetchall()
        return [{**dict(r),'values':json.loads(r['values_json']),'prediction_evidence_parsed':json.loads(r['prediction_evidence'] or '{}')} for r in reversed(rows)]

    def learning_rows(self)->list[dict[str,Any]]:
        if os.getenv('MODEL_SIGNAL_LABELS_ONLY','0')=='1':return self.signal_learning_rows()
        limit=max(100,min(20000,int(os.getenv('MODEL_MAX_TRAIN_ROWS','2000'))))
        with connect(self.path,self.settings) as con:
            rows=con.execute("""SELECT fv.feature_id,fv.prediction_time,fv.available_cutoff,fv.quality,fv.feature_version,fv.values_json,fv.asset_id,fv.horizon_seconds,
                  a.asset_class,e.domain,e.event_type,e.created_at event_created_at,e.historical_analogues_json,
                  mr.return_value,mr.mfe,mr.mae,mr.time_to_peak_seconds,mr.time_to_mfe_seconds,mr.time_to_mae_seconds,mr.volatility,
                  p.direction_probability,p.expected_magnitude,p.uncertainty,p.evidence_json prediction_evidence,
                  (SELECT r1.regime FROM regimes r1 WHERE r1.asset_id=fv.asset_id AND r1.observed_at<=fv.prediction_time ORDER BY r1.observed_at DESC LIMIT 1) current_regime_label,
                  (SELECT r2.regime FROM regimes r2 WHERE r2.asset_id=fv.asset_id AND r2.observed_at<=fv.prediction_time+fv.horizon_seconds ORDER BY r2.observed_at DESC LIMIT 1) future_regime_label,
                  CASE WHEN mr.return_value>0 THEN 1 ELSE 0 END label
              FROM feature_vectors fv JOIN events e ON e.id=fv.event_id JOIN assets a ON a.asset_id=fv.asset_id
              JOIN market_responses mr ON mr.event_id=fv.event_id AND mr.asset_id=fv.asset_id AND mr.horizon_seconds=fv.horizon_seconds
              LEFT JOIN predictions p ON p.id=(SELECT px.id FROM predictions px WHERE px.event_id=fv.event_id AND px.asset_id=fv.asset_id AND px.horizon_seconds=fv.horizon_seconds AND json_extract(px.evidence_json,'$.feature_id')=fv.feature_id ORDER BY px.id LIMIT 1)
              WHERE fv.replay=0 AND fv.prediction_time<=(SELECT event_epoch+15 FROM response_targets WHERE id=mr.target_id)
              AND fv.available_cutoff<=fv.prediction_time AND mr.measured_at<=?
              ORDER BY fv.prediction_time DESC,fv.feature_id DESC LIMIT ?""",(time.time(),limit)).fetchall()
        result=[]
        for raw in reversed(rows):
            row={**dict(raw),'values':json.loads(raw['values_json'])}
            try:row['prediction_evidence_parsed']=json.loads(row.get('prediction_evidence') or '{}')
            except (TypeError,ValueError):row['prediction_evidence_parsed']={}
            result.append(row)
        return result

    @staticmethod
    def _policy_values(r:dict[str,Any])->dict[str,float]:
        base=r['values'];evidence=r.get('prediction_evidence_parsed') or {};forecast=((evidence.get('inference') or {}).get('decision_models') or {})
        analogues=[]
        try:analogues=json.loads(r.get('historical_analogues_json') or '[]')
        except (TypeError,ValueError):analogues=[]
        values={name:float(base.get(name,0.0) or 0.0) for name in EXPERT_FEATURES['entry'] if name in base}
        values.update({
            'prediction_probability':float(r.get('direction_probability') if r.get('direction_probability') is not None else .5),
            'predicted_magnitude':float(forecast.get('expected_return',r.get('expected_magnitude') or 0.0)),
            'predicted_mfe':float(forecast.get('expected_mfe',0.0)),
            'predicted_mae':float(forecast.get('expected_mae',0.0)),
            'current_volatility':float(base.get('volatility',0.0) or 0.0),
            'current_liquidity':float(base.get('dex_liquidity',base.get('depth',0.0)) or 0.0),
            'current_regime':float(base.get('regime_probability',0.0) or 0.0),
            'event_age':max(0.0,float(r['prediction_time'])-float(r.get('event_created_at') or r['prediction_time'])),
            'expert_disagreement':float(r.get('uncertainty') or 0.0),
            'current_price_path':float(base.get('price_path_return',0.0) or 0.0),
            'historical_analogue_mean':sum(float(x.get('return',0.0)) for x in analogues if isinstance(x,dict))/max(1,len(analogues)),
            'data_quality':float(r.get('quality') or 0.0),
        })
        return values
    def _rows_for(self,expert:str,rows:list[dict[str,Any]])->list[dict[str,Any]]:
        if expert in {'meta','entry','exit'}:return rows
        spec=next((x for x in SPECIALISTS if x.name==expert),None)
        return [r for r in rows if spec and r['domain'] in spec.domains and r['asset_class'] in spec.asset_classes and (not spec.event_prefixes or any(str(r['event_type']).startswith(x) for x in spec.event_prefixes))]
    def _budget_full(self, created)->bool:
        return len(created)>=max(1,int(os.getenv("MODEL_MAX_NEW_PER_STAGE","2")))

    def _recent_training(self,expert:str)->bool:
        with connect(self.path,self.settings) as con:
            row=con.execute("SELECT MAX(created_at) t FROM model_artifacts WHERE expert=?",(expert,)).fetchone()
        return bool(row['t'] and time.time()-float(row['t'])<max(60,float(os.getenv("MODEL_RETRAIN_MIN_SECONDS","21600"))))

    def train_eligible(self)->list[dict[str,Any]]:
        rows=self.learning_rows();created=[]
        for expert in [x.name for x in SPECIALISTS]+['entry','exit']:
            if self._budget_full(created):break
            if self._recent_training(expert):continue
            subset=self._rows_for(expert,rows)
            if len(subset)<self.minimum_rows:continue
            with connect(self.path,self.settings) as con:
                if con.execute("SELECT 1 FROM model_artifacts WHERE expert=? AND state='SHADOW'",(expert,)).fetchone():continue
            model_rows=[]
            for r in subset:
                allowed=EXPERT_FEATURES.get(expert,tuple(r['values']))
                values=self._policy_values(r) if expert in {'entry','exit'} else {name:float(r['values'].get(name,0) or 0) for name in allowed}
                label=specialist_label(expert,r)
                assert_prediction_time_contract(r,values)
                model_rows.append({'prediction_time':r['prediction_time'],'available_cutoff':r['available_cutoff'],'label_available_at':float(r.get('label_available_at') or (float(r['prediction_time'])+int(r['horizon_seconds']))),'values':values,'label':label,'feature_version':r['feature_version']})
            canonical=json.dumps([{'t':r['prediction_time'],'v':r['values'],'y':r['label']} for r in model_rows],sort_keys=True,separators=(',',':')).encode()
            dataset_sha=hashlib.sha256(canonical).hexdigest()
            with connect(self.path,self.settings) as con:
                if con.execute("SELECT 1 FROM model_artifacts ma JOIN datasets d ON d.dataset_id=ma.dataset_id WHERE ma.expert=? AND d.sha256=?",(expert,dataset_sha)).fetchone():continue
            try:created.append(self.factory.train(expert,model_rows,state='SHADOW'))
            except ValueError:continue
        return created
    def train_decision_eligible(self)->list[dict[str,Any]]:
        """Train independent continuous/risk targets from resolved response labels."""
        rows=self.learning_rows();created=[]
        targets=(
            ('magnitude_return','return_value',lambda value:abs(float(value))),
            ('magnitude_mfe','mfe',lambda value:abs(float(value))),
            ('magnitude_mae','mae',lambda value:abs(float(value))),
            ('time_to_peak','time_to_peak_seconds',lambda value:max(0.0,float(value))),
        )
        for expert,column,transform in targets:
            if self._budget_full(created):break
            if self._recent_training(expert):continue
            subset=[r for r in rows if r.get(column) is not None]
            if len(subset)<self.minimum_rows:continue
            with connect(self.path,self.settings) as con:
                if con.execute("SELECT 1 FROM model_artifacts WHERE expert=? AND state='SHADOW'",(expert,)).fetchone():continue
            model_rows=[]
            for r in subset:
                values={str(k):float(v) for k,v in r['values'].items() if isinstance(v,(int,float))}
                values[f"asset_class_{str(r['asset_class']).lower()}"]=1.0
                assert_prediction_time_contract(r,values);model_rows.append({'prediction_time':r['prediction_time'],'available_cutoff':r['available_cutoff'],'label_available_at':float(r.get('label_available_at') or (float(r['prediction_time'])+int(r['horizon_seconds']))),'values':values,'label':transform(r[column]),'feature_version':r['feature_version']})
            try:created.append(self.factory.train_regression(expert,model_rows,state='SHADOW'))
            except ValueError:continue
        if self._budget_full(created):return created
        risk_rows=[]
        for r in rows:
            values={str(k):float(v) for k,v in r['values'].items() if isinstance(v,(int,float))};values[f"asset_class_{str(r['asset_class']).lower()}"]=1.0
            adverse=abs(float(r['mae']))>max(.005,abs(float(r['mfe']))*.75)
            assert_prediction_time_contract(r,values);risk_rows.append({'prediction_time':r['prediction_time'],'available_cutoff':r['available_cutoff'],'label_available_at':float(r.get('label_available_at') or (float(r['prediction_time'])+int(r['horizon_seconds']))),'values':values,'label':int(adverse),'feature_version':r['feature_version']})
        with connect(self.path,self.settings) as con:risk_shadow=con.execute("SELECT 1 FROM model_artifacts WHERE expert='risk' AND state='SHADOW'").fetchone()
        if len(risk_rows)>=self.minimum_rows and not risk_shadow and not self._recent_training('risk'):
            try:created.append(self.factory.train('risk',risk_rows,state='SHADOW'))
            except ValueError as exc:created.append({'expert':'risk','status':'NOT_QUALIFIED','reason':str(exc)})
        regime_rows=[]
        by_asset=defaultdict(list)
        for r in rows:by_asset[str(r['asset_id'])].append(r)
        for asset_rows in by_asset.values():
          asset_rows.sort(key=lambda item:float(item['prediction_time']))
          for index,r in enumerate(asset_rows[:-1]):
            values={str(k):float(v) for k,v in r['values'].items() if isinstance(v,(int,float))};values[f"asset_class_{str(r['asset_class']).lower()}"]=1.0
            current=str(r.get('current_regime_label') or r['values'].get('regime_label') or r['values'].get('current_regime') or 'UNKNOWN')
            future=str(r.get('future_regime_label') or asset_rows[index+1].get('current_regime_label') or asset_rows[index+1]['values'].get('regime_label') or asset_rows[index+1]['values'].get('current_regime') or 'UNKNOWN')
            assert_prediction_time_contract(r,values);regime_rows.append({'prediction_time':r['prediction_time'],'available_cutoff':r['available_cutoff'],'label_available_at':float(r.get('label_available_at') or (float(r['prediction_time'])+int(r['horizon_seconds']))),'values':values,'label':regime_transition_label(current,future),'feature_version':r['feature_version'],'regime_before':current,'regime_after':future})
        with connect(self.path,self.settings) as con:regime_shadow=con.execute("SELECT 1 FROM model_artifacts WHERE expert='regime_transition' AND state='SHADOW'").fetchone()
        if len(regime_rows)>=self.minimum_rows and not regime_shadow and not self._budget_full(created) and not self._recent_training('regime_transition'):
            try:created.append(self.factory.train('regime_transition',regime_rows,state='SHADOW'))
            except ValueError as exc:created.append({'expert':'regime_transition','status':'NOT_QUALIFIED','reason':str(exc)})
        for horizon in sorted({int(r['horizon_seconds']) for r in rows}):
            if self._budget_full(created):break
            expert=f'horizon_score:{horizon}'
            if self._recent_training(expert):continue
            subset=[r for r in rows if int(r['horizon_seconds'])==horizon]
            if len(subset)<self.minimum_rows:continue
            with connect(self.path,self.settings) as con:
                if con.execute("SELECT 1 FROM model_artifacts WHERE expert=? AND state='SHADOW'",(expert,)).fetchone():continue
            model_rows=[]
            for r in subset:
                values={str(k):float(v) for k,v in r['values'].items() if isinstance(v,(int,float))};values[f"asset_class_{str(r['asset_class']).lower()}"]=1.0
                score=abs(float(r['return_value']))-.25*abs(float(r['mae']))
                assert_prediction_time_contract(r,values);model_rows.append({'prediction_time':r['prediction_time'],'available_cutoff':r['available_cutoff'],'label_available_at':float(r.get('label_available_at') or (float(r['prediction_time'])+int(r['horizon_seconds']))),'values':values,'label':score,'feature_version':r['feature_version']})
            try:created.append(self.factory.train_regression(expert,model_rows,horizon=horizon,state='SHADOW'))
            except ValueError:continue
        return created
    def train_meta_eligible(self)->dict[str,Any]|None:
        strict=os.getenv('MODEL_SIGNAL_LABELS_ONLY','0')=='1'
        with connect(self.path,self.settings) as con:
            if con.execute("SELECT 1 FROM model_artifacts WHERE expert='meta' AND state='SHADOW'").fetchone():return None
            outcome_join="JOIN signal_outcomes mr ON mr.feature_id=fv.feature_id" if strict else "JOIN market_responses mr ON mr.event_id=fv.event_id AND mr.asset_id=fv.asset_id AND mr.horizon_seconds=fv.horizon_seconds"
            if strict and not con.execute("SELECT 1 FROM sqlite_master WHERE name='signal_outcomes'").fetchone():return None
            rows=con.execute(f"""SELECT fv.feature_id,fv.prediction_time,fv.horizon_seconds,fv.feature_version,eo.expert,eo.probability,mr.measured_at label_available_at,
                   CASE WHEN mr.return_value>0 THEN 1 ELSE 0 END label,fv.quality
              FROM feature_vectors fv JOIN expert_outputs eo ON eo.feature_id=fv.feature_id AND eo.model_state='CHAMPION'
              {outcome_join}
              WHERE fv.replay=0 ORDER BY fv.prediction_time DESC LIMIT 20000""").fetchall()
        grouped={}
        for raw in rows:
            row=dict(raw)
            item=grouped.setdefault(row['feature_id'],{'prediction_time':row['prediction_time'],'available_cutoff':row['prediction_time'],'label_available_at':float(row.get('label_available_at') or (float(row['prediction_time'])+int(row['horizon_seconds']))),'feature_version':row['feature_version'],'label':row['label'],'values':{'data_quality':row['quality']}})
            item['values'][f"expert_{row['expert']}"]=float(row['probability'])
        training=list(grouped.values())
        if len(training)<self.minimum_rows:return None
        return self.factory.train('meta',training,state='SHADOW')
    def train_temporal_eligible(self)->list[dict[str,Any]]:
        if os.getenv("TEMPORAL_TRAINING_ENABLED", "0") != "1":return []
        from .temporal_governance import TemporalGovernance
        rows=self.learning_rows();training=[]
        for row in rows:
            values={str(k):float(v) for k,v in row['values'].items() if isinstance(v,(int,float))}
            training.append({'prediction_time':row['prediction_time'],'available_cutoff':row['available_cutoff'],'label_available_at':float(row.get('label_available_at') or (float(row['prediction_time'])+int(row['horizon_seconds']))),'feature_version':row['feature_version'],'values':values,'label':int(row['label'])})
        return TemporalGovernance(self.settings).train(training)
    def train_exit_from_post_exit(self)->dict[str,Any]|None:
        """Train three semantically separate governed Exit families.

        ``exit_policy`` learns whether the active state warranted risk removal,
        ``exit_quality`` learns GOOD_EXIT versus other named quality classes, and
        ``post_exit_optimizer`` regresses the direction-normalised post-exit
        return.  The legacy ``exit`` artifact is produced only for V55 runtime
        compatibility and has the same target as ``exit_policy``.
        """
        with connect(self.path,self.settings) as con:
            rows=con.execute("""SELECT pe.signal_id,pe.classification,pe.return_after_exit,pe.created_at label_available_at,
                ass.observed_at prediction_time,ass.evidence_json
                FROM post_exit pe JOIN active_signal_snapshots ass ON ass.id=(
                    SELECT id FROM active_signal_snapshots x WHERE x.signal_id=pe.signal_id
                    AND x.observed_at<=(SELECT closed_at FROM signals WHERE id=pe.signal_id)
                    ORDER BY x.observed_at DESC LIMIT 1)
                ORDER BY ass.observed_at DESC,pe.signal_id,pe.horizon_seconds LIMIT ?""",(max(100,min(20000,int(os.getenv("MODEL_MAX_TRAIN_ROWS","2000")))),)).fetchall()
        policy_training=[];quality_training=[];optimizer_training=[]
        for row in rows:
            evidence=json.loads(row['evidence_json'] or '{}');values=evidence.get('exit_features') or {}
            values={str(k):float(v) for k,v in values.items() if isinstance(v,(int,float))}
            if not values:continue
            contract={'prediction_time':float(row['prediction_time']),'available_cutoff':float(row['prediction_time'])}
            assert_prediction_time_contract(contract,values)
            classification=str(row['classification'])
            common={'prediction_time':float(row['prediction_time']),'available_cutoff':float(row['prediction_time']),
                'label_available_at':float(row['label_available_at']),'values':values,'feature_version':'active-exit-57.3.0',
                'label_text':classification}
            policy_training.append({**common,'label':int(classification in {'GOOD_EXIT','LOSS_AVOIDED','LATE_EXIT'})})
            quality_training.append({**common,'label':int(classification=='GOOD_EXIT')})
            optimizer_training.append({**common,'label':float(row['return_after_exit'])})
        if len(policy_training)<self.minimum_rows:return {'expert':'exit_policy','status':'NOT_QUALIFIED','reason':'insufficient_post_exit_rows','rows':len(policy_training)}
        created=[]
        with connect(self.path,self.settings) as con:
            existing={str(r['expert']) for r in con.execute("SELECT expert FROM model_artifacts WHERE expert IN ('exit','exit_policy','exit_quality','post_exit_optimizer') AND state='SHADOW'").fetchall()}
        for expert,training,label_version,regression in (
            ('exit_policy',policy_training,'exit-action-v1',False),
            ('exit_quality',quality_training,'exit-quality-enum-v1:positive=GOOD_EXIT',False),
            ('post_exit_optimizer',optimizer_training,'post-exit-direction-normalized-return-v1',True),
            ('exit',[{**item,'feature_version':'active-exit-55.0.0'} for item in policy_training],'exit-action-v1-legacy-alias',False),
        ):
            if expert in existing:continue
            try:
                result=(self.factory.train_regression(expert,training,state='SHADOW',label_version=label_version)
                        if regression else self.factory.train(expert,training,state='SHADOW',label_version=label_version))
                created.append(result)
            except ValueError as exc:
                created.append({'expert':expert,'status':'NOT_QUALIFIED','reason':str(exc),'rows':len(training)})
        legacy=next((item for item in created if item.get('artifact_id','').startswith('artifact:exit:')),None)
        primary=next((item for item in created if item.get('artifact_id','').startswith('artifact:exit_policy:')),None)
        result=legacy or primary or (created[0] if created else None)
        if result is not None:result={**result,'exit_family_results':created}
        return result
    def resolve_shadow_outcomes(self)->int:
        with connect(self.path,self.settings) as con:
            outcome_join="LEFT JOIN market_responses mr ON mr.event_id=fv.event_id AND mr.asset_id=fv.asset_id AND mr.horizon_seconds=fv.horizon_seconds"
            if os.getenv("MODEL_SIGNAL_LABELS_ONLY","0")=="1":
                if not con.execute("SELECT 1 FROM sqlite_master WHERE name='signal_outcomes'").fetchone():return 0
                outcome_join="LEFT JOIN signal_outcomes mr ON mr.feature_id=fv.feature_id"
            rows=con.execute(f"""SELECT sp.id,sp.target_type,ma.expert,mr.return_value,mr.mfe,mr.mae,mr.time_to_peak_seconds,
                pe.classification exit_classification,pe.return_after_exit,
                (SELECT r.regime FROM regimes r WHERE r.asset_id=fv.asset_id AND r.observed_at>=fv.prediction_time ORDER BY r.observed_at LIMIT 1) actual_regime
                FROM shadow_predictions sp JOIN model_artifacts ma ON ma.artifact_id=sp.artifact_id
                JOIN feature_vectors fv ON fv.feature_id=sp.feature_id
                {outcome_join}
                LEFT JOIN post_exit pe ON pe.signal_id=sp.signal_id AND pe.horizon_seconds=(SELECT MAX(px.horizon_seconds) FROM post_exit px WHERE px.signal_id=sp.signal_id)
                WHERE sp.resolved_at IS NULL AND (mr.id IS NOT NULL OR pe.signal_id IS NOT NULL)""").fetchall();now=time.time()
            for row in rows:
                expert=str(row['expert'])
                exit_classification=str(row['exit_classification'] or '')
                if expert in {'exit','exit_policy'}:
                    outcome=float(row['return_after_exit'] or 0.0);label=int(exit_classification in {'GOOD_EXIT','LOSS_AVOIDED','LATE_EXIT'})
                elif expert=='exit_quality':
                    outcome=float(row['return_after_exit'] or 0.0);label=int(exit_classification=='GOOD_EXIT')
                elif expert=='post_exit_optimizer':
                    outcome=float(row['return_after_exit'] or 0.0);label=None
                elif expert=='magnitude_return':outcome=abs(float(row['return_value']))
                elif expert=='magnitude_mfe':outcome=abs(float(row['mfe']))
                elif expert=='magnitude_mae':outcome=abs(float(row['mae']))
                elif expert=='time_to_peak':outcome=max(0.0,float(row['time_to_peak_seconds'] or 0))
                elif expert.startswith('horizon_score:'):outcome=abs(float(row['return_value']))-.25*abs(float(row['mae']))
                else:outcome=float(row['return_value'])
                if expert not in {'exit','exit_policy','exit_quality','post_exit_optimizer'}:
                    if expert.startswith('regime_classifier:'):label=int(str(row['actual_regime'] or '').upper()==expert.split(':',1)[1].upper())
                    elif expert=='risk':label=int(abs(float(row['mae']))>max(.005,abs(float(row['mfe']))*.75))
                    elif expert=='pump_risk':label=int(float(row['return_value'])>=float(os.getenv('PUMP_RETURN_THRESHOLD','.03')))
                    elif expert=='dump_risk':label=int(float(row['return_value'])<=-float(os.getenv('DUMP_RETURN_THRESHOLD','.03')))
                    else:label=int(float(row['return_value'])>0)
                con.execute('UPDATE shadow_predictions SET outcome=?,label=?,label_text=?,resolved_at=? WHERE id=?',
                    (outcome,label,exit_classification or None,now,row['id']))
            return len(rows)
    def evaluate_shadows(self)->list[dict[str,Any]]:
        results=[]
        with connect(self.path,self.settings) as con:
            artifacts=con.execute("SELECT * FROM model_artifacts WHERE state='SHADOW'").fetchall()
            for artifact in artifacts:
                rows=con.execute('SELECT probability,label,outcome FROM shadow_predictions WHERE artifact_id=? AND resolved_at IS NOT NULL ORDER BY resolved_at',(artifact['artifact_id'],)).fetchall()
                if len(rows)<self.minimum_shadow:continue
                champion=con.execute("SELECT * FROM model_artifacts WHERE expert=? AND state='CHAMPION'",(artifact['expert'],)).fetchone();baseline=.25
                metadata=json.loads(open(str(artifact['artifact_path']),'r',encoding='utf-8').read());regression=metadata.get('task')=='regression'
                if regression:
                    p=[float(r['probability']) for r in rows];y=[float(r['outcome']) for r in rows];metric=sum(abs(a-b) for a,b in zip(p,y))/len(y);ece=0.0
                    baseline=sum(abs(value-(sum(y)/len(y))) for value in y)/len(y)
                    if champion:
                        cp=con.execute("SELECT eo.probability,sp.outcome FROM shadow_predictions sp JOIN expert_outputs eo ON eo.feature_id=sp.feature_id AND eo.artifact_id=? WHERE sp.artifact_id=? AND sp.resolved_at IS NOT NULL",(champion['artifact_id'],artifact['artifact_id'])).fetchall()
                        if len(cp)>=self.minimum_shadow:baseline=sum(abs(float(r['probability'])-float(r['outcome'])) for r in cp)/len(cp)
                    improvement=baseline-metric;qualified=math.isfinite(metric) and improvement>=max(1e-9,baseline*.005);metric_name='shadow_mae'
                else:
                    p=[float(r['probability']) for r in rows];y=[int(r['label']) for r in rows];metric=brier_score(p,y);ece=calibration_error(p,y)
                    if champion:
                        cp=con.execute("SELECT eo.probability,sp.label FROM shadow_predictions sp JOIN expert_outputs eo ON eo.feature_id=sp.feature_id AND eo.artifact_id=? WHERE sp.artifact_id=? AND sp.resolved_at IS NOT NULL",(champion['artifact_id'],artifact['artifact_id'])).fetchall()
                        if len(cp)>=self.minimum_shadow:baseline=brier_score([float(r['probability']) for r in cp],[int(r['label']) for r in cp])
                    improvement=baseline-metric;qualified=metric<=.245 and ece<=.18 and improvement>=.002;metric_name='shadow_brier'
                transaction(con)
                try:
                    now=time.time()
                    if qualified:
                        if champion:con.execute("UPDATE model_artifacts SET state='RETIRED',retired_at=? WHERE artifact_id=?",(now,champion['artifact_id']))
                        con.execute("UPDATE model_artifacts SET state='CHAMPION',promoted_at=? WHERE artifact_id=?",(now,artifact['artifact_id']))
                        action='ARTIFACT_PROMOTED'
                    else:con.execute("UPDATE model_artifacts SET state='REJECTED' WHERE artifact_id=?",(artifact['artifact_id'],));action='ARTIFACT_REJECTED'
                    con.execute("INSERT INTO improvement_records(metric,before_value,after_value,sample_size,validation_window,model_version,created_at) VALUES(?,?,?,?,?,?,?)",(metric_name,baseline,metric,len(rows),'unseen_runtime_shadow',artifact['artifact_id'],now))
                    con.execute("INSERT INTO audit_log(actor,action,object_type,object_id,detail_json,created_at) VALUES('training',?,'artifact',?,?,?)",(action,artifact['artifact_id'],json.dumps({'metric':metric_name,'value':metric,'ece':ece,'baseline':baseline,'samples':len(rows)}),now));con.execute('COMMIT')
                except BaseException:con.execute('ROLLBACK');raise
                results.append({'artifact_id':artifact['artifact_id'],'expert':artifact['expert'],'promoted':qualified,'metric':metric_name,'value':metric,'brier':metric if not regression else None,'mae':metric if regression else None,'ece':ece,'baseline':baseline,'samples':len(rows)})
        return results

class PolicyEngine:
    def __init__(self,settings:Settings):self.factory=ModelFactory(settings)
    def decide(self,feature:dict[str,Any],probability:float,forecast:dict[str,Any]|None=None)->dict[str,Any]:
        forecast=forecast or {};base=feature['values'];values={name:float(base.get(name,0.0) or 0.0) for name in EXPERT_FEATURES['entry'] if name in base}
        values.update({'prediction_probability':probability,'predicted_magnitude':float(forecast.get('expected_return',0.0)),'predicted_mfe':float(forecast.get('expected_mfe',0.0)),'predicted_mae':float(forecast.get('expected_mae',0.0)),'current_volatility':float(base.get('volatility',0.0) or 0.0),'current_liquidity':float(base.get('dex_liquidity',base.get('depth',0.0)) or 0.0),'current_regime':float(base.get('regime_probability',0.0) or 0.0),'event_age':float(base.get('event_age',0.0) or 0.0),'expert_disagreement':float(forecast.get('magnitude_uncertainty',0.0)),'current_price_path':float(base.get('price_path_return',0.0) or 0.0),'historical_analogue_mean':float(base.get('historical_analogue_mean',0.0) or 0.0),'data_quality':float(feature.get('quality',0.0) or 0.0)})
        if REALIZED_OUTCOME_FIELDS.intersection(values):raise RuntimeError('realized outcomes cannot enter runtime policy features')
        entry=self.factory.registry.champion('entry');exit_model=self.factory.registry.champion('exit_policy') or self.factory.registry.champion('exit')
        ep=entry.predict(values) if entry else None;xp=exit_model.predict(values) if exit_model else None
        style='IMMEDIATE' if ep is not None and ep>=.65 else 'PULLBACK' if ep is not None and ep>=.55 else 'CONFIRMATION' if abs(probability-.5)>=.12 else 'WAIT'
        return {'entry_style':style,'entry_artifact':entry.metadata['artifact_id'] if entry else 'BOOTSTRAP_FALLBACK','exit_probability':xp,'exit_artifact':exit_model.metadata['artifact_id'] if exit_model else 'BOOTSTRAP_FALLBACK'}
