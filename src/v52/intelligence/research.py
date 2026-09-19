from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import sqlite3
import statistics
import time
import uuid
from collections import defaultdict
from typing import Any

from ..config import Settings
from ..metrics import Metrics
from ..storage.control import ControlStore
from ..storage.sqlite import connect, transaction
from .experts import brier_score, calibration_error, purged_walk_forward
from .store import BrainStore
from .learning import TrainingCoordinator, EXPERT_FEATURES
from .warmup import RuntimeWarmup
from .signal_state import SignalStateMachine
from .hypothesis_engine import FeatureInteractionHypothesisEngine
from .drift import DriftEngine, DriftDataCollector
from .attribution import FailureAttributionEngine
from .telegram_producers import TelegramIntelligenceProducers
from .active_signal_monitor import ActiveSignalMonitorService
from .regime_governance import RegimeGovernance


logger = logging.getLogger(__name__)


def _calibrator(rows: list[dict[str, Any]], bins: int = 10) -> dict[str, Any]:
    grouped: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        index = min(bins - 1, max(0, int(float(row["probability"]) * bins)))
        grouped[index].append(int(row["label"]))
    mapping = {}
    for index in range(bins):
        labels = grouped.get(index, [])
        mapping[str(index)] = (sum(labels) + 2.0) / (len(labels) + 4.0)
    return {"type": "beta_binned_calibrator", "bins": bins, "mapping": mapping, "samples": len(rows)}


def _apply_calibrator(probability: float, artifact: dict[str, Any]) -> float:
    bins = int(artifact.get("bins") or 10)
    index = min(bins - 1, max(0, int(probability * bins)))
    return float((artifact.get("mapping") or {}).get(str(index), probability))


def _benjamini_hochberg(p_values:list[float])->list[float]:
    """Return monotone Benjamini-Hochberg q-values in original order."""
    n=len(p_values)
    if not n:return []
    ordered=sorted(enumerate(max(0.0,min(1.0,float(p))) for p in p_values),key=lambda item:item[1])
    q=[1.0]*n;running=1.0
    for reverse_index in range(n-1,-1,-1):
        original,p=ordered[reverse_index];rank=reverse_index+1
        running=min(running,p*n/rank);q[original]=max(0.0,min(1.0,running))
    return q


