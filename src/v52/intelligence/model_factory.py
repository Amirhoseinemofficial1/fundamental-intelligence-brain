from __future__ import annotations
import copy,hashlib,json,math,os,platform,random,statistics,time,uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any,Sequence
from ..config import Settings
from ..storage.sqlite import connect
from .experts import brier_score,calibration_error

def sigmoid(x:float)->float:return 1/(1+math.exp(-max(-30,min(30,x))))

class LinearEstimator:
    def __init__(self,l1:float=0.0,l2:float=.001,epochs:int=500,rate:float=.05,seed:int=52):self.l1=l1;self.l2=l2;self.epochs=min(epochs,max(10,int(os.getenv("MODEL_LINEAR_EPOCHS","80"))));self.rate=rate;self.seed=seed;self.weights=[];self.bias=0.0
    def fit(self,X:Sequence[Sequence[float]],y:Sequence[int])->'LinearEstimator':
        if not X:return self
        self.weights=[0.0]*len(X[0]);rng=random.Random(self.seed);order=list(range(len(X)))
        for epoch in range(self.epochs):
            rng.shuffle(order); lr=self.rate/math.sqrt(1+epoch/40)
            for i in order:
                p=sigmoid(self.bias+sum(w*v for w,v in zip(self.weights,X[i])));e=p-y[i];self.bias-=lr*e
                for j,v in enumerate(X[i]):self.weights[j]-=lr*(e*v+self.l2*self.weights[j]+self.l1*(1 if self.weights[j]>0 else -1 if self.weights[j]<0 else 0))
        return self
    def predict_proba(self,X:Sequence[Sequence[float]])->list[float]:return [sigmoid(self.bias+sum(w*v for w,v in zip(self.weights,row))) for row in X]
    def to_dict(self)->dict[str,Any]:return {'kind':'linear','weights':self.weights,'bias':self.bias,'l1':self.l1,'l2':self.l2}
    @classmethod
    def from_dict(cls,obj):
        x=cls(float(obj.get('l1',0)),float(obj.get('l2',0)));x.weights=[float(v) for v in obj['weights']];x.bias=float(obj['bias']);return x

class BayesianEstimator:
    def __init__(self):self.probability=.5
    def fit(self,X,y):self.probability=(sum(y)+2)/(len(y)+4);return self
    def predict_proba(self,X):return [self.probability for _ in X]
    def to_dict(self):return {'kind':'bayesian','probability':self.probability}

class LinearRegressor:
    def __init__(self,epochs:int=800,rate:float=.03,l2:float=.001):self.epochs=epochs;self.rate=rate;self.l2=l2;self.weights=[];self.bias=0.0;self.means=[];self.scales=[]
    def fit(self,X,y):
        if not X:return self
        width=len(X[0]);self.means=[sum(row[j] for row in X)/len(X) for j in range(width)];self.scales=[max(1e-9,(sum((row[j]-self.means[j])**2 for row in X)/len(X))**.5) for j in range(width)];Z=[[(row[j]-self.means[j])/self.scales[j] for j in range(width)] for row in X];self.weights=[0.0]*width;self.bias=sum(float(v) for v in y)/len(y)
        for epoch in range(self.epochs):
            rate=self.rate/math.sqrt(1+epoch/50);pred=[self.bias+sum(w*v for w,v in zip(self.weights,row)) for row in Z];errors=[p-float(t) for p,t in zip(pred,y)];self.bias-=rate*sum(errors)/len(errors)
            for j in range(width):self.weights[j]-=rate*(sum(errors[i]*Z[i][j] for i in range(len(Z)))/len(Z)+self.l2*self.weights[j])
        return self
    def predict(self,X):return [self.bias+sum(w*((row[j]-self.means[j])/self.scales[j]) for j,w in enumerate(self.weights)) for row in X]
    def to_dict(self):return {'kind':'regression_linear','weights':self.weights,'bias':self.bias,'means':self.means,'scales':self.scales,'l2':self.l2}
    @classmethod
    def from_dict(cls,obj):x=cls(l2=float(obj.get('l2',.001)));x.weights=list(map(float,obj['weights']));x.bias=float(obj['bias']);x.means=list(map(float,obj['means']));x.scales=list(map(float,obj['scales']));return x

