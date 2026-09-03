from __future__ import annotations

import hashlib
import json
import math
import time
import statistics
from itertools import combinations
from typing import Any

from ..config import Settings
from ..storage.sqlite import connect


def _solve(matrix: list[list[float]], labels: list[float], ridge: float=1e-6) -> list[float]:
    width=len(matrix[0]);a=[[sum(row[i]*row[j] for row in matrix)+(ridge if i==j else 0.0) for j in range(width)]+[sum(row[i]*y for row,y in zip(matrix,labels))] for i in range(width)]
    for column in range(width):
        pivot=max(range(column,width),key=lambda row:abs(a[row][column]));a[column],a[pivot]=a[pivot],a[column];scale=a[column][column] or 1e-12;a[column]=[value/scale for value in a[column]]
        for row in range(width):
            if row==column:continue
            factor=a[row][column];a[row]=[value-factor*base for value,base in zip(a[row],a[column])]
    return [a[index][-1] for index in range(width)]


class FeatureInteractionHypothesisEngine:
    """Runs chronological interaction experiments; only validated effects become knowledge."""

    def __init__(self,settings:Settings):self.settings=settings;self.path=settings.data_dir/'brain'/'intelligence.sqlite3'

    @staticmethod
    def _design(rows:list[dict[str,Any]],first:str,second:str,interaction:bool)->list[list[float]]:
        result=[]
        for row in rows:
            a=float(row['values'].get(first,0));b=float(row['values'].get(second,0));result.append([1.0,a,b]+([a*b] if interaction else []))
        return result

    @staticmethod
    def benjamini_hochberg(p_values:list[float],alpha:float=.05)->list[dict[str,Any]]:
        total=len(p_values);ordered=sorted(enumerate(p_values),key=lambda item:item[1]);adjusted=[1.0]*total;running=1.0
        for rank,(index,p_value) in reversed(list(enumerate(ordered,start=1))):
            running=min(running,float(p_value)*total/rank);adjusted[index]=min(1.0,running)
        return [{'p_value':float(p_values[i]),'q_value':adjusted[i],'rejected':adjusted[i]<=alpha} for i in range(total)]

    def experiment(self,first:str,second:str,minimum_rows:int=40,apply_promotion:bool=True)->dict[str,Any]:
        return self._experiment_features((first,second),minimum_rows,apply_promotion)

    def _experiment_features(self,features:tuple[str,...],minimum_rows:int=40,apply_promotion:bool=True)->dict[str,Any]:
        """Evaluate a real pair or triple interaction across expanding OOS folds."""
        with connect(self.path,self.settings) as con:
            records=con.execute("""SELECT fv.prediction_time,fv.values_json,mr.return_value FROM feature_vectors fv JOIN market_responses mr ON mr.event_id=fv.event_id AND mr.asset_id=fv.asset_id AND mr.horizon_seconds=fv.horizon_seconds WHERE fv.replay=0 ORDER BY fv.prediction_time,fv.feature_id""").fetchall()
        rows=[{'prediction_time':float(row['prediction_time']),'values':json.loads(row['values_json']),'label':float(row['return_value'])} for row in records]
        rows=[row for row in rows if all(name in row['values'] for name in features)]
        if len(rows)<minimum_rows:return {'executed':False,'reason':'insufficient_rows','rows':len(rows),'features':list(features)}
        def design(items:list[dict[str,Any]],interaction:bool)->list[list[float]]:
            matrix=[]
            for row in items:
                xs=[float(row['values'][name]) for name in features];columns=[1.0,*xs]
                if len(features)==3:columns.extend((xs[0]*xs[1],xs[0]*xs[2],xs[1]*xs[2]))
                if interaction:columns.append(math.prod(xs))
                matrix.append(columns)
            return matrix
        folds=[];coefficients=[]
        for fraction in (.55,.70,.85):
            split=max(20,int(len(rows)*fraction));end=min(len(rows),split+max(5,int(len(rows)*.15)))
            train,validation=rows[:max(1,split-1)],rows[split:end] # one-row purge embargo
            if len(validation)<3:continue
            base=_solve(design(train,False),[row['label'] for row in train]);interaction=_solve(design(train,True),[row['label'] for row in train])
            def mae(weights,enabled):return sum(abs(sum(w*x for w,x in zip(weights,m))-item['label']) for m,item in zip(design(validation,enabled),validation))/len(validation)
            base_mae=mae(base,False);interaction_mae=mae(interaction,True);coefficients.append(float(interaction[-1]));folds.append({'train_rows':len(train),'validation_rows':len(validation),'base_mae':base_mae,'interaction_mae':interaction_mae,'improvement':(base_mae-interaction_mae)/max(base_mae,1e-12)})
        if len(folds)<2:return {'executed':False,'reason':'insufficient_walk_forward_folds','rows':len(rows),'features':list(features)}
        base_mae=statistics.mean(x['base_mae'] for x in folds);interaction_mae=statistics.mean(x['interaction_mae'] for x in folds);improvement=statistics.mean(x['improvement'] for x in folds);coefficient=statistics.mean(coefficients);stderr=statistics.pstdev(coefficients)/math.sqrt(len(coefficients)) if len(coefficients)>1 else abs(coefficient);ci=(coefficient-1.96*stderr,coefficient+1.96*stderr);stability=sum((x>=0)==(coefficient>=0) for x in coefficients)/len(coefficients);z=abs(coefficient)/max(stderr,1e-9);p_value=min(1.0,math.exp(-.5*z*z));validated=math.isfinite(improvement) and improvement>=.02 and abs(coefficient)>1e-9 and stability>=.67
        key='*'.join(features);digest=hashlib.sha256(key.encode()).hexdigest()[:20];experiment_id=f'interaction:{digest}:{len(rows)}';hypothesis_id=f'hypothesis:interaction:{digest}';now=time.time();result={'executed':True,'features':list(features),'rows':len(rows),'validation_rows':sum(x['validation_rows'] for x in folds),'baseline_mae':base_mae,'interaction_mae':interaction_mae,'relative_improvement':improvement,'interaction_coefficient':coefficient,'confidence_interval':list(ci),'fold_stability':stability,'p_value':p_value,'validated':validated,'chronological_oos':True,'purged_walk_forward':True,'folds':folds,'repeatability':'multi_fold_pending_fdr'}
        with connect(self.path,self.settings) as con:
            con.execute("INSERT OR REPLACE INTO experiments(experiment_id,hypothesis,status,method,specification_json,result_json,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?)",(experiment_id,f'{key} interact','VALIDATED' if validated else 'REJECTED','purged_expanding_walk_forward_interaction',json.dumps({'features':list(features),'folds':len(folds),'purge':1}),json.dumps(result),now,now))
            statement=' x '.join(features)+' improves response prediction'
            con.execute("INSERT INTO hypotheses(hypothesis_id,statement,status,scope_json,evidence_json,effect_size,confidence_low,confidence_high,sample_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(hypothesis_id) DO UPDATE SET status=excluded.status,evidence_json=excluded.evidence_json,effect_size=excluded.effect_size,confidence_low=excluded.confidence_low,confidence_high=excluded.confidence_high,sample_count=excluded.sample_count,updated_at=excluded.updated_at",(hypothesis_id,statement,'VALIDATED' if validated else 'REJECTED',json.dumps({'features':list(features)}),json.dumps(result),coefficient,ci[0],ci[1],len(rows),now,now))
            if validated and apply_promotion:
                con.execute("INSERT INTO knowledge(knowledge_key,subject,predicate,object,level,sample_count,effect_mean,uncertainty,evidence_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(knowledge_key) DO UPDATE SET level=excluded.level,sample_count=excluded.sample_count,effect_mean=excluded.effect_mean,uncertainty=excluded.uncertainty,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at",(f'interaction:{digest}',features[0],'INTERACTS_WITH','*'.join(features[1:]),'MODERATE_EVIDENCE',len(rows),coefficient,interaction_mae,json.dumps(result),now))
            else:
                con.execute("DELETE FROM knowledge WHERE knowledge_key=?",(f'interaction:{digest}',))
        return result

    def experiment_triple(self,first:str,second:str,third:str,minimum_rows:int=40,apply_promotion:bool=True)->dict[str,Any]:return self._experiment_features((first,second,third),minimum_rows,apply_promotion)

    def discover(self,minimum_rows:int=40,complexity_budget:int=24,alpha:float=.05)->dict[str,Any]:
        with connect(self.path,self.settings) as con:
            records=con.execute("""SELECT fv.values_json,mr.return_value FROM feature_vectors fv JOIN market_responses mr ON mr.event_id=fv.event_id AND mr.asset_id=fv.asset_id AND mr.horizon_seconds=fv.horizon_seconds WHERE fv.replay=0 ORDER BY fv.prediction_time""").fetchall()
        rows=[(json.loads(r['values_json']),float(r['return_value'])) for r in records]
        if len(rows)<minimum_rows:return {'executed':False,'reason':'insufficient_rows','rows':len(rows),'candidates':[]}
        numeric=sorted({key for values,_ in rows for key,value in values.items() if isinstance(value,(int,float)) and not key.endswith(('__missing','__valid'))})
        effects=[]
        labels=[label for _,label in rows];label_mean=statistics.mean(labels);label_scale=max(statistics.pstdev(labels),1e-9)
        for name in numeric:
            pairs=[(float(values[name]),label) for values,label in rows if name in values]
            if len(pairs)<minimum_rows:continue
            xs=[x for x,_ in pairs];ys=[y for _,y in pairs];xm=statistics.mean(xs);ym=statistics.mean(ys);den=max(math.sqrt(sum((x-xm)**2 for x in xs)*sum((y-ym)**2 for y in ys)),1e-12);corr=sum((x-xm)*(y-ym) for x,y in pairs)/den
            if abs(corr)>=.02:effects.append((abs(corr),name,corr))
        selected=[name for _,name,_ in sorted(effects,reverse=True)[:max(3,min(12,complexity_budget//2))]]
        candidate_pairs=[]
        for first,second in combinations(selected,2):
            overlap=[values for values,_ in rows if first in values and second in values]
            if len(overlap)<minimum_rows:continue
            a=[float(x[first]) for x in overlap];b=[float(x[second]) for x in overlap];am=statistics.mean(a);bm=statistics.mean(b);den=max(math.sqrt(sum((x-am)**2 for x in a)*sum((x-bm)**2 for x in b)),1e-12);correlation=abs(sum((x-am)*(y-bm) for x,y in zip(a,b))/den)
            if correlation<.98:candidate_pairs.append((first,second))
            if len(candidate_pairs)>=complexity_budget:break
        triples=[tuple(item) for item in combinations(selected[:6],3)][:max(0,complexity_budget-len(candidate_pairs))]
        results=[self.experiment(a,b,minimum_rows,False) for a,b in candidate_pairs]+[self.experiment_triple(a,b,c,minimum_rows,False) for a,b,c in triples]
        executed=[result for result in results if result.get('executed')]
        adjustments=self.benjamini_hochberg([float(result['p_value']) for result in executed],alpha) if executed else []
        with connect(self.path,self.settings) as con:
            for result,adjustment in zip(executed,adjustments):
                result.update(adjustment);features=result['features'];first=features[0];second='*'.join(features[1:]);digest=hashlib.sha256('*'.join(features).encode()).hexdigest()[:20];promote=bool(result['validated'] and adjustment['rejected'] and result['rows']>=minimum_rows and result.get('fold_stability',0)>=.67)
                result['promoted']=promote;result['multiple_testing']='BENJAMINI_HOCHBERG_FDR';result['repeatability']='OOS_EFFECT_STABLE' if promote else 'NOT_PROMOTED'
                con.execute("UPDATE hypotheses SET status=?,evidence_json=?,updated_at=? WHERE hypothesis_id=?",('VALIDATED' if promote else 'REJECTED',json.dumps(result),time.time(),f'hypothesis:interaction:{digest}'))
                if promote:con.execute("INSERT INTO knowledge(knowledge_key,subject,predicate,object,level,sample_count,effect_mean,uncertainty,evidence_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(knowledge_key) DO UPDATE SET level=excluded.level,sample_count=excluded.sample_count,effect_mean=excluded.effect_mean,uncertainty=excluded.uncertainty,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at",(f'interaction:{digest}',first,'INTERACTS_WITH',second,'MODERATE_EVIDENCE',result['rows'],result['interaction_coefficient'],result['interaction_mae'],json.dumps(result),time.time()))
        return {'executed':True,'rows':len(rows),'registry_features':numeric,'selected_features':selected,'candidates':executed,'evaluated_triples':[list(x) for x in triples],'multiple_testing':'BENJAMINI_HOCHBERG_FDR','fdr_family':'ALL_EVALUATED_PAIRS_AND_TRIPLES','alpha':alpha}