class ResearchService:
    """Controlled continual learning, evaluation, promotion and review plane."""

    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.brain = BrainStore(settings)
        self.control = ControlStore(settings)
        self.training = TrainingCoordinator(settings)
        self.warmup = RuntimeWarmup(settings)
        self.path = self.brain.path
        self.interactions = FeatureInteractionHypothesisEngine(settings)
        self.drift_engine = DriftEngine(settings)
        self.drift_collector = DriftDataCollector(settings)
        self.attribution = FailureAttributionEngine(settings)
        self.regime_governance = RegimeGovernance(settings)
        self.telegram = TelegramIntelligenceProducers(self.control)
        self.minimum_training_rows = max(40, int(os.getenv("RESEARCH_MIN_TRAINING_ROWS", "100")))
        self.qualification_min_events = max(1, int(os.getenv("QUALIFICATION_MIN_EVENTS", "200")))
        self.qualification_min_responses = max(1, int(os.getenv("QUALIFICATION_MIN_RESPONSES", "100")))
        self.max_brier = float(os.getenv("QUALIFICATION_MAX_BRIER", ".24"))
        self.max_ece = float(os.getenv("QUALIFICATION_MAX_CALIBRATION_ERROR", ".15"))
        self.qualification_min_specialists = max(1, int(os.getenv("QUALIFICATION_MIN_SPECIALIST_CHAMPIONS", "4")))

    def _learning_rows_sync(self, limit: int = 10000) -> list[dict[str, Any]]:
        if os.getenv('MODEL_SIGNAL_LABELS_ONLY','0')=='1':
            return [{**r,'created_at':r['prediction_time'],'probability':r['direction_probability']} for r in self.training.signal_learning_rows()]
        with connect(self.path, self.settings) as con:
            rows = con.execute(
                """SELECT p.id,p.created_at,p.direction_probability probability,
                          CASE WHEN mr.return_value>0 THEN 1 ELSE 0 END label,
                          mr.return_value,e.event_type,p.asset_id,p.horizon_seconds
                   FROM predictions p JOIN market_responses mr ON mr.event_id=p.event_id
                    AND mr.asset_id=p.asset_id AND mr.horizon_seconds=p.horizon_seconds
                   JOIN events e ON e.id=p.event_id
                   ORDER BY p.created_at,p.id LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _evaluate_walk_forward(rows: list[dict[str, Any]]) -> dict[str, Any]:
        tuples = [(float(r["created_at"]), [float(r["probability"])], int(r["label"])) for r in rows]
        splits = purged_walk_forward(tuples, folds=4, embargo=1)
        if not splits:
            return {"valid": False, "samples": len(rows), "reason": "insufficient_walk_forward_rows"}
        raw_probabilities: list[float] = []
        calibrated_probabilities: list[float] = []
        labels: list[int] = []
        fold_metrics: list[dict[str, Any]] = []
        for train_idx, validation_idx in splits:
            cutoff=min(float(rows[i]["created_at"]) for i in validation_idx)
            train = [rows[i] for i in train_idx if float(rows[i].get("label_available_at",rows[i]["created_at"]))<cutoff]
            if len(train)<10:continue
            validation = [rows[i] for i in validation_idx]
            artifact = _calibrator(train)
            raw = [float(row["probability"]) for row in validation]
            calibrated = [_apply_calibrator(value, artifact) for value in raw]
            truth = [int(row["label"]) for row in validation]
            raw_probabilities.extend(raw)
            calibrated_probabilities.extend(calibrated)
            labels.extend(truth)
            fold_metrics.append({"train": len(train), "validation": len(validation), "brier": brier_score(calibrated, truth), "ece": calibration_error(calibrated, truth)})
        if not labels:return {"valid":False,"samples":len(rows),"reason":"no_mature_purged_folds"}
        return {
            "valid": True,
            "samples": len(rows),
            "validation_samples": len(labels),
            "raw_brier": brier_score(raw_probabilities, labels),
            "calibrated_brier": brier_score(calibrated_probabilities, labels),
            "calibration_error": calibration_error(calibrated_probabilities, labels),
            "folds": fold_metrics,
        }

    async def train_challenger(self) -> dict[str, Any]:
        rows = await asyncio.to_thread(self._learning_rows_sync)
        if len(rows) < self.minimum_training_rows:
            return {"created": False, "reason": "minimum_training_rows", "rows": len(rows)}
        metrics = self._evaluate_walk_forward(rows)
        if not metrics.get("valid"):
            return {"created": False, **metrics}
        artifact = _calibrator(rows)
        version_id = f"meta-ensemble-56-{uuid.uuid4().hex[:12]}"
        now = time.time()
        with connect(self.path, self.settings) as con:
            con.execute(
                """INSERT INTO model_versions(model_version_id,model_name,role,status,parent_version_id,code_version,data_version,
                   feature_version,train_start,train_end,validation_start,validation_end,hyperparameters_json,metrics_json,artifact_json,created_at)
                   SELECT ?, 'meta_ensemble','META','CHALLENGER',model_version_id,'57.3.0',?,'features-57.3.0',?,?,?,?,?,?,?,?
                   FROM model_versions WHERE model_name='meta_ensemble' AND status='CHAMPION'""",
                (version_id, f"responses:{len(rows)}", rows[0]["created_at"], rows[-1]["created_at"],
                 rows[0]["created_at"], rows[-1]["created_at"], json.dumps({"method": "beta_binned", "purged_walk_forward": True, "embargo": 1}),
                 json.dumps(metrics), json.dumps(artifact), now),
            )
            con.execute(
                "INSERT INTO audit_log(actor,action,object_type,object_id,detail_json,created_at) VALUES('research','CHALLENGER_CREATED','model',?,?,?)",
                (version_id, json.dumps(metrics), now),
            )
            con.execute(
                """INSERT INTO experiments(experiment_id,hypothesis,status,method,specification_json,result_json,created_at)
                   VALUES(?,?,'EVALUATED','PURGED_WALK_FORWARD',?,?,?)""",
                (f"experiment:{version_id}", "Calibrated challenger improves out-of-sample Brier score without worse calibration",
                 json.dumps({"model_version_id": version_id, "embargo": 1, "folds": 4}), json.dumps(metrics), now),
            )
        return {"created": True, "model_version_id": version_id, "metrics": metrics}

    async def evaluate_promotion(self, version_id: str) -> dict[str, Any]:
        with connect(self.path, self.settings) as con:
            challenger = con.execute("SELECT * FROM model_versions WHERE model_version_id=? AND status='CHALLENGER'", (version_id,)).fetchone()
            champion = con.execute("SELECT * FROM model_versions WHERE model_name='meta_ensemble' AND status='CHAMPION'").fetchone()
            if not challenger or not champion:
                return {"promoted": False, "reason": "model_state_missing"}
            challenger_metrics = json.loads(challenger["metrics_json"])
            champion_metrics = json.loads(champion["metrics_json"])
            challenger_brier = float(challenger_metrics.get("calibrated_brier", math.inf))
            champion_brier = float(champion_metrics.get("calibrated_brier", math.inf))
            ece = float(challenger_metrics.get("calibration_error", math.inf))
            stable_folds = all(float(x.get("brier", math.inf)) <= .30 for x in challenger_metrics.get("folds") or [])
            qualifies = challenger_brier < champion_brier - .002 and challenger_brier <= self.max_brier and ece <= self.max_ece and stable_folds
            transaction(con)
            try:
                if qualifies:
                    now = time.time()
                    con.execute("UPDATE model_versions SET status='RETIRED',retired_at=? WHERE model_version_id=?", (now, champion["model_version_id"]))
                    con.execute("UPDATE model_versions SET status='CHAMPION',promoted_at=? WHERE model_version_id=?", (now, version_id))
                    action = "MODEL_PROMOTED"
                else:
                    con.execute("UPDATE model_versions SET status='REJECTED' WHERE model_version_id=?", (version_id,))
                    action = "MODEL_REJECTED"
                con.execute(
                    "INSERT INTO audit_log(actor,action,object_type,object_id,detail_json,created_at) VALUES('research',?,'model',?,?,?)",
                    (action, version_id, json.dumps({"challenger_brier": challenger_brier, "champion_brier": champion_brier, "ece": ece, "stable_folds": stable_folds}), time.time()),
                )
                con.execute(
                    "UPDATE experiments SET status=?,result_json=?,completed_at=? WHERE experiment_id=?",
                    ("PROMOTED" if qualifies else "REJECTED", json.dumps({"promoted": qualifies, "challenger_brier": challenger_brier,
                     "champion_brier": champion_brier, "ece": ece, "stable_folds": stable_folds}), time.time(), f"experiment:{version_id}"),
                )
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        return {"promoted": qualifies, "model_version_id": version_id, "challenger_brier": challenger_brier, "champion_brier": champion_brier, "ece": ece}

    async def update_knowledge(self) -> int:
        return await asyncio.to_thread(self._update_knowledge_sync)

    def _update_knowledge_sync(self) -> int:
        with connect(self.path, self.settings) as con:
            rows = con.execute(
                """SELECT e.event_type,mr.asset_id,mr.horizon_seconds,COUNT(*) samples,AVG(mr.return_value) effect,
                          AVG(mr.return_value*mr.return_value)-AVG(mr.return_value)*AVG(mr.return_value) variance
                   FROM market_responses mr JOIN events e ON e.id=mr.event_id
                   GROUP BY e.event_type,mr.asset_id,mr.horizon_seconds"""
            ).fetchall()
            now = time.time()
            alpha=max(1e-6,min(.25,float(os.getenv('KNOWLEDGE_FDR_ALPHA','.05'))))
            evaluated=[]
            for row in rows:
                samples=int(row['samples']);effect=float(row['effect'] or 0.0);variance=max(0.0,float(row['variance'] or 0.0))
                uncertainty=math.sqrt(variance/max(1,samples))
                if samples<2:p_value=1.0
                elif uncertainty<=1e-12:p_value=0.0 if abs(effect)>1e-12 else 1.0
                else:p_value=math.erfc(abs(effect/uncertainty)/math.sqrt(2.0))
                evaluated.append({'row':row,'samples':samples,'effect':effect,'uncertainty':uncertainty,'p_value':p_value})
            q_values=_benjamini_hochberg([item['p_value'] for item in evaluated])
            for item,q_value in zip(evaluated,q_values):
                row=item['row'];samples=item['samples'];effect=item['effect'];uncertainty=item['uncertainty'];p_value=item['p_value']
                key=f"{row['event_type']}:{row['asset_id']}:{row['horizon_seconds']}"
                effects=[float(x['return_value']) for x in con.execute("SELECT mr.return_value FROM market_responses mr JOIN events e ON e.id=mr.event_id WHERE e.event_type=? AND mr.asset_id=? AND mr.horizon_seconds=? ORDER BY mr.id",(row['event_type'],row['asset_id'],row['horizon_seconds'])).fetchall()]
                half=max(1,len(effects)//2);older=statistics.mean(effects[:half]);newer=statistics.mean(effects[half:]) if effects[half:] else older
                stable=(older==0 and newer==0) or older*newer>0
                ci_low=effect-1.96*uncertainty;ci_high=effect+1.96*uncertainty
                ci_significant=ci_low>0 or ci_high<0
                fdr_qualified=q_value<=alpha
                significant=ci_significant and fdr_qualified
                level='HYPOTHESIS'
                if samples>=20 and stable:level='WEAK_EVIDENCE'
                if samples>=75 and stable and significant:level='MODERATE_EVIDENCE'
                if samples>=200 and stable and significant and abs(newer-older)<=max(abs(effect),1e-6):level='STRONG_EVIDENCE'
                if samples>=500 and level=='STRONG_EVIDENCE':level='PRODUCTION_VALIDATED'
                evidence={'horizon_seconds':int(row['horizon_seconds']),'estimator':'sample_mean','ci95':[ci_low,ci_high],'temporal_stability':stable,'older_effect':older,'newer_effect':newer,'p_value':p_value,'q_value':q_value,'fdr_alpha':alpha,'fdr_family_size':len(evaluated),'multiple_testing_method':'BENJAMINI_HOCHBERG_FDR','fdr_qualified':fdr_qualified}
                con.execute(
                    """INSERT INTO knowledge(knowledge_key,subject,predicate,object,level,sample_count,effect_mean,uncertainty,evidence_json,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(knowledge_key) DO UPDATE SET level=excluded.level,sample_count=excluded.sample_count,
                       effect_mean=excluded.effect_mean,uncertainty=excluded.uncertainty,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                    (key,str(row['event_type']),'IMPACTS',str(row['asset_id']),level,samples,effect,uncertainty,json.dumps(evidence),now),
                )
                hypothesis_id='hypothesis:'+hashlib.sha256(key.encode()).hexdigest()[:20]
                status='VALIDATED' if level in {'STRONG_EVIDENCE','PRODUCTION_VALIDATED'} else 'EVIDENCE_ACCUMULATING' if level!='HYPOTHESIS' else 'HYPOTHESIS'
                con.execute("""INSERT INTO hypotheses(hypothesis_id,statement,status,scope_json,evidence_json,effect_size,confidence_low,confidence_high,sample_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(hypothesis_id) DO UPDATE SET status=excluded.status,evidence_json=excluded.evidence_json,effect_size=excluded.effect_size,confidence_low=excluded.confidence_low,confidence_high=excluded.confidence_high,sample_count=excluded.sample_count,updated_at=excluded.updated_at""",
                  (hypothesis_id,f"{row['event_type']} affects {row['asset_id']} over {row['horizon_seconds']} seconds",status,json.dumps({'event_type':row['event_type'],'asset_id':row['asset_id'],'horizon':row['horizon_seconds']}),json.dumps({'level':level,'stable':stable,'ci_significant':ci_significant,'fdr_qualified':fdr_qualified,'p_value':p_value,'q_value':q_value,'multiple_testing_method':'BENJAMINI_HOCHBERG_FDR'}),effect,ci_low,ci_high,samples,now,now))
            assets = con.execute("""SELECT mr.asset_id,a.asset_class,a.metadata_json,COUNT(*) samples,AVG(ABS(mr.return_value)) avg_abs,
                AVG(CASE WHEN mr.reversal=1 THEN 1.0 ELSE 0.0 END) reversal FROM market_responses mr JOIN assets a ON a.asset_id=mr.asset_id GROUP BY mr.asset_id""").fetchall()
            for row in assets:
                responses=[dict(x) for x in con.execute("""SELECT mr.*,e.domain,e.event_type,e.source_credibility,
                    (SELECT r.regime FROM regimes r WHERE r.asset_id=mr.asset_id AND r.observed_at<=mr.measured_at ORDER BY r.observed_at DESC LIMIT 1) outcome_regime
                    FROM market_responses mr JOIN events e ON e.id=mr.event_id WHERE mr.asset_id=? ORDER BY mr.measured_at""",(row['asset_id'],)).fetchall()]
                returns=[float(x['return_value']) for x in responses];mfes=sorted(abs(float(x['mfe'])) for x in responses);maes=sorted(abs(float(x['mae'])) for x in responses)
                def sensitivity(predicate):
                    chosen=[abs(float(x['return_value'])) for x in responses if predicate(x)];return statistics.mean(chosen) if chosen else 0.0
                horizons={int(x['horizon_seconds']):[] for x in responses}
                for item in responses:horizons[int(item['horizon_seconds'])].append(float(item['return_value']))
                horizon_scores={key:statistics.mean(value) for key,value in horizons.items() if value};best_horizon=max(horizon_scores,key=horizon_scores.get) if horizon_scores else 0;worst_horizon=min(horizon_scores,key=horizon_scores.get) if horizon_scores else 0
                metadata=json.loads(row['metadata_json'] or '{}');klass=str(row['asset_class']);exchange=str(metadata.get('exchange') or '')
                if klass in {'CRYPTO','TOKEN'}:benchmark='BTC' if row['asset_id']!='BTC' else 'ETH'
                elif klass=='EQUITY':benchmark={'XNAS':'NDX','XNYS':'SPX','XLON':'FTSE','XETR':'DAX','XASX':'ASX200','XTKS':'NIKKEI'}.get(exchange,'SPX')
                elif klass=='FOREX':benchmark='DXY'
                elif str(row['asset_id']) in {'XAUUSD','XAGUSD'}:benchmark='DXY'
                elif str(row['asset_id']) in {'WTI','BRENT'}:benchmark='WTI' if row['asset_id']=='BRENT' else 'BRENT'
                else:benchmark='SPX'
                benchmark_rows=[dict(x) for x in con.execute("SELECT event_id,horizon_seconds,return_value FROM market_responses WHERE asset_id=?",(benchmark,)).fetchall()];benchmark_map={(x['event_id'],x['horizon_seconds']):float(x['return_value']) for x in benchmark_rows};paired=[(float(x['return_value']),benchmark_map[(x['event_id'],x['horizon_seconds'])]) for x in responses if (x['event_id'],x['horizon_seconds']) in benchmark_map]
                market_beta=sum(a*b for a,b in paired)/max(sum(b*b for _,b in paired),1e-12) if paired else 0.0
                expert_rows=[dict(x) for x in con.execute("""SELECT eo.expert,AVG((eo.probability-(CASE WHEN mr.return_value>0 THEN 1 ELSE 0 END))*(eo.probability-(CASE WHEN mr.return_value>0 THEN 1 ELSE 0 END))) score FROM expert_outputs eo JOIN feature_vectors fv ON fv.feature_id=eo.feature_id JOIN market_responses mr ON mr.event_id=fv.event_id AND mr.asset_id=fv.asset_id AND mr.horizon_seconds=fv.horizon_seconds WHERE fv.asset_id=? GROUP BY eo.expert""",(row['asset_id'],)).fetchall()]
                expert_scores={str(x['expert']):float(x['score']) for x in expert_rows};best_expert=min(expert_scores,key=expert_scores.get) if expert_scores else 'UNKNOWN';worst_expert=max(expert_scores,key=expert_scores.get) if expert_scores else 'UNKNOWN'
                exit_rows=[dict(x) for x in con.execute("SELECT pe.* FROM post_exit pe JOIN signals s ON s.id=pe.signal_id JOIN predictions p ON p.id=s.prediction_id WHERE p.asset_id=?",(row['asset_id'],)).fetchall()]
                regime_groups:dict[str,list[float]]=defaultdict(list)
                for item in responses:
                    if item.get('outcome_regime'):regime_groups[str(item['outcome_regime'])].append(float(item['return_value']))
                regime_scores={name:statistics.mean(values) for name,values in regime_groups.items() if values}
                entry_rows=[dict(x) for x in con.execute("""SELECT CASE
                    WHEN UPPER(s.entry_logic) LIKE 'IMMEDIATE:%' THEN 'IMMEDIATE' WHEN UPPER(s.entry_logic) LIKE 'PULLBACK:%' THEN 'PULLBACK'
                    WHEN UPPER(s.entry_logic) LIKE 'CONFIRMATION:%' THEN 'CONFIRMATION' ELSE 'WAIT' END style,
                    p.expected_value,mr.return_value,mr.mfe,mr.mae FROM signals s JOIN predictions p ON p.id=s.prediction_id
                    JOIN market_responses mr ON mr.event_id=p.event_id AND mr.asset_id=p.asset_id AND mr.horizon_seconds=p.horizon_seconds WHERE p.asset_id=?""",(row['asset_id'],)).fetchall()]
                entry_by_style={}
                for style in ('IMMEDIATE','PULLBACK','CONFIRMATION','WAIT'):
                    chosen=[x for x in entry_rows if x['style']==style]
                    entry_by_style[style]={'count':len(chosen),'hit_rate':sum(float(x['return_value'])>0 for x in chosen)/max(1,len(chosen)),
                        'average_ev':statistics.mean(float(x['expected_value']) for x in chosen) if chosen else 0.0,
                        'mfe':statistics.mean(float(x['mfe']) for x in chosen) if chosen else 0.0,'mae':statistics.mean(float(x['mae']) for x in chosen) if chosen else 0.0,
                        'drawdown':min((float(x['mae']) for x in chosen),default=0.0)}
                entry_score=max((item['hit_rate']*max(0.0,item['average_ev']) for item in entry_by_style.values()),default=0.0)
                profile = {
                    "average_absolute_response":float(row['avg_abs'] or 0.0),"market_beta":market_beta,"btc_beta":market_beta if benchmark in {'BTC','ETH'} else 0.0,
                    "benchmark_asset":benchmark,"benchmark_pair_count":len(paired),"benchmark_provenance":f"market_responses:{benchmark}",
                    "macro_sensitivity":sensitivity(lambda x:str(x['domain']) in {'MACRO','CENTRAL_BANK'}),"listing_sensitivity":sensitivity(lambda x:'LIST' in str(x['event_type'])),"unlock_sensitivity":sensitivity(lambda x:'UNLOCK' in str(x['event_type'])),"whale_sensitivity":sensitivity(lambda x:'WHALE' in str(x['event_type'])),"news_sensitivity":statistics.mean([abs(x) for x in returns]) if returns else 0.0,"liquidity_sensitivity":sensitivity(lambda x:'LIQUID' in str(x['event_type'])),
                    "best_horizon":best_horizon,"worst_horizon":worst_horizon,"best_regime":max(regime_scores,key=regime_scores.get) if regime_scores else 'INSUFFICIENT_DATA',"worst_regime":min(regime_scores,key=regime_scores.get) if regime_scores else 'INSUFFICIENT_DATA',"best_expert":best_expert,"worst_expert":worst_expert,
                    "fakeout_rate":float(row['reversal'] or 0.0),"reversal_rate":float(row['reversal'] or 0.0),"reversal_probability":float(row['reversal'] or 0.0),
                    "mfe_mean":statistics.mean(mfes) if mfes else 0.0,"mfe_p90":mfes[min(len(mfes)-1,int(len(mfes)*.9))] if mfes else 0.0,"mae_mean":statistics.mean(maes) if maes else 0.0,"mae_p90":maes[min(len(maes)-1,int(len(maes)*.9))] if maes else 0.0,
                    "entry_style_performance":entry_score,"entry_style_metrics":entry_by_style,"exit_style_performance":statistics.mean(float(x['return_after_exit']) for x in exit_rows) if exit_rows else 0.0,"source_sensitivity":sensitivity(lambda x:float(x['source_credibility'])>=.8),
                }
                con.execute(
                    """INSERT INTO asset_profiles(asset_id,profile_json,sample_count,updated_at) VALUES(?,?,?,?)
                       ON CONFLICT(asset_id) DO UPDATE SET profile_json=excluded.profile_json,sample_count=excluded.sample_count,updated_at=excluded.updated_at""",
                    (row["asset_id"], json.dumps(profile), int(row["samples"]), now),
                )
            return len(rows)

    async def review_signal_lifecycle(self) -> dict[str, int]:
        result = await asyncio.to_thread(self._review_signal_lifecycle_sync)
        closed_signals = result.pop("closed_signals", [])
        if self.settings.telegram_enabled:
            for item in closed_signals:
                await self.control.enqueue_alert(
                    f"signal-exit:{item['signal_id']}", "signal_exit", 9,
                    {"kind": "signal_exit", **item}, time.time() + 86400,
                )
        return result

    def _review_signal_lifecycle_sync(self) -> dict[str, Any]:
        counts: dict[str, Any] = {"closed": 0, "false_alert": 0, "missed": 0, "closed_signals": []}
        machine=SignalStateMachine(self.settings)
        with connect(self.path, self.settings) as con:
            rows = con.execute(
                """SELECT s.id signal_id,s.action,s.state,s.telegram_state,p.id prediction_id,p.event_id,p.asset_id,p.horizon_seconds,
                          p.calibrated_confidence,e.title event_title,mr.return_value,mr.mfe,mr.mae
                   FROM signals s JOIN predictions p ON p.id=s.prediction_id JOIN market_responses mr ON mr.event_id=p.event_id
                    AND mr.asset_id=p.asset_id AND mr.horizon_seconds=p.horizon_seconds JOIN events e ON e.id=p.event_id
                   WHERE s.state='ACTIVE'"""
            ).fetchall()
            now = time.time()
            for row in rows:
                correct = (row["action"] in {"BUY", "ENTER_ON_CONFIRMATION"} and row["return_value"] > 0) or (row["action"] in {"SELL_SHORT", "ENTER_ON_PULLBACK"} and row["return_value"] < 0)
                reason = "HORIZON_OUTCOME_CORRECT" if correct else "HORIZON_OUTCOME_ADVERSE"
                context={"return":row['return_value'],"mfe":row['mfe'],"mae":row['mae']}
                clock=con.execute("SELECT MIN(observed_at) first_at,MAX(available_at) last_at FROM reference_prices WHERE asset_id=?",(row['asset_id'],)).fetchone()
                if clock and clock['last_at'] is not None and float(clock['last_at'])>now+1:context['effective_at']=float(clock['first_at'])
                machine.transition_sync(int(row['signal_id']),'UPDATED',reason,context);machine.transition_sync(int(row['signal_id']),'EXIT_PENDING','HORIZON_EXIT_REQUESTED',context)
                if not correct:
                    attributed=self.attribution.attribute({**self.attribution.prediction_evidence(int(row['prediction_id'])),**context,'confidence':row['calibrated_confidence'],'correct':correct,'peak_after_horizon':abs(float(row['mfe']))>abs(float(row['return_value']))*2})
                    con.execute("INSERT OR IGNORE INTO failure_attribution(prediction_id,category,evidence_json,created_at) VALUES(?,?,?,?)",(row['prediction_id'],attributed['category'],json.dumps(attributed['evidence']),now))
                counts["closed"] += 1
                if row["telegram_state"] == "ELIGIBLE":
                    counts["closed_signals"].append({
                        "signal_id": int(row["signal_id"]), "action": "EXIT", "previous_action": row["action"],
                        "asset_id": row["asset_id"], "event_title": row["event_title"], "horizon_seconds": int(row["horizon_seconds"]),
                        "close_reason": reason, "return_value": float(row["return_value"]), "mfe": float(row["mfe"]),
                        "mae": float(row["mae"]), "calibrated_confidence": float(row["calibrated_confidence"]),
                    })
                if abs(float(row["return_value"])) < .001:
                    counts["false_alert"] += 1
            missed = con.execute(
                """SELECT mr.id,mr.event_id,mr.asset_id,mr.return_value FROM market_responses mr
                   WHERE ABS(mr.return_value)>=.01 AND NOT EXISTS(
                    SELECT 1 FROM predictions p JOIN signals s ON s.prediction_id=p.id
                    WHERE p.event_id=mr.event_id AND p.asset_id=mr.asset_id AND s.state IN ('ACTIVE','CLOSED'))
                   LIMIT 500"""
            ).fetchall()
            for row in missed:
                object_id = f"{row['event_id']}:{row['asset_id']}:{row['id']}"
                exists = con.execute("SELECT 1 FROM audit_log WHERE action='MISSED_OPPORTUNITY' AND object_id=?", (object_id,)).fetchone()
                if not exists:
                    has_prediction=bool(con.execute("SELECT 1 FROM predictions WHERE event_id=? AND asset_id=?",(row['event_id'],row['asset_id'])).fetchone());asset_resolved=bool(con.execute("SELECT 1 FROM assets WHERE asset_id=?",(row['asset_id'],)).fetchone())
                    missed=self.attribution.missed(self.attribution.missed_evidence(int(row['id'])))
                    con.execute("INSERT OR IGNORE INTO missed_opportunities(response_id,event_id,asset_id,return_value,root_cause,evidence_json,created_at) VALUES(?,?,?,?,?,?,?)",(row['id'],row['event_id'],row['asset_id'],row['return_value'],missed['cause'],json.dumps(missed['evidence']),now))
                    con.execute(
                        "INSERT INTO audit_log(actor,action,object_type,object_id,detail_json,created_at) VALUES('research','MISSED_OPPORTUNITY','market_response',?,?,?)",
                        (object_id, json.dumps({"return": row["return_value"], "review": "WHY_DID_WE_MISS_THIS"}), now),
                    )
                    counts["missed"] += 1
            simulated_now=float(con.execute("SELECT COALESCE(MAX(available_at),?) value FROM reference_prices",(now,)).fetchone()['value'])
        # The lifecycle consumer remains the runtime monitor. In replay/tests its
        # clock advances to the latest actually persisted quote, never to a label.
        ActiveSignalMonitorService(self.settings).cycle(max(now,simulated_now))
        return counts

    def _update_source_reliability_sync(self)->dict[str,Any]:
        """Learn a conservative source score from directionally attributable outcomes.

        The join is deliberately performed in Python because asset membership is
        stored as canonical JSON and SQLite JSON1 is not assumed.  Only observations
        available before an event and within six hours of it are attributable.
        """
        with connect(self.path,self.settings) as con:
            observations=[dict(r) for r in con.execute("SELECT source_id,event_time,available_time,ingestion_time,direction,asset_ids_json FROM canonical_observations WHERE direction IN ('UP','DOWN') ORDER BY available_time").fetchall()]
            outcomes=[dict(r) for r in con.execute("SELECT rt.event_epoch,mr.asset_id,mr.return_value FROM market_responses mr JOIN response_targets rt ON rt.id=mr.target_id ORDER BY rt.event_epoch").fetchall()]
            stats:dict[str,dict[str,float]]={}
            for observation in observations:
                assets=set(json.loads(observation['asset_ids_json']));direction=str(observation['direction']);available=float(observation['available_time'])
                eligible=[row for row in outcomes if row['asset_id'] in assets and available<=float(row['event_epoch'])<=available+21600]
                if not eligible:continue
                nearest=min(eligible,key=lambda row:float(row['event_epoch'])-available);actual='UP' if float(nearest['return_value'])>0 else 'DOWN' if float(nearest['return_value'])<0 else 'FLAT'
                item=stats.setdefault(str(observation['source_id']),{'samples':0.0,'correct':0.0,'latency':0.0});item['samples']+=1;item['correct']+=int(actual==direction);item['latency']+=max(0.0,float(observation['ingestion_time'])-float(observation['event_time']))
            now=time.time()
            for source,item in stats.items():
                samples=int(item['samples']);reliability=(item['correct']+2.0)/(samples+4.0)
                con.execute("""INSERT INTO source_profiles(source_id,observations,latency_sum,latency_samples,reliability,updated_at) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(source_id) DO UPDATE SET reliability=excluded.reliability,latency_sum=excluded.latency_sum,latency_samples=excluded.latency_samples,updated_at=excluded.updated_at""",(source,samples,item['latency'],samples,reliability,now))
            return {'sources':len(stats),'attributed_observations':sum(int(x['samples']) for x in stats.values())}

    async def _recover_drift_remediations(self)->list[dict[str,Any]]:
        """Resume PENDING/RUNNING remediation work after process restart."""
        with connect(self.path,self.settings) as con:
            rows=con.execute("""SELECT q.id queue_id,q.drift_type,q.remediation_type,q.payload_json,a.id drift_action_id,a.requested_action,a.affected_component,a.result_json
                FROM drift_remediation_queue q JOIN drift_actions a ON a.id=q.drift_action_id
                WHERE q.state IN ('PENDING','RUNNING') ORDER BY q.id""").fetchall()
        actions=[]
        for row in rows:
            try:detail=json.loads(row['result_json'] or '{}')
            except (TypeError,ValueError):detail={}
            actions.append({'queue_id':int(row['queue_id']),'drift_action_id':int(row['drift_action_id']),'drift_type':str(row['drift_type']),'action':str(row['requested_action']),'execution_status':'QUEUED','affected_component':str(row['affected_component']),'result':detail})
        return await self._consume_drift_remediations(actions) if actions else []

    async def _consume_drift_remediations(self,actions:list[dict[str,Any]])->list[dict[str,Any]]:
        """Execute durable drift remediation requests and record real completion.

        A drift action is not considered executed until the intended subsystem has
        actually changed or a governed retraining/review operation has run.
        """
        completed=[]
        for action in actions:
            if action.get('execution_status')!='QUEUED' or not action.get('queue_id'):
                completed.append(action);continue
            queue_id=int(action['queue_id']);action_id=int(action['drift_action_id']);drift_type=str(action['drift_type'])
            with connect(self.path,self.settings) as con:
                q=con.execute('SELECT * FROM drift_remediation_queue WHERE id=?',(queue_id,)).fetchone()
                if not q:continue
                payload=json.loads(q['payload_json'] or '{}');remediation=str(q['remediation_type']);now=time.time()
                con.execute("UPDATE drift_remediation_queue SET state='RUNNING',attempts=attempts+1,updated_at=? WHERE id=?",(now,queue_id))
            try:
                result:dict[str,Any]={'remediation':remediation}
                if remediation=='RETRAIN_AFFECTED_MODELS':
                    features=set(payload.get('features') or [])
                    affected=sorted(name for name,names in EXPERT_FEATURES.items() if features.intersection(names))
                    specialist=await asyncio.to_thread(self.training.train_eligible)
                    decision=await asyncio.to_thread(self.training.train_decision_eligible)
                    meta=await asyncio.to_thread(self.training.train_meta_eligible)
                    result.update({'affected_experts':affected,'specialist_results':specialist,'decision_results':decision,'meta_result':meta})
                elif remediation=='RECALIBRATE_MODELS':
                    calibration=await self.train_challenger()
                    meta=await asyncio.to_thread(self.training.train_meta_eligible)
                    result.update({'calibration_challenger':calibration,'meta_shadow':meta})
                elif remediation=='FORCE_SHADOW_REVIEW':
                    await self.brain.set_meta('signals_enabled','0')
                    specialist=await asyncio.to_thread(self.training.train_eligible)
                    decision=await asyncio.to_thread(self.training.train_decision_eligible)
                    meta=await asyncio.to_thread(self.training.train_meta_eligible)
                    result.update({'signals_enabled':False,'specialist_results':specialist,'decision_results':decision,'meta_result':meta})
                elif remediation=='RETRAIN_REGIME_AND_RAISE_UNCERTAINTY':
                    regime=await asyncio.to_thread(self.regime_governance.train,self.training.minimum_rows)
                    result.update({'regime_training':regime,'uncertainty_multiplier':float(payload.get('uncertainty_multiplier') or 1.5)})
                elif remediation=='REBUILD_ASSET_PROFILES':
                    rebuilt=await self.update_knowledge()
                    result.update({'profiles_rebuilt':payload.get('assets') or [],'knowledge_rows':rebuilt})
                elif remediation=='CONSERVATIVE_MODE_AND_DATA_REVIEW':
                    await self.brain.set_meta('signals_enabled','0')
                    result.update({'signals_enabled':False,'uncertainty_multiplier':float(payload.get('uncertainty_multiplier') or 1.25),'data_review':'runtime_conservative_mode_enabled'})
                else:
                    raise RuntimeError(f'unknown remediation type {remediation}')
                now=time.time()
                with connect(self.path,self.settings) as con:
                    con.execute("UPDATE drift_remediation_queue SET state='COMPLETED',last_error=NULL,updated_at=?,completed_at=? WHERE id=?",(now,now,queue_id))
                    con.execute("UPDATE drift_actions SET execution_status='COMPLETED',execution_time=?,result_json=? WHERE id=?",(now,json.dumps(result,sort_keys=True,default=str),action_id))
                completed.append({**action,'execution_status':'COMPLETED','result':result})
            except Exception as exc:
                now=time.time();error=f'{type(exc).__name__}: {exc}'[:2000]
                with connect(self.path,self.settings) as con:
                    con.execute("UPDATE drift_remediation_queue SET state='FAILED',last_error=?,updated_at=? WHERE id=?",(error,now,queue_id))
                    con.execute("UPDATE drift_actions SET execution_status='FAILED',execution_time=?,result_json=? WHERE id=?",(now,json.dumps({'error':error},sort_keys=True),action_id))
                completed.append({**action,'execution_status':'FAILED','result':{'error':error}})
        return completed

    async def detect_drift(self) -> dict[str, Any]:
        older,newer,scope=await asyncio.to_thread(self.drift_collector.windows,400)
        samples=len(older.get('labels',[]))+len(newer.get('labels',[]))
        if samples < 100:
            return {"detected": False, "reason": "insufficient_samples", "samples": samples,"source":"persisted_runtime_windows"}
        result=await asyncio.to_thread(self.drift_engine.evaluate,older,newer,True,scope)
        detected=[name for name,item in result['types'].items() if item['detected']]
        requested=await asyncio.to_thread(self.drift_engine.execute_actions,result)
        executed=await self._consume_drift_remediations(requested)
        result['requested_actions']=requested
        result['executed_actions']=executed
        for drift_type in detected:
            action_evidence=next((item for item in executed if item['drift_type']==drift_type),{})
            await self.brain.audit('research','SCOPED_DRIFT_ACTION_EXECUTED','drift',drift_type,{'action':result['types'][drift_type]['action'],'scope':scope,'global_signal_disable':False,'execution':action_evidence})
        return result

    async def qualification(self) -> dict[str, Any]:
        warmup = await self.warmup.status()
        age = float(warmup['observation_runtime_seconds']) + float(warmup['shadow_runtime_seconds'])
        rows = await asyncio.to_thread(self._learning_rows_sync)
        summary = await self.brain.summary()
        events = int(summary["counts"]["events"])
        responses = int(summary["counts"]["market_responses"])
        brier = brier_score([float(x["probability"]) for x in rows], [int(x["label"]) for x in rows]) if rows else math.inf
        ece = calibration_error([float(x["probability"]) for x in rows], [int(x["label"]) for x in rows]) if rows else math.inf
        recent_drift = False
        with connect(self.path, self.settings) as con:
            recent_drift = con.execute("SELECT 1 FROM drift_events WHERE created_at>? LIMIT 1", (time.time() - 86400,)).fetchone() is not None
            meta=con.execute("SELECT artifact_id FROM model_artifacts WHERE expert='meta' AND state='CHAMPION' AND promoted_at IS NOT NULL").fetchone()
            specialist_champions=int(con.execute("SELECT COUNT(*) count FROM model_artifacts WHERE expert NOT IN ('meta','entry','exit','risk','regime_transition','magnitude_return','magnitude_mfe','magnitude_mae','time_to_peak') AND expert NOT LIKE 'horizon_score:%' AND state='CHAMPION'").fetchone()['count'])
            meta_validation=bool(meta and con.execute("SELECT 1 FROM improvement_records WHERE model_version=? AND validation_window='unseen_runtime_shadow' AND sample_size>=?",(meta['artifact_id'],self.training.minimum_shadow)).fetchone())
        checks = {
            "warmup_48h": float(warmup['observation_runtime_seconds']) >= 86400 and float(warmup['shadow_runtime_seconds']) >= 86400,
            "events": events >= self.qualification_min_events,
            "responses": responses >= self.qualification_min_responses,
            "brier": brier <= self.max_brier,
            "calibration_error": ece <= self.max_ece,
            "no_recent_drift": not recent_drift,
            "meta_champion": bool(meta),
            "specialist_champions": specialist_champions >= self.qualification_min_specialists,
            "meta_shadow_validation": meta_validation,
        }
        qualified = all(checks.values())
        await self.brain.set_meta("signals_enabled", "1" if qualified else "0")
        result = {"qualified": qualified, "checks": checks, "accumulated_runtime_seconds": age, "warmup": warmup, "events": events, "responses": responses, "brier": brier, "calibration_error": ece,"meta_artifact_id":str(meta['artifact_id']) if meta else None,"specialist_champions":specialist_champions}
        await self.brain.audit("research", "QUALIFICATION_EVALUATED", "deployment", "current", result)
        return result

    async def daily_report(self) -> dict[str, Any]:
        source_learning=await asyncio.to_thread(self._update_source_reliability_sync)
        summary = await self.brain.summary()
        lifecycle = await self.review_signal_lifecycle()
        drift = await self.detect_drift()
        qualification = await self.qualification()
        mode = await self.brain.mode()
        with connect(self.path,self.settings) as con:
            by_expert=[dict(r) for r in con.execute("SELECT eo.expert,COUNT(*) predictions,AVG((eo.probability-(CASE WHEN mr.return_value>0 THEN 1 ELSE 0 END))*(eo.probability-(CASE WHEN mr.return_value>0 THEN 1 ELSE 0 END))) brier FROM expert_outputs eo JOIN feature_vectors fv ON fv.feature_id=eo.feature_id JOIN market_responses mr ON mr.event_id=fv.event_id AND mr.asset_id=fv.asset_id AND mr.horizon_seconds=fv.horizon_seconds GROUP BY eo.expert ORDER BY brier").fetchall()]
            by_asset=[dict(r) for r in con.execute("SELECT asset_id,COUNT(*) outcomes,AVG(return_value) mean_return,AVG(mfe) mean_mfe,AVG(mae) mean_mae FROM market_responses GROUP BY asset_id").fetchall()]
            by_horizon=[dict(r) for r in con.execute("SELECT horizon_seconds,COUNT(*) outcomes,AVG(return_value) mean_return FROM market_responses GROUP BY horizon_seconds").fetchall()]
            by_regime=[dict(r) for r in con.execute("SELECT regime,COUNT(*) observations,AVG(confidence) confidence FROM regimes GROUP BY regime").fetchall()]
            failures=[dict(r) for r in con.execute("SELECT category,COUNT(*) count FROM failure_attribution GROUP BY category ORDER BY count DESC").fetchall()]
            hypotheses=[dict(r) for r in con.execute("SELECT status,COUNT(*) count FROM hypotheses GROUP BY status").fetchall()]
            models=[dict(r) for r in con.execute("SELECT expert,state,COUNT(*) count FROM model_artifacts GROUP BY expert,state").fetchall()]
            improvements=[dict(r) for r in con.execute("SELECT * FROM improvement_records ORDER BY id DESC LIMIT 50").fetchall()]
            source_profiles=[dict(r) for r in con.execute("SELECT source_id,observations,primary_count,correction_count,duplicate_count,reliability,CASE WHEN observations>0 THEN 1.0-correction_count*1.0/observations END parse_and_correction_quality,CASE WHEN observations>0 THEN duplicate_count*1.0/observations END duplicate_rate,CASE WHEN latency_samples>0 THEN latency_sum/latency_samples ELSE NULL END mean_latency_seconds FROM source_profiles ORDER BY reliability DESC,observations DESC LIMIT 50").fetchall()]
            entry_metrics=dict(con.execute("""SELECT COUNT(*) samples,
                AVG(CASE WHEN (s.action IN ('BUY','ENTER_ON_CONFIRMATION','ENTER_ON_PULLBACK') AND mr.return_value>0) OR (s.action NOT IN ('BUY','ENTER_ON_CONFIRMATION','ENTER_ON_PULLBACK') AND mr.return_value<0) THEN 1.0 ELSE 0.0 END) directional_precision,
                AVG(ABS(mr.mfe)) mfe_capture_opportunity,AVG(ABS(mr.mae)) entry_adverse_excursion
                FROM signals s JOIN predictions p ON p.id=s.prediction_id JOIN market_responses mr ON mr.event_id=p.event_id AND mr.asset_id=p.asset_id AND mr.horizon_seconds=p.horizon_seconds""").fetchone())
            entry_by_style=[dict(r) for r in con.execute("""SELECT CASE
                WHEN UPPER(s.entry_logic) LIKE 'IMMEDIATE:%' THEN 'IMMEDIATE' WHEN UPPER(s.entry_logic) LIKE 'PULLBACK:%' THEN 'PULLBACK'
                WHEN UPPER(s.entry_logic) LIKE 'CONFIRMATION:%' THEN 'CONFIRMATION' ELSE 'WAIT' END style,COUNT(*) samples,
                AVG(p.expected_value) entry_ev,AVG(CASE WHEN (s.action IN ('BUY','ENTER_ON_CONFIRMATION','ENTER_ON_PULLBACK') AND mr.return_value>0) OR (s.action NOT IN ('BUY','ENTER_ON_CONFIRMATION','ENTER_ON_PULLBACK') AND mr.return_value<0) THEN 1.0 ELSE 0.0 END) hit_rate,
                AVG(mr.mfe) mfe,AVG(mr.mae) mae,AVG(MAX(0,s.opened_at-p.created_at)) entry_delay_seconds,
                AVG(CASE WHEN ABS(mr.mfe)<.001 AND ABS(mr.mae)>=.003 THEN 1.0 ELSE 0.0 END) false_start_rate,
                AVG(mr.return_value-p.expected_magnitude) delay_cost_proxy
                FROM signals s JOIN predictions p ON p.id=s.prediction_id JOIN market_responses mr ON mr.event_id=p.event_id AND mr.asset_id=p.asset_id AND mr.horizon_seconds=p.horizon_seconds GROUP BY style""").fetchall()]
            exit_metrics=dict(con.execute("""SELECT COUNT(*) samples,AVG(return_after_exit) mean_direction_normalized_return_after_exit,
                AVG(CASE WHEN classification IN ('GOOD_EXIT','LOSS_AVOIDED') THEN 1.0 ELSE 0.0 END) good_exit_rate,
                AVG(CASE WHEN classification='EARLY_EXIT' THEN 1.0 ELSE 0.0 END) early_exit_rate,
                AVG(CASE WHEN classification='LATE_EXIT' THEN 1.0 ELSE 0.0 END) late_exit_rate,
                AVG(CASE WHEN classification='FALSE_REVERSAL' THEN 1.0 ELSE 0.0 END) false_reversal_rate,
                AVG(CASE WHEN classification='LOSS_AVOIDED' THEN 1.0 ELSE 0.0 END) loss_avoided_rate,
                AVG(CASE WHEN classification='MISSED_CONTINUATION' THEN 1.0 ELSE 0.0 END) missed_continuation_rate,
                AVG(max_gain_after_exit) missed_continuation_magnitude,AVG(ABS(max_loss_after_exit)) avoided_loss_magnitude
                FROM post_exit""").fetchone())
            partial_metrics=dict(con.execute("""SELECT COUNT(*) partial_signals,AVG(partial_realized_result) partial_exit_effectiveness,
                AVG(position_fraction) mean_remaining_fraction FROM signals WHERE partial_exit_count>0""").fetchone())
            trail_metrics=dict(con.execute("""SELECT COUNT(*) trail_decisions,AVG(return_since_entry) trail_return_at_decision
                FROM active_signal_snapshots WHERE action='TRAIL'""").fetchone())
            post_exit_distribution=[dict(r) for r in con.execute("SELECT horizon_seconds,COUNT(*) samples,AVG(return_after_exit) mean,MIN(return_after_exit) minimum,MAX(return_after_exit) maximum FROM post_exit GROUP BY horizon_seconds ORDER BY horizon_seconds").fetchall()]
            exit_models=[dict(r) for r in con.execute("SELECT ma.expert,ma.state,ma.artifact_id,ma.model_family,ma.metrics_json FROM model_artifacts ma WHERE ma.expert IN ('exit_policy','exit_quality','post_exit_optimizer','exit') ORDER BY ma.created_at DESC").fetchall()]
        report = {"date": time.strftime("%Y-%m-%d", time.gmtime()), "mode": mode, "summary": summary, "lifecycle": lifecycle,
                  "performance":{"resolved_outcomes":summary['counts'].get('market_responses',0),"brier":qualification['brier'],"ece":qualification['calibration_error']},
                  "per_expert":by_expert,"per_asset":by_asset,"per_horizon":by_horizon,"per_regime":by_regime,
                  "entry_quality":{"realized_metrics":entry_metrics,"by_style":entry_by_style,"models":[x for x in models if x['expert']=='entry']},
                  "exit_quality":{"realized_metrics":exit_metrics,"partial_exit":partial_metrics,"trail":trail_metrics,"post_exit_return_distribution":post_exit_distribution,"governed_models":exit_models,"models":[x for x in models if x['expert'] in {'exit','exit_policy','exit_quality','post_exit_optimizer'}]},
                  "missed_opportunities":lifecycle.get('missed',0),"failure_reasons":failures,"hypotheses":hypotheses,
                  "model_governance":models,"before_after_improvements":improvements,"source_learning":source_learning,"source_profiles":source_profiles,"drift": drift, "qualification": qualification}
        with connect(self.path, self.settings) as con:
            con.execute(
                "INSERT INTO daily_reports(report_date,mode,report_json,created_at) VALUES(?,?,?,?) ON CONFLICT(report_date) DO UPDATE SET mode=excluded.mode,report_json=excluded.report_json,created_at=excluded.created_at",
                (report["date"], mode, json.dumps(report, ensure_ascii=False, default=str), time.time()),
            )
        if self.settings.telegram_enabled:
            await self.telegram.produce({**report,'report_date':report['date'],'daily_report_ready':True})
        return report

    async def _cycle(
        self,
        stop_event: asyncio.Event | None = None,
    ) -> None:

        def stopping() -> bool:
            return bool(
                stop_event is not None
                and stop_event.is_set()
            )

        async def stage(name: str, awaitable):
            if stopping():
                self.metrics.inc(
                    "research_cycle_stop_checkpoint"
                )
                logger.info(
                    "research cycle stop checkpoint "
                    "before_stage=%s",
                    name,
                )
                return None, True

            started = time.monotonic()

            logger.info(
                "research stage started stage=%s",
                name,
            )

            value = await awaitable

            elapsed = time.monotonic() - started

            self.metrics.set(
                f"research_stage_seconds_{name}",
                elapsed,
            )

            logger.info(
                "research stage completed "
                "stage=%s seconds=%.3f",
                name,
                elapsed,
            )

            return value, stopping()

        recovered_drift_actions, stop = await stage(
            "recover_drift",
            self._recover_drift_remediations(),
        )
        if stop:
            return

        learned, stop = await stage(
            "update_knowledge",
            self.update_knowledge(),
        )
        if stop:
            return

        resolved, stop = await stage(
            "resolve_shadow",
            asyncio.to_thread(
                self.training.resolve_shadow_outcomes
            ),
        )
        if stop:
            return

        challengers, stop = await stage(
            "train_specialists",
            asyncio.to_thread(
                self.training.train_eligible
            ),
        )
        if stop:
            return

        decision_challengers, stop = await stage(
            "train_decisions",
            asyncio.to_thread(
                self.training.train_decision_eligible
            ),
        )
        if stop:
            return

        post_exit_challenger, stop = await stage(
            "train_exit",
            asyncio.to_thread(
                self.training.train_exit_from_post_exit
            ),
        )
        if stop:
            return

        warmup_status = await self.warmup.status()

        runtime_age = (
            float(
                warmup_status.get(
                    'observation_runtime_seconds'
                ) or 0.0
            )
            +
            float(
                warmup_status.get(
                    'shadow_runtime_seconds'
                ) or 0.0
            )
        )

        temporal_after = max(
            0.0,
            float(
                os.getenv(
                    'RESEARCH_TEMPORAL_MIN_RUNTIME_SECONDS',
                    '3600',
                )
            ),
        )

        if runtime_age >= temporal_after:
            temporal_challengers, stop = await stage(
                "train_temporal",
                asyncio.to_thread(
                    self.training.train_temporal_eligible
                ),
            )
            if stop:
                return
        else:
            temporal_challengers = [{
                'status': 'DEFERRED_WARMUP',
                'runtime_seconds': runtime_age,
                'minimum_runtime_seconds': temporal_after,
            }]
            self.metrics.inc(
                'research_temporal_deferred_warmup'
            )

        regime_challengers, stop = await stage(
            "train_regime",
            asyncio.to_thread(
                self.regime_governance.train,
                self.training.minimum_rows,
            ),
        )
        if stop:
            return

        interaction_after = max(
            0.0,
            float(
                os.getenv(
                    'RESEARCH_INTERACTION_MIN_RUNTIME_SECONDS',
                    '1800',
                )
            ),
        )

        if runtime_age >= interaction_after:
            interaction_results, stop = await stage(
                "interactions",
                asyncio.to_thread(
                    self.interactions.discover
                ),
            )
            if stop:
                return
        else:
            interaction_results = {
                'executed': False,
                'reason': 'deferred_warmup',
                'runtime_seconds': runtime_age,
                'minimum_runtime_seconds':
                    interaction_after,
            }

            self.metrics.inc(
                'research_interactions_deferred_warmup'
            )

        meta_challenger, stop = await stage(
            "train_meta",
            asyncio.to_thread(
                self.training.train_meta_eligible
            ),
        )
        if stop:
            return

        shadow_results, stop = await stage(
            "evaluate_shadows",
            asyncio.to_thread(
                self.training.evaluate_shadows
            ),
        )
        if stop:
            return

        if self.settings.telegram_enabled:
            for promoted in (
                item
                for item in shadow_results
                if item.get('promoted')
            ):
                if stopping():
                    return

                await self.telegram.produce({
                    'knowledge_promoted': True,
                    'oos_validated': True,
                    'summary':
                        f"{promoted['expert']} promoted "
                        f"after unseen shadow outcomes",
                    'asset_id': 'GLOBAL',
                    'event_id': promoted['artifact_id'],
                })

        if stopping():
            return

        report, stop = await stage(
            "daily_report",
            self.daily_report(),
        )

        if stop:
            return

        report['training'] = {
            'recovered_drift_actions':
                recovered_drift_actions,
            'resolved_shadow_outcomes':
                resolved,
            'challengers':
                challengers,
            'decision_challengers':
                decision_challengers,
            'post_exit_challenger':
                post_exit_challenger,
            'temporal_challengers':
                temporal_challengers,
            'regime_challengers':
                regime_challengers,
            'interaction_experiments':
                interaction_results,
            'meta_challenger':
                meta_challenger,
            'shadow_results':
                shadow_results,
        }

        self.metrics.set(
            "research_report",
            report,
        )

        self.metrics.inc(
            "knowledge_patterns_updated",
            learned,
        )

    async def run(
        self,
        stop_event: asyncio.Event,
    ) -> None:

        interval = max(
            60.0,
            float(
                os.getenv(
                    "RESEARCH_CYCLE_SECONDS",
                    "900",
                )
            ),
        )

        initial_delay = max(
            0.0,
            float(
                os.getenv(
                    "RESEARCH_INITIAL_DELAY_SECONDS",
                    "120",
                )
            ),
        )

        # Collectors and Intelligence must get a short head start.
        if (
            initial_delay > 0
            and not stop_event.is_set()
        ):
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=initial_delay,
                )
                return
            except asyncio.TimeoutError:
                pass

        while not stop_event.is_set():
            try:
                await self._cycle(stop_event)

                if stop_event.is_set():
                    break

                self.metrics.inc(
                    "research_cycle_completed"
                )

            except asyncio.CancelledError:
                raise

            except Exception:
                self.metrics.inc(
                    "research_cycle_error"
                )
                logger.exception(
                    "research cycle failed"
                )

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=interval,
                )
            except asyncio.TimeoutError:
                continue
