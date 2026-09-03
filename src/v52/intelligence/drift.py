from __future__ import annotations

import json
import math
import statistics
import time
from typing import Any

from ..config import Settings
from ..storage.sqlite import connect

DRIFT_TYPES=("DATA_DRIFT","FEATURE_DRIFT","CONCEPT_DRIFT","SOURCE_DRIFT","PREDICTION_DRIFT","CALIBRATION_DRIFT","REGIME_DRIFT","ASSET_BEHAVIOR_DRIFT")
DRIFT_ACTIONS={
    "DATA_DRIFT":"INCREASE_UNCERTAINTY","FEATURE_DRIFT":"RETRAIN_AFFECTED_MODELS","CONCEPT_DRIFT":"SHADOW_AFFECTED_MODELS",
    "SOURCE_DRIFT":"DEGRADE_AFFECTED_SOURCE_WEIGHT","PREDICTION_DRIFT":"SHADOW_AFFECTED_MODELS","CALIBRATION_DRIFT":"RECALIBRATE_AFFECTED_MODELS",
    "REGIME_DRIFT":"INCREASE_REGIME_UNCERTAINTY","ASSET_BEHAVIOR_DRIFT":"REBUILD_AFFECTED_ASSET_PROFILE",
}

class DriftDataCollector:
    """Build drift windows exclusively from persisted runtime observations."""
    def __init__(self,settings:Settings):self.settings=settings;self.path=settings.data_dir/'brain'/'intelligence.sqlite3'
    def windows(self,limit:int=400)->tuple[dict[str,list[Any]],dict[str,list[Any]],dict[str,Any]]:
        with connect(self.path,self.settings) as con:
            rows=[dict(r) for r in con.execute("""SELECT fv.asset_id,fv.values_json,fv.quality,p.direction_probability,
                mr.return_value,CASE WHEN mr.return_value>0 THEN 1 ELSE 0 END label,
                COALESCE((SELECT regime FROM regimes r WHERE r.asset_id=fv.asset_id AND r.observed_at<=fv.prediction_time ORDER BY observed_at DESC LIMIT 1),'UNKNOWN') regime
                FROM feature_vectors fv JOIN predictions p ON p.event_id=fv.event_id AND p.asset_id=fv.asset_id AND p.horizon_seconds=fv.horizon_seconds
                JOIN market_responses mr ON mr.event_id=fv.event_id AND mr.asset_id=fv.asset_id AND mr.horizon_seconds=fv.horizon_seconds
                WHERE fv.replay=0 ORDER BY fv.prediction_time DESC LIMIT ?""",(limit,)).fetchall()]
            sources=[dict(r) for r in con.execute("SELECT source_id,reliability,CASE WHEN latency_samples>0 THEN latency_sum/latency_samples END latency FROM source_profiles ORDER BY updated_at DESC LIMIT ?",(limit,)).fetchall()]
        rows.reverse();mid=len(rows)//2
        def pack(items:list[dict[str,Any]],source_items:list[dict[str,Any]])->dict[str,Any]:
            feature_distributions:dict[str,list[float|None]]={};missing=[]
            for row in items:
                values=json.loads(row['values_json']);missing.append(None if float(row['quality'])<.01 else float(row['quality']))
                for name,value in values.items():
                    if name.endswith(('__valid','__missing')):continue
                    if isinstance(value,(int,float)) and math.isfinite(float(value)):feature_distributions.setdefault(str(name),[]).append(float(value))
            return {'data':missing,'features':feature_distributions,'labels':[x['label'] for x in items],
                    'predictions':[x['direction_probability'] for x in items],
                    'source_quality':[x['reliability'] for x in source_items],'source_latency':[x['latency'] for x in source_items],
                    'regimes':[x['regime'] for x in items],'asset_returns':[x['return_value'] for x in items]}
        smid=len(sources)//2
        scope={'assets':sorted({str(x['asset_id']) for x in rows[mid:]}),'sources':sorted({str(x['source_id']) for x in sources[smid:]})}
        return pack(rows[:mid],sources[:smid]),pack(rows[mid:],sources[smid:]),scope