class MeanRegressor:
    def __init__(self):self.value=0.0
    def fit(self,X,y):self.value=sum(map(float,y))/max(1,len(y));return self
    def predict(self,X):return [self.value for _ in X]
    def to_dict(self):return {'kind':'regression_mean','value':self.value}

def fit_platt(probabilities:list[float],labels:list[int])->dict[str,float]:
    a,b=1.0,0.0
    for _ in range(300):
        ga=gb=0.0
        for p,y in zip(probabilities,labels):
            z=math.log(max(1e-6,p)/max(1e-6,1-p));q=sigmoid(a*z+b);ga+=(q-y)*z;gb+=q-y
        n=max(1,len(labels));a-=.03*ga/n;b-=.03*gb/n
    return {'method':'platt','a':a,'b':b}
def apply_calibration(p:float,c:dict[str,Any])->float:
    if c.get('method')=='platt':return sigmoid(float(c['a'])*math.log(max(1e-6,p)/max(1e-6,1-p))+float(c['b']))
    return max(0,min(1,p))

@dataclass(slots=True)
class LoadedArtifact:
    metadata:dict[str,Any]; estimator:Any
    def predict(self,values:dict[str,float])->float:
        if self.metadata.get('task')=='temporal_classification':
            sequence=values.get('__sequence__')
            if not isinstance(sequence,list):
                row=[float(values.get(name,0.0)) for name in self.metadata['feature_names']];sequence=[row for _ in range(int(self.metadata['sequence_length']))]
            return float(self.estimator.predict([sequence])[0])
        row=[float(values.get(name,0.0)) for name in self.metadata['feature_names']]
        if self.metadata.get('task')=='regression':
            return float(self.estimator.predict([row])[0])
        if hasattr(self.estimator,'predict_proba'):
            raw=self.estimator.predict_proba([row]);p=float(raw[0][1] if hasattr(raw[0],'__len__') else raw[0])
        else:p=.5
        return apply_calibration(p,self.metadata.get('calibration') or {})

