from __future__ import annotations
import asyncio,csv,json,time
from pathlib import Path
from typing import Any,Awaitable,Callable
from ..config import Settings
from ..storage.sqlite import connect
from .observation import CanonicalObservation,ObservationBridge

class HistoricalImporter:
    def __init__(self,settings:Settings):self.settings=settings;self.bridge=ObservationBridge(settings);self.path=self.bridge.path
    def import_prices_csv(self,path:Path,provider:str='historical_csv')->int:
        inserted=0
        with path.open(encoding='utf-8',newline='') as fh:
            for index,row in enumerate(csv.DictReader(fh),1):
                event=float(row['event_time']);available=float(row['available_time']);ingestion=float(row.get('ingestion_time') or available)
                if available<event or ingestion<available:raise ValueError(f'timestamp contract violation row={index}')
                asset=row['asset_id'];price=float(row['price']);volume=float(row['volume']) if row.get('volume') else None
                with connect(self.path,self.settings) as con:
                    cur=con.execute('INSERT OR IGNORE INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES(?,?,?,?,?,?,?,?)',(provider,asset,event,available,price,volume,float(row.get('quality') or .8),json.dumps({'import_file':path.name,'row':index})));inserted+=int(cur.rowcount>0)
        return inserted
    def import_observations_jsonl(self,path:Path)->int:
        inserted=0
        for index,line in enumerate(path.read_text(encoding='utf-8').splitlines(),1):
            if not line.strip():continue
            obj=json.loads(line);event=float(obj['event_time']);available=float(obj['available_time']);ingestion=float(obj.get('ingestion_time') or available)
            if not event<=available<=ingestion:raise ValueError(f'timestamp contract violation row={index}')
            obs=CanonicalObservation(str(obj['observation_id']),event,available,ingestion,str(obj['source_id']),float(obj.get('source_quality',.7)),tuple(obj.get('asset_ids') or ()),tuple(obj.get('entity_ids') or ()),str(obj['observation_type']),float(obj['value']) if obj.get('value') is not None else None,str(obj.get('unit') or ''),str(obj.get('direction') or 'NEUTRAL'),float(obj.get('confidence',.7)),max(0,ingestion-event),float(obj.get('quality',.7)),f'import:{path.name}:{index}',obj.get('metadata') or {},True)
            inserted+=int(self.bridge.persist_sync(obs))
        return inserted

class ReplayEngine:
    def __init__(self,settings:Settings):self.settings=settings;self.path=settings.data_dir/'brain'/'intelligence.sqlite3'
    async def replay(self,consumer:Callable[[dict[str,Any]],Awaitable[Any]],speed:float=0,limit:int=100000)->int:
        with connect(self.path,self.settings) as con:rows=[dict(r) for r in con.execute('SELECT * FROM canonical_observations ORDER BY available_time,observation_id LIMIT ?',(limit,)).fetchall()]
        previous=None
        for row in rows:
            if speed>0 and previous is not None:await asyncio.sleep(min(2,(row['available_time']-previous)/speed))
            row['_replay']=True;await consumer(row);previous=row['available_time']
        return len(rows)

class WalkForwardEngine:
    """Chronological rolling/expanding splits with purge and embargo boundaries."""
    @staticmethod
    def splits(rows:list[dict[str,Any]],folds:int=4,purge_seconds:float=0,embargo_rows:int=1,mode:str='expanding',rolling_rows:int|None=None)->list[tuple[list[int],list[int]]]:
        if mode not in {'expanding','rolling'}:raise ValueError("mode must be expanding or rolling")
        if len(rows)<max(20,folds*4):return []
        ordered=sorted(range(len(rows)),key=lambda i:(float(rows[i]['prediction_time']),i));block=max(1,len(rows)//(folds+1));result=[]
        for fold in range(1,folds+1):
            start=fold*block;end=len(rows) if fold==folds else min(len(rows),(fold+1)*block)
            validation=ordered[start:end]
            if not validation:continue
            boundary=float(rows[validation[0]]['prediction_time'])-max(0.0,purge_seconds)
            training=[i for i in ordered[:max(0,start-max(0,embargo_rows))] if float(rows[i]['prediction_time'])<boundary]
            if mode=='rolling' and training:training=training[-max(1,int(rolling_rows or block*2)):]
            if training:result.append((training,validation))
        return result

class BacktestEngine:
    def __init__(self,settings:Settings):self.settings=settings;self.path=settings.data_dir/'brain'/'intelligence.sqlite3'
    def evaluate_artifact(self,artifact,spread_bps:float=1.0,fee_bps:float=1.0,slippage_bps:float=1.0,delay_bps:float=.5,liquidity_penalty_bps:float=.5)->dict[str,Any]:
        rows=[]
        with connect(self.path,self.settings) as con:
            data=con.execute("""SELECT fv.*,mr.return_value,mr.measured_at FROM feature_vectors fv JOIN market_responses mr ON mr.event_id=fv.event_id AND mr.asset_id=fv.asset_id AND mr.horizon_seconds=fv.horizon_seconds WHERE fv.available_cutoff<=fv.prediction_time AND fv.prediction_time<mr.measured_at ORDER BY fv.prediction_time""").fetchall()
        gross_equity=1.0;equity=1.0;peak=1.0;drawdown=0.0;probabilities=[];labels=[];cost_rate=max(0.0,spread_bps+fee_bps+slippage_bps+delay_bps+liquidity_penalty_bps)/10000.0
        for row in data:
            p=artifact.predict(json.loads(row['values_json']));label=int(row['return_value']>0);probabilities.append(p);labels.append(label);gross=float(row['return_value'])*(1 if p>=.5 else -1);net=gross-cost_rate;gross_equity*=1+gross;equity*=1+net;peak=max(peak,equity);drawdown=min(drawdown,equity/peak-1)
        from .experts import brier_score,calibration_error
        return {'samples':len(data),'brier':brier_score(probabilities,labels),'ece':calibration_error(probabilities,labels),'gross_expected_value':gross_equity-1,'expected_value':equity-1,'net_expected_value':equity-1,'total_cost_bps_per_trade':cost_rate*10000,'cost_model':{'spread_bps':spread_bps,'fee_bps':fee_bps,'slippage_bps':slippage_bps,'delay_bps':delay_bps,'liquidity_penalty_bps':liquidity_penalty_bps},'max_drawdown':drawdown,'leakage_safe':all(float(r['available_cutoff'])<=float(r['prediction_time'])<float(r['measured_at']) for r in data)}