class DriftEngine:
    def __init__(self,settings:Settings):self.settings=settings;self.path=settings.data_dir/'brain'/'intelligence.sqlite3'
    @staticmethod
    def _mean(values:list[Any])->float:
        clean=[float(x) for x in values if x is not None and math.isfinite(float(x))];return statistics.mean(clean) if clean else 0.0
    @staticmethod
    def _missing(values:list[Any])->float:return sum(x is None for x in values)/max(1,len(values))
    @staticmethod
    def _distribution_shift(a:list[Any],b:list[Any])->float:
        names=set(map(str,a))|set(map(str,b));return .5*sum(abs(sum(str(x)==name for x in a)/max(1,len(a))-sum(str(x)==name for x in b)/max(1,len(b))) for name in names)
    def _feature_shift(self,baseline:Any,recent:Any)->tuple[float,dict[str,float]]:
        if not isinstance(baseline,dict) or not isinstance(recent,dict):
            return abs(self._mean(recent or [])-self._mean(baseline or [])),{'legacy_aggregate':abs(self._mean(recent or [])-self._mean(baseline or []))}
        scores={}
        for name in sorted(set(baseline)|set(recent)):
            old=[float(x) for x in baseline.get(name,[]) if x is not None]
            new=[float(x) for x in recent.get(name,[]) if x is not None]
            if not old or not new:continue
            scale=max(statistics.pstdev(old+new),abs(self._mean(old)),abs(self._mean(new)),1e-9)
            scores[name]=min(5.0,abs(self._mean(new)-self._mean(old))/scale)
        return (max(scores.values()) if scores else 0.0),scores
    def evaluate(self,baseline:dict[str,list[Any]],recent:dict[str,list[Any]],persist:bool=True,scope:dict[str,Any]|None=None)->dict[str,Any]:
        old_label=baseline.get('labels',[]);new_label=recent.get('labels',[]);old_pred=baseline.get('predictions',[]);new_pred=recent.get('predictions',[])
        old_brier=self._mean([(float(p)-float(y))**2 for p,y in zip(old_pred,old_label)]);new_brier=self._mean([(float(p)-float(y))**2 for p,y in zip(new_pred,new_label)])
        feature_shift,feature_scores=self._feature_shift(baseline.get('features',{}),recent.get('features',{}))
        metrics={
            'DATA_DRIFT':abs(self._missing(recent.get('data',[]))-self._missing(baseline.get('data',[]))),
            'FEATURE_DRIFT':feature_shift,
            'CONCEPT_DRIFT':abs(self._mean(new_label)-self._mean(old_label)),
            'SOURCE_DRIFT':max(abs(self._mean(recent.get('source_quality',[]))-self._mean(baseline.get('source_quality',[]))),abs(self._mean(recent.get('source_latency',[]))-self._mean(baseline.get('source_latency',[])))/3600),
            'PREDICTION_DRIFT':abs(self._mean(new_pred)-self._mean(old_pred)),
            'CALIBRATION_DRIFT':max(0.0,new_brier-old_brier),
            'REGIME_DRIFT':self._distribution_shift(baseline.get('regimes',[]),recent.get('regimes',[])),
            'ASSET_BEHAVIOR_DRIFT':abs(self._mean(recent.get('asset_returns',[]))-self._mean(baseline.get('asset_returns',[]))),
        }
        thresholds={'DATA_DRIFT':.15,'FEATURE_DRIFT':.20,'CONCEPT_DRIFT':.15,'SOURCE_DRIFT':.20,'PREDICTION_DRIFT':.15,'CALIBRATION_DRIFT':.05,'REGIME_DRIFT':.25,'ASSET_BEHAVIOR_DRIFT':.03}
        result={name:{'score':float(metrics[name]),'threshold':thresholds[name],'detected':float(metrics[name])>=thresholds[name],'action':DRIFT_ACTIONS[name]} for name in DRIFT_TYPES}
        result['FEATURE_DRIFT']['per_feature_scores']=feature_scores
        if persist:
            with connect(self.path,self.settings) as con:
                for name,item in result.items():
                    if item['detected']:con.execute("INSERT INTO drift_events(drift_type,scope,score,threshold,action,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",(name,json.dumps(scope or {'scope':'global'},sort_keys=True),item['score'],item['threshold'],item['action'],json.dumps({'baseline_samples':len(old_label),'recent_samples':len(new_label),'scope':scope or {},'per_feature_scores':feature_scores if name=='FEATURE_DRIFT' else {}}),time.time()))
        return {'detected':any(item['detected'] for item in result.values()),'types':result,'scope':scope or {}}

    def execute_actions(self,result:dict[str,Any])->list[dict[str,Any]]:
        """Create durable remediation actions.

        Immediate state mutations are marked COMPLETED. Operations that require
        training/recalibration/governance work are explicitly QUEUED and must be
        consumed by ResearchService; they are never mislabeled as executed merely
        because a metadata flag was written.
        """
        scope=result.get('scope') or {};created=[];now=time.time()
        with connect(self.path,self.settings) as con:
            for drift_type,item in result.get('types',{}).items():
                if not item.get('detected'):continue
                action=str(item['action']);affected='global';detail:dict[str,Any]={};status='COMPLETED';remediation=None;payload={}
                if drift_type=='SOURCE_DRIFT':
                    sources=list(scope.get('sources') or []);affected=','.join(sources) or 'source_profiles'
                    for source in sources:con.execute("UPDATE source_profiles SET reliability=MAX(.05,reliability*.8),updated_at=? WHERE source_id=?",(now,source))
                    detail={'sources_degraded':sources,'mutation':'source_profiles.reliability'}
                elif drift_type=='FEATURE_DRIFT':
                    names=sorted((item.get('per_feature_scores') or {}),key=(item.get('per_feature_scores') or {}).get,reverse=True)[:20]
                    affected=','.join(names) or 'feature_registry';status='QUEUED';remediation='RETRAIN_AFFECTED_MODELS';payload={'features':names,'scope':scope}
                    detail={'retraining_queued':True,'features':names}
                elif drift_type=='CALIBRATION_DRIFT':
                    affected='calibration';status='QUEUED';remediation='RECALIBRATE_MODELS';payload={'scope':scope}
                    detail={'recalibration_queued':True}
                elif drift_type in {'CONCEPT_DRIFT','PREDICTION_DRIFT'}:
                    affected='model_governance';status='QUEUED';remediation='FORCE_SHADOW_REVIEW';payload={'reason':drift_type,'scope':scope}
                    detail={'shadow_review_queued':True}
                elif drift_type=='REGIME_DRIFT':
                    affected='regime_engine';status='QUEUED';remediation='RETRAIN_REGIME_AND_RAISE_UNCERTAINTY';payload={'scope':scope,'uncertainty_multiplier':1.5}
                    con.execute("INSERT INTO metadata(key,value,updated_at) VALUES('regime_uncertainty_multiplier','1.5',?) ON CONFLICT(key) DO UPDATE SET value='1.5',updated_at=excluded.updated_at",(now,));detail={'uncertainty_multiplier':1.5,'regime_retraining_queued':True}
                elif drift_type=='ASSET_BEHAVIOR_DRIFT':
                    assets=list(scope.get('assets') or []);affected=','.join(assets) or 'asset_profiles';status='QUEUED';remediation='REBUILD_ASSET_PROFILES';payload={'assets':assets,'scope':scope}
                    for asset in assets:con.execute("DELETE FROM asset_profiles WHERE asset_id=?",(asset,))
                    detail={'profiles_invalidated':assets,'rebuild_queued':True}
                elif drift_type=='DATA_DRIFT':
                    affected='runtime_risk';status='QUEUED';remediation='CONSERVATIVE_MODE_AND_DATA_REVIEW';payload={'scope':scope,'uncertainty_multiplier':1.25}
                    con.execute("INSERT INTO metadata(key,value,updated_at) VALUES('drift_uncertainty_multiplier','1.25',?) ON CONFLICT(key) DO UPDATE SET value='1.25',updated_at=excluded.updated_at",(now,));detail={'uncertainty_multiplier':1.25,'conservative_mode_queued':True}
                else:
                    status='NOT_APPLICABLE';detail={'reason':'no_dispatch_rule'}
                drift_row=con.execute("SELECT id FROM drift_events WHERE drift_type=? ORDER BY id DESC LIMIT 1",(drift_type,)).fetchone()
                cur=con.execute("INSERT INTO drift_actions(drift_event_id,drift_type,requested_action,execution_status,execution_time,affected_component,result_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (drift_row['id'] if drift_row else None,drift_type,action,status,now if status=='COMPLETED' else None,affected,json.dumps(detail,sort_keys=True),now))
                action_id=int(cur.lastrowid)
                queue_id=None
                if remediation:
                    q=con.execute("INSERT INTO drift_remediation_queue(drift_action_id,drift_type,remediation_type,payload_json,state,attempts,created_at,updated_at) VALUES(?,?,?,?, 'PENDING',0,?,?)",
                        (action_id,drift_type,remediation,json.dumps(payload,sort_keys=True),now,now))
                    queue_id=int(q.lastrowid)
                created.append({'drift_action_id':action_id,'queue_id':queue_id,'drift_type':drift_type,'action':action,'execution_status':status,'affected_component':affected,'result':detail})
        return created
