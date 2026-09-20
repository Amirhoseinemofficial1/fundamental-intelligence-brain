from __future__ import annotations
import dataclasses,json,math,os,time
from typing import Any
from ..config import Settings
from ..storage.sqlite import connect
from .experts import SPECIALISTS,build_votes,decide
from .model_factory import ArtifactRegistry
from .decision_models import DecisionModelEngine
from .types import ExpertVote,SignalDecision

def _applicable(spec,event,asset)->bool:
    return str(event['domain']) in spec.domains and str(asset['asset_class']) in spec.asset_classes and (not spec.event_prefixes or any(str(event['event_type']).startswith(x) for x in spec.event_prefixes))

class RuntimeInferenceEngine:
    def __init__(self,settings:Settings):self.settings=settings;self.registry=ArtifactRegistry(settings);self.decisions=DecisionModelEngine(settings);self.path=self.registry.path
    def _drift_uncertainty_multiplier(self)->float:
        with connect(self.path,self.settings) as con:
            rows=con.execute("SELECT key,value FROM metadata WHERE key IN ('drift_uncertainty_multiplier','regime_uncertainty_multiplier')").fetchall()
        values=[1.0]
        for row in rows:
            try:values.append(max(1.0,float(row['value'])))
            except (TypeError,ValueError):continue
        return max(values)
    def _record(self,feature_id:str,expert:str,artifact_id:str,state:str,p:float,u:float)->None:
        with connect(self.path,self.settings) as con:con.execute("INSERT OR IGNORE INTO expert_outputs(feature_id,expert,artifact_id,model_state,probability,uncertainty,created_at) VALUES(?,?,?,?,?,?,?)",(feature_id,expert,artifact_id,state,p,u,time.time()))
    def _temporal_sequence(self,asset_id:str,horizon:int,feature:dict[str,Any],names:list[str],length:int)->list[list[float]]:
        with connect(self.path,self.settings) as con:rows=con.execute("SELECT values_json FROM feature_vectors WHERE asset_id=? AND horizon_seconds=? AND prediction_time<=? ORDER BY prediction_time DESC LIMIT ?",(asset_id,horizon,feature['prediction_time'],length)).fetchall()
        values=[json.loads(row['values_json']) for row in reversed(rows)] or [feature['values']]
        while len(values)<length:values.insert(0,values[0])
        return [[float(item.get(name,0.0)) for name in names] for item in values[-length:]]
    def infer(self,event:dict[str,Any],asset:dict[str,Any],horizon:int,feature:dict[str,Any],mode:str,outcomes:list[float],allow_bootstrap:bool=True,candidate_horizons:tuple[int,...]|None=None,expert_allowlist:tuple[str,...]|None=None)->tuple[list[ExpertVote],SignalDecision,dict[str,Any]]:
        votes=[];used=[]
        allowed=set(expert_allowlist or ())
        feature_version=feature.get('feature_version') if os.getenv('MODEL_REQUIRE_CURRENT_FEATURE_VERSION','0')=='1' else None
        for spec in SPECIALISTS:
            if allowed and spec.name not in allowed:continue
            if not _applicable(spec,event,asset):continue
            champion=self.registry.champion(spec.name,feature_version)
            if champion:
                p=champion.predict(feature['values']);metrics=champion.metadata.get('metrics') or {};u=min(.9,max(.05,float(metrics.get('brier',.25))+.35*(1-float(feature['quality']))))
                votes.append(ExpertVote(spec.name,str(asset['asset_id']),horizon,p,float(metrics.get('expected_magnitude',.002)),u,spec.base_weight,(f"artifact={champion.metadata['artifact_id']}",'runtime_artifact_inference')))
                used.append(champion.metadata['artifact_id']);self._record(feature['feature_id'],spec.name,champion.metadata['artifact_id'],'CHAMPION',p,u)
            for shadow in self.registry.shadows(spec.name,feature_version):
                p=shadow.predict(feature['values']);self._record(feature['feature_id'],spec.name,shadow.metadata['artifact_id'],'SHADOW',p,.5)
                with connect(self.path,self.settings) as con:con.execute('INSERT OR IGNORE INTO shadow_predictions(feature_id,artifact_id,probability,prediction_time) VALUES(?,?,?,?)',(feature['feature_id'],shadow.metadata['artifact_id'],p,feature['prediction_time']))
        source='ARTIFACT'
        with connect(self.path,self.settings) as con:temporal_rows=con.execute("SELECT artifact_id,expert,state FROM model_artifacts WHERE expert LIKE 'temporal_meta:%' AND state IN ('CHAMPION','SHADOW') AND (? IS NULL OR feature_version=?)",(feature_version,feature_version)).fetchall()
        for temporal_row in temporal_rows:
            # Event-routed execution deliberately avoids waking every temporal
            # challenger. The normal/refinement lane still sees the full set.
            if allowed:continue
            artifact=self.registry.load(str(temporal_row['artifact_id']));sequence=self._temporal_sequence(str(asset['asset_id']),horizon,feature,list(artifact.metadata['feature_names']),int(artifact.metadata['sequence_length']));p=artifact.predict({'__sequence__':sequence});u=min(.9,max(.05,float((artifact.metadata.get('metrics') or {}).get('brier',.25))));state=str(temporal_row['state']);self._record(feature['feature_id'],str(temporal_row['expert']),str(temporal_row['artifact_id']),state,p,u)
            if state=='SHADOW':
                with connect(self.path,self.settings) as con:con.execute('INSERT OR IGNORE INTO shadow_predictions(feature_id,artifact_id,probability,prediction_time) VALUES(?,?,?,?)',(feature['feature_id'],temporal_row['artifact_id'],p,feature['prediction_time']))
                continue
            votes.append(ExpertVote(str(temporal_row['expert']),str(asset['asset_id']),horizon,p,float((artifact.metadata.get('metrics') or {}).get('expected_magnitude',.002)),u,1.0,(f"artifact={temporal_row['artifact_id']}",'temporal_runtime_inference')));used.append(str(temporal_row['artifact_id']))
        if not votes and allow_bootstrap: votes=build_votes(event,asset,horizon,None,outcomes);source='BOOTSTRAP_FALLBACK'
        if not votes:return [],decide([],mode),{'source':'NO_MODEL','artifact_ids':[]}
        meta_values={f"expert_{v.expert}":v.direction_probability for v in votes}
        meta_values.update({f"expert_{v.expert}_uncertainty":v.uncertainty for v in votes})
        meta_values.update({'data_quality':feature['quality'],'event_importance':float(event.get('importance') or 0),'source_quality':float(event.get('source_credibility') or .5),'volatility':float(feature['values'].get('volatility',0)),'liquidity':float(feature['values'].get('dex_liquidity',feature['values'].get('depth',0))),'priced_in_score':float(event.get('priced_in_score') or .5),'historical_analogue_count':float(len(event.get('historical_analogues') or ()))})
        meta_values.update({k:float(v) for k,v in feature['values'].items() if k.startswith(('asset_profile_','regime_','cross_')) and isinstance(v,(int,float))})
        for v in votes:
            champion=self.registry.champion(v.expert,feature_version)
            if champion:meta_values[f"expert_{v.expert}_historic_brier"]=float((champion.metadata.get('metrics') or {}).get('brier',.25))
        meta=self.registry.champion('meta',feature_version)
        if meta:
            p=meta.predict(meta_values);total=sum(v.weight for v in votes);u=sum(v.uncertainty*v.weight for v in votes)/max(total,.01);agreement=abs(sum(1 if v.direction_probability>.55 else -1 if v.direction_probability<.45 else 0 for v in votes))/max(1,len(votes));samples=int(meta.metadata.get('metrics',{}).get('samples',0));confidence=max(.5,min(.99,.5+abs(p-.5)*(1-u)));risk=min(1,u*.6+(1-agreement)*.25+(1-feature['quality'])*.15);ev=abs(p-.5)*.01*(1-risk)
            qualified=samples>=30 and confidence>=.58 and risk<=.45 and feature['quality']>=.45;action='NO_TRADE' if not qualified else 'BUY' if p>=.62 else 'SELL_SHORT' if p<=.38 else 'WAIT'
            d=SignalDecision(action,str(asset['asset_id']),horizon,p,confidence,.005,u,ev,risk,'IMMEDIATE if event direction is confirmed; otherwise CONFIRMATION.','Invalidate on corrected event, adverse regime or learned risk boundary.','Use artifact-estimated response distribution and MFE.','Exit on probability/event decay, invalidation, target, or learned exit policy.',agreement,samples,mode,(f"meta_artifact={meta.metadata['artifact_id']}",f"feature_id={feature['feature_id']}"))
            source='ARTIFACT_META';used.append(meta.metadata['artifact_id']);self._record(feature['feature_id'],'meta',meta.metadata['artifact_id'],'CHAMPION',p,u)
        else:d=decide(votes,mode)
        for shadow in self.registry.shadows('meta',feature_version):
            shadow_probability=shadow.predict(meta_values);self._record(feature['feature_id'],'meta',shadow.metadata['artifact_id'],'SHADOW',shadow_probability,.5)
            with connect(self.path,self.settings) as con:con.execute('INSERT OR IGNORE INTO shadow_predictions(feature_id,artifact_id,probability,prediction_time) VALUES(?,?,?,?)',(feature['feature_id'],shadow.metadata['artifact_id'],shadow_probability,feature['prediction_time']))
        forecast=self.decisions.forecast(feature,candidate_horizons or (horizon,),outcomes)
        edge=abs(float(d.probability)-.5)*2.0
        expected_value=forecast.expected_return*edge*(1.0-forecast.risk_score)
        reasons=tuple(d.reasons)+(f"magnitude_source={forecast.sources.get('magnitude_return')}",f"risk_source={forecast.sources.get('risk')}",f"preferred_horizon={forecast.preferred_horizon}")
        drift_multiplier=self._drift_uncertainty_multiplier()
        base_uncertainty=max(float(d.uncertainty),forecast.magnitude_uncertainty)
        adjusted_uncertainty=min(1.0,base_uncertainty*drift_multiplier)
        adjusted_risk=min(1.0,max(float(d.risk_score),forecast.risk_score)+(adjusted_uncertainty-base_uncertainty)*.5)
        adjusted_ev=expected_value/max(1.0,drift_multiplier)
        reasons=tuple(reasons)+(f"drift_uncertainty_multiplier={drift_multiplier:.3f}",)
        d=dataclasses.replace(d,horizon_seconds=forecast.preferred_horizon,expected_magnitude=forecast.expected_return,uncertainty=adjusted_uncertainty,expected_value=adjusted_ev,risk_score=adjusted_risk,target_logic=f"Expected return {forecast.expected_return:.4%}; MFE {forecast.expected_mfe:.4%}; MAE {forecast.expected_mae:.4%}; time-to-peak {forecast.time_to_peak:.0f}s.",exit_logic=f"{d.exit_logic} Modelled MAE and time-to-peak boundary={forecast.time_to_peak:.0f}s.",reasons=reasons)
        used.extend(forecast.artifact_ids)
        return votes,d,{'source':source,'artifact_ids':list(dict.fromkeys(used)),'controlling_artifact_id':meta.metadata['artifact_id'] if meta else None,'feature_id':feature['feature_id'],'decision_models':dataclasses.asdict(forecast),'routed_experts':sorted(allowed)}