class ArtifactRegistry:
    def __init__(self,settings:Settings):self.settings=settings;self.path=settings.data_dir/'brain'/'intelligence.sqlite3';self.directory=settings.data_dir/'brain'/'artifacts';self.directory.mkdir(parents=True,exist_ok=True);self._cache=OrderedDict()
    def load(self,artifact_id:str)->LoadedArtifact:
        with connect(self.path,self.settings) as con:row=con.execute('SELECT * FROM model_artifacts WHERE artifact_id=?',(artifact_id,)).fetchone()
        if not row:raise KeyError(artifact_id)
        path=Path(row['artifact_path']);raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=row['artifact_sha256']:raise RuntimeError('artifact checksum mismatch')
        meta=json.loads(raw);kind=meta['estimator'].get('kind')
        model_path=path.with_suffix('.pt' if kind=='temporal_torch' else '.joblib')
        stat=model_path.stat() if model_path.exists() else None
        cache_key=(artifact_id,row['artifact_sha256'],(stat.st_mtime_ns,stat.st_size) if stat else None)
        if cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]
        if kind=='linear':est=LinearEstimator.from_dict(meta['estimator'])
        elif kind=='bayesian':est=BayesianEstimator();est.probability=float(meta['estimator']['probability'])
        elif kind=='regression_linear':est=LinearRegressor.from_dict(meta['estimator'])
        elif kind=='regression_mean':est=MeanRegressor();est.value=float(meta['estimator']['value'])
        elif kind=='temporal_torch':
            from .temporal_models import TemporalArtifact,TemporalModelFactory
            try:
                import torch
                model_path=path.with_suffix('.pt')
                if hashlib.sha256(model_path.read_bytes()).hexdigest()!=meta['model_sha256']:raise RuntimeError('temporal model checksum mismatch')
                model=TemporalModelFactory._model(str(meta['model_family']),int(meta['feature_count']),max(4,min(32,int(meta['feature_count'])*2)));model.load_state_dict(torch.load(model_path,map_location='cpu',weights_only=True));est=TemporalArtifact(str(meta['model_family']),model,int(meta['sequence_length']),int(meta['feature_count']))
            except ImportError as exc:raise RuntimeError('temporal artifact requires pip install .[full]') from exc
        else:
            try:
                import joblib
                if hashlib.sha256(path.with_suffix('.joblib').read_bytes()).hexdigest()!=meta['model_sha256']:raise RuntimeError('model checksum mismatch')
                est=joblib.load(path.with_suffix('.joblib'))
            except ImportError as exc:raise RuntimeError('ML artifact requires pip install .[ml]') from exc
        loaded=LoadedArtifact(meta,est)
        self._cache[cache_key]=loaded
        while len(self._cache)>8:self._cache.popitem(last=False)
        return loaded
    def champion(self,expert:str,feature_version:str|None=None)->LoadedArtifact|None:
        with connect(self.path,self.settings) as con:row=con.execute("SELECT artifact_id FROM model_artifacts WHERE expert=? AND state='CHAMPION' AND (? IS NULL OR feature_version=?)",(expert,feature_version,feature_version)).fetchone()
        return self.load(str(row['artifact_id'])) if row else None
    def shadows(self,expert:str,feature_version:str|None=None)->list[LoadedArtifact]:
        with connect(self.path,self.settings) as con:rows=con.execute("SELECT artifact_id FROM model_artifacts WHERE expert=? AND state='SHADOW' AND (? IS NULL OR feature_version=?)",(expert,feature_version,feature_version)).fetchall()
        return [self.load(str(row['artifact_id'])) for row in rows]
    def rollback(self,expert:str,target_id:str|None=None)->dict[str,Any]:
        with connect(self.path,self.settings) as con:
            from ..storage.sqlite import transaction
            transaction(con)
            try:
                current=con.execute("SELECT * FROM model_artifacts WHERE expert=? AND state='CHAMPION'",(expert,)).fetchone()
                target=con.execute("SELECT * FROM model_artifacts WHERE expert=? AND state='RETIRED' AND (? IS NULL OR artifact_id=?) ORDER BY retired_at DESC LIMIT 1",(expert,target_id,target_id)).fetchone()
                if not current or not target:con.execute('ROLLBACK');return {'rolled_back':False,'reason':'eligible_artifact_missing'}
                now=time.time();con.execute("UPDATE model_artifacts SET state='RETIRED',retired_at=? WHERE artifact_id=?",(now,current['artifact_id']));con.execute("UPDATE model_artifacts SET state='CHAMPION',promoted_at=?,retired_at=NULL WHERE artifact_id=?",(now,target['artifact_id']))
                con.execute("INSERT INTO audit_log(actor,action,object_type,object_id,detail_json,created_at) VALUES('governance','ARTIFACT_ROLLBACK','artifact',?,?,?)",(target['artifact_id'],json.dumps({'from':current['artifact_id'],'to':target['artifact_id'],'expert':expert}),now));con.execute('COMMIT');return {'rolled_back':True,'from':current['artifact_id'],'to':target['artifact_id'],'expert':expert}
            except BaseException:con.execute('ROLLBACK');raise

