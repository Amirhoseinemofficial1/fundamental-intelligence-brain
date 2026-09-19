from __future__ import annotations

import hashlib
import json
import os
import platform
import statistics
import time
import uuid
from typing import Any

from ..config import Settings
from ..storage.sqlite import connect
from .experts import brier_score,calibration_error
from .model_factory import ModelFactory
from .temporal_models import TemporalModelFactory

class TemporalGovernance:
    """Dataset -> purged walk-forward Candidate -> Challenger -> Shadow artifacts."""
    def __init__(self,settings:Settings):self.settings=settings;self.path=settings.data_dir/'brain'/'intelligence.sqlite3';self.factory=TemporalModelFactory();self.registry=ModelFactory(settings).registry

    def train(self,rows:list[dict[str,Any]],expert_prefix:str='temporal_meta',sequence_length:int=8)->list[dict[str,Any]]:
        availability=self.factory.availability();minimum=max(32,sequence_length*4)
        if not availability['available']:return [{'family':family,'status':'NOT_QUALIFIED','reason':'dependency_missing'} for family in self.factory.FAMILIES]
        rows=sorted(rows,key=lambda row:float(row['prediction_time']))
        if len(rows)<minimum:return [{'family':family,'status':'NOT_QUALIFIED','reason':'insufficient_data','rows':len(rows),'minimum':minimum} for family in self.factory.FAMILIES]
        ModelFactory._validate_temporal_contract(rows);names=sorted({key for row in rows for key,value in row['values'].items() if isinstance(value,(int,float))})
        matrix=ModelFactory._matrix(rows,names);samples=[]
        for index in range(sequence_length-1,len(rows)):samples.append({'prediction_time':rows[index]['prediction_time'],'available_cutoff':rows[index].get('available_cutoff',rows[index]['prediction_time']),'sequence':matrix[index-sequence_length+1:index+1],'label':int(rows[index]['label'])})
        try:folds=ModelFactory._walk_forward_folds(samples,fold_count=2,purge_rows=1,embargo_rows=1)
        except ValueError:return [{'family':family,'status':'NOT_QUALIFIED','reason':'insufficient_purged_folds','rows':len(rows)} for family in self.factory.FAMILIES]
        epochs=max(1,int(os.getenv('TEMPORAL_GOVERNANCE_EPOCHS','2')));created=[]
        import torch
        for family in self.factory.FAMILIES:
            expert=f'{expert_prefix}:{family}'
            with connect(self.path,self.settings) as con:
                if con.execute("SELECT 1 FROM model_artifacts WHERE expert=? AND state IN ('SHADOW','CHAMPION')",(expert,)).fetchone():created.append({'family':family,'expert':expert,'status':'ALREADY_GOVERNED'});continue
            oos=[];truth=[];fold_metrics=[]
            try:
                for train_idx,val_idx in folds:
                    artifact=self.factory.train(family,[samples[i]['sequence'] for i in train_idx],[samples[i]['label'] for i in train_idx],epochs=epochs,seed=55)
                    probabilities=artifact.predict([samples[i]['sequence'] for i in val_idx]);labels=[samples[i]['label'] for i in val_idx];oos.extend(probabilities);truth.extend(labels);fold_metrics.append(brier_score(probabilities,labels))
                final=self.factory.train(family,[x['sequence'] for x in samples],[x['label'] for x in samples],epochs=epochs,seed=55)
            except (ValueError,RuntimeError) as exc:created.append({'family':family,'expert':expert,'status':'NOT_QUALIFIED','reason':type(exc).__name__});continue
            canonical=json.dumps([{'t':x['prediction_time'],'y':x['label'],'s':x['sequence']} for x in samples],sort_keys=True,separators=(',',':')).encode();dataset_sha=hashlib.sha256(canonical).hexdigest();dataset_id=f'dataset:{expert}:{dataset_sha[:16]}';artifact_id=f'artifact:{expert}:{family}:{uuid.uuid4().hex[:12]}';now=time.time();path=self.registry.directory/(artifact_id.replace(':','_')+'.json');model_path=path.with_suffix('.pt');torch.save(final.model.state_dict(),model_path);model_sha=hashlib.sha256(model_path.read_bytes()).hexdigest()
            metrics={'brier':brier_score(oos,truth),'ece':calibration_error(oos,truth),'stability':statistics.pstdev(fold_metrics) if len(fold_metrics)>1 else 0.0,'walk_forward_folds':len(folds),'purge_rows':1,'embargo_rows':1,'samples':len(samples),'validation_samples':len(truth)}
            meta={'artifact_id':artifact_id,'expert_name':expert,'task':'temporal_classification','model_family':family,'model_version':'57.3.0','dataset_version':dataset_id,'feature_version':rows[0].get('feature_version','features-57.3.0'),'feature_names':names,'sequence_length':sequence_length,'feature_count':len(names),'training_start':rows[0]['prediction_time'],'training_end':rows[-1]['prediction_time'],'validation_start':samples[folds[0][1][0]]['prediction_time'],'validation_end':samples[folds[-1][1][-1]]['prediction_time'],'hyperparameters':{'epochs':epochs},'metrics':metrics,'calibration':{},'created_at':now,'seed':56,'environment':{'python':platform.python_version(),'torch':torch.__version__},'estimator':{'kind':'temporal_torch'},'model_sha256':model_sha}
            raw=json.dumps(meta,sort_keys=True,separators=(',',':'),default=str).encode();path.write_bytes(raw);artifact_sha=hashlib.sha256(raw).hexdigest()
            with connect(self.path,self.settings) as con:
                con.execute("INSERT OR IGNORE INTO datasets(dataset_id,expert,asset_scope,horizon_seconds,start_at,end_at,row_count,sha256,feature_version,label_version,specification_json,created_at) VALUES(?,?,'*',0,?,?,?,?,?,'direction-v2-temporal',?,?)",(dataset_id,expert,float(rows[0]['prediction_time']),float(rows[-1]['prediction_time']),len(samples),dataset_sha,meta['feature_version'],json.dumps({'sequence_length':sequence_length,'purged_walk_forward':True}),now))
                con.execute("INSERT INTO model_artifacts(artifact_id,expert,model_family,state,artifact_path,artifact_sha256,dataset_id,feature_version,code_version,seed,training_start,training_end,validation_start,validation_end,hyperparameters_json,metrics_json,calibration_json,created_at) VALUES(?,?,?,'SHADOW',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(artifact_id,expert,family,str(path),artifact_sha,dataset_id,meta['feature_version'],'57.3.0',56,meta['training_start'],meta['training_end'],meta['validation_start'],meta['validation_end'],json.dumps(meta['hyperparameters']),json.dumps(metrics),json.dumps({}),now))
                for action,state in (('TEMPORAL_DATASET_BUILT','DATASET'),('TEMPORAL_CANDIDATE_EVALUATED','CANDIDATE'),('TEMPORAL_CHALLENGER_QUALIFIED','CHALLENGER'),('TEMPORAL_SHADOW_STARTED','SHADOW')):con.execute("INSERT INTO audit_log(actor,action,object_type,object_id,detail_json,created_at) VALUES('temporal_governance',?,'artifact',?,?,?)",(action,artifact_id,json.dumps({'state':state,'family':family,'metrics':metrics}),now))
            created.append({'family':family,'expert':expert,'artifact_id':artifact_id,'status':'SHADOW','metrics':metrics})
        return created
