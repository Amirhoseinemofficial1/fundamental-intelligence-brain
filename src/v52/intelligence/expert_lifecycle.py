from __future__ import annotations
import json,time
from typing import Any
from ..config import Settings
from ..storage.sqlite import connect,transaction
from .learning import EXPERT_FEATURES,TrainingCoordinator
from .model_factory import ArtifactRegistry,ModelFactory

class TrainableExpertLifecycle:
    """Executable lifecycle facade used by every specialist, not a marker interface."""
    def __init__(self,settings:Settings,expert_name:str):self.settings=settings;self.expert_name=expert_name;self.coordinator=TrainingCoordinator(settings);self.factory=ModelFactory(settings);self.registry=ArtifactRegistry(settings)
    def build_dataset(self):return self.coordinator._rows_for(self.expert_name,self.coordinator.learning_rows())
    def validate_dataset(self,rows):return {'valid':len(rows)>=self.coordinator.minimum_rows and len({int(r['label']) for r in rows})>1,'rows':len(rows),'chronological':all(rows[i]['prediction_time']<=rows[i+1]['prediction_time'] for i in range(len(rows)-1))}
    def select_features(self,rows):
        names=EXPERT_FEATURES.get(self.expert_name,())
        return [{**r,'values':{name:float(r['values'].get(name,0)) for name in names}} for r in rows]
    def train_candidates(self,rows):
        validation=self.validate_dataset(rows)
        if not validation['valid']:raise ValueError(f"invalid {self.expert_name} dataset: {validation}")
        prepared=self.select_features(rows);return self.factory.train(self.expert_name,prepared,state='SHADOW')
    def evaluate_candidates(self,result):return dict(result['metrics'])
    def select_candidate(self,result):return result['artifact_id']
    def calibrate(self,artifact_id):return self.registry.load(artifact_id).metadata['calibration']
    def serialize_artifact(self,artifact_id):
        artifact=self.registry.load(artifact_id);return {'artifact_id':artifact_id,'path':str(self.registry.directory),'sha256_verified':True,'metadata':artifact.metadata}
    def register_challenger(self,rows):return self.train_candidates(rows)
    def shadow_infer(self,values):return [{'artifact_id':a.metadata['artifact_id'],'probability':a.predict(values)} for a in self.registry.shadows(self.expert_name)]
    def evaluate_shadow(self):return [x for x in self.coordinator.evaluate_shadows() if x['expert']==self.expert_name]
    def promote(self,artifact_id):
        with connect(self.registry.path,self.settings) as con:
            transaction(con);now=time.time();current=con.execute("SELECT artifact_id FROM model_artifacts WHERE expert=? AND state='CHAMPION'",(self.expert_name,)).fetchone()
            if current:con.execute("UPDATE model_artifacts SET state='RETIRED',retired_at=? WHERE artifact_id=?",(now,current['artifact_id']))
            con.execute("UPDATE model_artifacts SET state='CHAMPION',promoted_at=? WHERE artifact_id=? AND expert=? AND state='SHADOW'",(now,artifact_id,self.expert_name));con.execute('COMMIT')
        return self.registry.champion(self.expert_name)
    def reject(self,artifact_id):
        with connect(self.registry.path,self.settings) as con:con.execute("UPDATE model_artifacts SET state='REJECTED' WHERE artifact_id=? AND expert=? AND state='SHADOW'",(artifact_id,self.expert_name))
    def rollback(self,target_id=None):return self.registry.rollback(self.expert_name,target_id)
    def infer(self,values):
        artifact=self.registry.champion(self.expert_name)
        if not artifact:raise RuntimeError(f'no champion artifact for {self.expert_name}')
        return {'expert':self.expert_name,'artifact_id':artifact.metadata['artifact_id'],'probability':artifact.predict(values)}