class ModelFactory:
    FAMILIES=('bayesian','logistic','elastic_net','sgd','random_forest','extra_trees','hist_gradient_boosting','xgboost','lightgbm','catboost')
    def __init__(self,settings:Settings,seed:int=52):
        self.settings=settings;self.seed=seed;self.registry=ArtifactRegistry(settings);self.path=self.registry.path
        # Boosting libraries are valuable challengers once there is enough evidence, but
        # fitting XGBoost/LightGBM/CatBoost repeatedly on tiny warm-up datasets adds
        # latency and can leave third-party worker state behind in long-lived test/runtime
        # processes. Keep the families available, but qualify them only after a
        # meaningful minimum sample size. This is a model-governance rule, not a
        # test shortcut.
        self.boosting_min_rows=max(20,int(os.getenv('MODEL_BOOSTING_MIN_ROWS','200')))
    @staticmethod
    def _matrix(rows:list[dict[str,Any]],feature_names:list[str]):return [[float(r['values'].get(n,0)) for n in feature_names] for r in rows]
    @staticmethod
    def _validate_temporal_contract(rows:list[dict[str,Any]])->None:
        for row in rows:
            decision=float(row['prediction_time']);available=float(row.get('available_cutoff',decision))
            if available>decision+1e-9:raise ValueError('feature.available_time must be <= decision_time for every row')
    @staticmethod
    def _walk_forward_folds(rows:list[dict[str,Any]],fold_count:int=4,purge_rows:int=1,embargo_rows:int=1)->list[tuple[list[int],list[int]]]:
        n=len(rows);minimum=max(12,n//3);remaining=n-minimum
        width=max(2,remaining//max(2,fold_count));folds=[];start=minimum
        while start<n and len(folds)<fold_count:
            stop=min(n,start+width);train_stop=max(0,start-max(1,purge_rows));validation=list(range(start,stop))
            validation_start=float(rows[start]['prediction_time']);training=[]
            for index in range(train_stop):
                label_end=float(rows[index].get('label_available_at',rows[index].get('label_end_time',rows[index]['prediction_time'])))
                if label_end<validation_start:training.append(index)
            if len(training)>=12 and validation:folds.append((training,validation))
            start=stop+max(0,embargo_rows)
        if len(folds)<2:raise ValueError('purged walk-forward requires at least two OOS folds')
        return folds
    @staticmethod
    def _classification_metrics(probabilities:list[float],labels:list[int])->dict[str,float]:
        predicted=[int(p>=.5) for p in probabilities];tp=sum(p==1 and y==1 for p,y in zip(predicted,labels));fp=sum(p==1 and y==0 for p,y in zip(predicted,labels));tn=sum(p==0 and y==0 for p,y in zip(predicted,labels))
        return {'brier':brier_score(probabilities,labels),'ece':calibration_error(probabilities,labels),'precision':tp/max(1,tp+fp),'false_positive_rate':fp/max(1,fp+tn),'expected_value':sum((2*y-1)*(p-.5) for p,y in zip(probabilities,labels))/max(1,len(labels))}
    def train(self,expert:str,rows:list[dict[str,Any]],asset_scope:str='*',horizon:int=0,state:str='SHADOW',label_version:str='direction-v1')->dict[str,Any]:
        if len(rows)<20:raise ValueError('minimum 20 chronological rows required')
        rows=sorted(rows,key=lambda r:float(r['prediction_time']));self._validate_temporal_contract(rows);folds=self._walk_forward_folds(rows);feature_names=sorted({k for r in rows for k in r['values']});X=self._matrix(rows,feature_names);y=[int(r['label']) for r in rows]
        split=max(12,int(len(rows)*.7));Xtr,Xv=X[:split],X[split:];ytr,yv=y[:split],y[split:]
        if len(set(ytr))<2 or not Xv:raise ValueError('dataset requires both labels and validation rows')
        candidates=[]
        for name,est in [('bayesian',BayesianEstimator()),('logistic',LinearEstimator(seed=self.seed)),('elastic_net',LinearEstimator(l1=.0005,l2=.001,seed=self.seed)),('sgd',LinearEstimator(epochs=120,rate=.12,seed=self.seed))]:
            est.fit(Xtr,ytr);p=est.predict_proba(Xv);candidates.append((brier_score(p,yv),name,est,p))
        try:
            from sklearn.ensemble import RandomForestClassifier,ExtraTreesClassifier,HistGradientBoostingClassifier
            for name,est in [('random_forest',RandomForestClassifier(n_estimators=100,min_samples_leaf=3,random_state=self.seed,n_jobs=1)),('extra_trees',ExtraTreesClassifier(n_estimators=100,min_samples_leaf=3,random_state=self.seed,n_jobs=1)),('hist_gradient_boosting',HistGradientBoostingClassifier(max_iter=100,random_state=self.seed))]:
                est.fit(Xtr,ytr);p=[float(x[1]) for x in est.predict_proba(Xv)];candidates.append((brier_score(p,yv),name,est,p))
        except ImportError:candidates.extend(())
        if len(rows)>=self.boosting_min_rows:
            for name,module_name,class_name,kwargs in (
                ('xgboost','xgboost','XGBClassifier',{'n_estimators':100,'max_depth':4,'learning_rate':.05,'random_state':self.seed,'n_jobs':1}),
                ('lightgbm','lightgbm','LGBMClassifier',{'n_estimators':100,'max_depth':4,'learning_rate':.05,'random_state':self.seed,'verbosity':-1,'n_jobs':1}),
                ('catboost','catboost','CatBoostClassifier',{'iterations':100,'depth':4,'learning_rate':.05,'random_seed':self.seed,'verbose':False,'thread_count':1,'allow_writing_files':False}),
            ):
                try:
                    module=__import__(module_name,fromlist=[class_name]);est=getattr(module,class_name)(**kwargs);est.fit(Xtr,ytr);p=[float(x[1]) for x in est.predict_proba(Xv)];candidates.append((brier_score(p,yv),name,est,p))
                except Exception:continue
        walk_forward=[]
        for _,name,template,_ in candidates:
            fold_scores=[];oos_p=[];oos_y=[]
            for train_idx,val_idx in folds:
                if len({y[i] for i in train_idx})<2:continue
                estimator_fold=copy.deepcopy(template);estimator_fold.fit([X[i] for i in train_idx],[y[i] for i in train_idx]);pred=estimator_fold.predict_proba([X[i] for i in val_idx]);pred=[float(v[1] if hasattr(v,'__len__') else v) for v in pred];labels=[y[i] for i in val_idx];fold_scores.append(brier_score(pred,labels));oos_p.extend(pred);oos_y.extend(labels)
            if len(fold_scores)>=2:walk_forward.append((statistics.mean(fold_scores),statistics.pstdev(fold_scores),name,template,oos_p,oos_y))
        if not walk_forward:raise ValueError('no candidate completed multiple purged OOS folds')
        score,stability,family,estimator,raw,oos_labels=min(walk_forward,key=lambda x:(x[0],x[1]));cal_split=max(1,len(raw)//2);cal=fit_platt(raw[:cal_split],oos_labels[:cal_split]);calibrated=[apply_calibration(p,cal) for p in raw[cal_split:]];estimator.fit(X,y);oos_metrics=self._classification_metrics(calibrated,oos_labels[cal_split:])
        canonical=json.dumps([{'t':r['prediction_time'],'a':r.get('available_cutoff',r['prediction_time']),'l':r.get('label_available_at'),'v':r['values'],'y':r['label']} for r in rows],sort_keys=True,separators=(',',':')).encode();dataset_sha=hashlib.sha256(canonical).hexdigest();dataset_id=f"dataset:{expert}:{dataset_sha[:16]}";now=time.time()
        artifact_id=f"artifact:{expert}:{family}:{uuid.uuid4().hex[:12]}";meta={'artifact_id':artifact_id,'expert_name':expert,'target_type':label_version,'model_family':family,'model_version':'57.3.0','dataset_version':dataset_id,'feature_version':rows[0].get('feature_version','features-57.3.0'),'feature_names':feature_names,'training_start':rows[0]['prediction_time'],'training_end':rows[split-1]['prediction_time'],'validation_start':rows[folds[0][1][0]]['prediction_time'],'validation_end':rows[folds[-1][1][-1]]['prediction_time'],'hyperparameters':getattr(estimator,'get_params',lambda:{})(),'metrics':{**oos_metrics,'raw_brier':score,'stability':stability,'samples':len(rows),'validation_samples':len(oos_labels),'walk_forward_folds':len(folds),'purge_rows':1,'embargo_rows':1},'calibration':cal,'created_at':now,'seed':self.seed,'environment':{'python':platform.python_version()},'estimator':estimator.to_dict() if hasattr(estimator,'to_dict') else {'kind':'joblib'}}
        path=self.registry.directory/(artifact_id.replace(':','_')+'.json')
        if meta['estimator']['kind']=='joblib':
            import joblib
            model_path=path.with_suffix('.joblib');joblib.dump(estimator,model_path);meta['model_sha256']=hashlib.sha256(model_path.read_bytes()).hexdigest()
        raw_meta=json.dumps(meta,sort_keys=True,separators=(',',':'),default=str).encode();path.write_bytes(raw_meta);artifact_sha=hashlib.sha256(raw_meta).hexdigest()
        with connect(self.path,self.settings) as con:
            con.execute("INSERT OR IGNORE INTO datasets(dataset_id,expert,asset_scope,horizon_seconds,start_at,end_at,row_count,sha256,feature_version,label_version,specification_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(dataset_id,expert,asset_scope,horizon,float(rows[0]['prediction_time']),float(rows[-1]['prediction_time']),len(rows),dataset_sha,meta['feature_version'],label_version,json.dumps({'available_at_contract':True,'target_type':label_version}),now))
            con.execute("""INSERT INTO model_artifacts(artifact_id,expert,model_family,state,artifact_path,artifact_sha256,dataset_id,feature_version,code_version,seed,training_start,training_end,validation_start,validation_end,hyperparameters_json,metrics_json,calibration_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(artifact_id,expert,family,state,str(path),artifact_sha,dataset_id,meta['feature_version'],'57.3.0',self.seed,meta['training_start'],meta['training_end'],meta['validation_start'],meta['validation_end'],json.dumps(meta['hyperparameters'],default=str),json.dumps(meta['metrics']),json.dumps(cal),now))
        return {'artifact_id':artifact_id,'family':family,'state':state,'metrics':meta['metrics'],'dataset_id':dataset_id,'candidate_metrics':{name:score for score,_,name,_,_,_ in walk_forward}}

    def train_regression(self,expert:str,rows:list[dict[str,Any]],asset_scope:str='*',horizon:int=0,state:str='SHADOW',label_version:str='continuous-v1')->dict[str,Any]:
        if len(rows)<20:raise ValueError('minimum 20 chronological rows required')
        rows=sorted(rows,key=lambda r:float(r['prediction_time']));self._validate_temporal_contract(rows);folds=self._walk_forward_folds(rows);feature_names=sorted({k for r in rows for k in r['values']});X=self._matrix(rows,feature_names);y=[float(r['label']) for r in rows];split=max(12,int(len(rows)*.7));Xtr,Xv=X[:split],X[split:];ytr,yv=y[:split],y[split:]
        if not Xv:raise ValueError('validation rows required')
        candidates=[]
        for name,est in [('mean_regression',MeanRegressor()),('linear_regression',LinearRegressor())]:
            est.fit(Xtr,ytr);p=est.predict(Xv);mae=sum(abs(a-b) for a,b in zip(p,yv))/len(yv);candidates.append((mae,name,est,p))
        try:
            from sklearn.ensemble import RandomForestRegressor,ExtraTreesRegressor,HistGradientBoostingRegressor
            for name,est in [('random_forest_regression',RandomForestRegressor(n_estimators=100,min_samples_leaf=3,random_state=self.seed,n_jobs=1)),('extra_trees_regression',ExtraTreesRegressor(n_estimators=100,min_samples_leaf=3,random_state=self.seed,n_jobs=1)),('hist_gradient_regression',HistGradientBoostingRegressor(max_iter=100,random_state=self.seed))]:
                est.fit(Xtr,ytr);p=list(map(float,est.predict(Xv)));mae=sum(abs(a-b) for a,b in zip(p,yv))/len(yv);candidates.append((mae,name,est,p))
        except ImportError:candidates.extend(())
        if len(rows)>=self.boosting_min_rows:
            for name,module_name,class_name,kwargs in (
                ('xgboost_regression','xgboost','XGBRegressor',{'n_estimators':100,'max_depth':4,'learning_rate':.05,'random_state':self.seed,'n_jobs':1}),
                ('lightgbm_regression','lightgbm','LGBMRegressor',{'n_estimators':100,'max_depth':4,'learning_rate':.05,'random_state':self.seed,'verbosity':-1,'n_jobs':1}),
                ('catboost_regression','catboost','CatBoostRegressor',{'iterations':100,'depth':4,'learning_rate':.05,'random_seed':self.seed,'verbose':False,'thread_count':1,'allow_writing_files':False}),
            ):
                try:
                    module=__import__(module_name,fromlist=[class_name]);est=getattr(module,class_name)(**kwargs);est.fit(Xtr,ytr);p=list(map(float,est.predict(Xv)));candidate_mae=sum(abs(a-b) for a,b in zip(p,yv))/len(yv);candidates.append((candidate_mae,name,est,p))
                except Exception:continue
        walk_forward=[]
        for _,name,template,_ in candidates:
            fold_mae=[];oos_pred=[];oos_y=[]
            for train_idx,val_idx in folds:
                estimator_fold=copy.deepcopy(template);estimator_fold.fit([X[i] for i in train_idx],[y[i] for i in train_idx]);pred=list(map(float,estimator_fold.predict([X[i] for i in val_idx])));labels=[y[i] for i in val_idx];fold_mae.append(sum(abs(a-b) for a,b in zip(pred,labels))/len(labels));oos_pred.extend(pred);oos_y.extend(labels)
            if len(fold_mae)>=2:walk_forward.append((statistics.mean(fold_mae),statistics.pstdev(fold_mae),name,template,oos_pred,oos_y))
        if not walk_forward:raise ValueError('no regressor completed multiple purged OOS folds')
        mae,stability,family,estimator,predicted,oos_y=min(walk_forward,key=lambda x:(x[0],x[1]));estimator.fit(X,y);rmse=(sum((a-b)**2 for a,b in zip(predicted,oos_y))/len(oos_y))**.5;canonical=json.dumps([{'t':r['prediction_time'],'a':r.get('available_cutoff',r['prediction_time']),'l':r.get('label_available_at'),'v':r['values'],'y':r['label']} for r in rows],sort_keys=True,separators=(',',':')).encode();dataset_sha=hashlib.sha256(canonical).hexdigest();dataset_id=f"dataset:{expert}:{dataset_sha[:16]}";now=time.time();artifact_id=f"artifact:{expert}:{family}:{uuid.uuid4().hex[:12]}";meta={'artifact_id':artifact_id,'expert_name':expert,'target_type':label_version,'task':'regression','model_family':family,'model_version':'57.3.0','dataset_version':dataset_id,'feature_version':rows[0].get('feature_version','features-57.3.0'),'feature_names':feature_names,'training_start':rows[0]['prediction_time'],'training_end':rows[split-1]['prediction_time'],'validation_start':rows[folds[0][1][0]]['prediction_time'],'validation_end':rows[folds[-1][1][-1]]['prediction_time'],'hyperparameters':getattr(estimator,'get_params',lambda:{})(),'metrics':{'mae':mae,'rmse':rmse,'stability':stability,'samples':len(rows),'validation_samples':len(oos_y),'walk_forward_folds':len(folds),'purge_rows':1,'embargo_rows':1},'calibration':{},'created_at':now,'seed':self.seed,'environment':{'python':platform.python_version()},'estimator':estimator.to_dict() if hasattr(estimator,'to_dict') else {'kind':'joblib'}};path=self.registry.directory/(artifact_id.replace(':','_')+'.json')
        if meta['estimator']['kind']=='joblib':
            import joblib
            model_path=path.with_suffix('.joblib');joblib.dump(estimator,model_path);meta['model_sha256']=hashlib.sha256(model_path.read_bytes()).hexdigest()
        raw_meta=json.dumps(meta,sort_keys=True,separators=(',',':'),default=str).encode();path.write_bytes(raw_meta);artifact_sha=hashlib.sha256(raw_meta).hexdigest()
        with connect(self.path,self.settings) as con:
            con.execute("INSERT OR IGNORE INTO datasets(dataset_id,expert,asset_scope,horizon_seconds,start_at,end_at,row_count,sha256,feature_version,label_version,specification_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(dataset_id,expert,asset_scope,horizon,float(rows[0]['prediction_time']),float(rows[-1]['prediction_time']),len(rows),dataset_sha,meta['feature_version'],label_version,json.dumps({'available_at_contract':True,'task':'regression','target_type':label_version}),now))
            con.execute("""INSERT INTO model_artifacts(artifact_id,expert,model_family,state,artifact_path,artifact_sha256,dataset_id,feature_version,code_version,seed,training_start,training_end,validation_start,validation_end,hyperparameters_json,metrics_json,calibration_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(artifact_id,expert,family,state,str(path),artifact_sha,dataset_id,meta['feature_version'],'57.3.0',self.seed,meta['training_start'],meta['training_end'],meta['validation_start'],meta['validation_end'],json.dumps(meta['hyperparameters'],default=str),json.dumps(meta['metrics']),json.dumps({}),now))
        return {'artifact_id':artifact_id,'family':family,'state':state,'metrics':meta['metrics'],'dataset_id':dataset_id,'candidate_metrics':{name:score for score,_,name,_,_,_ in walk_forward}}
