from __future__ import annotations
import json,math,time
from typing import Any
from ..config import Settings
from ..storage.sqlite import connect
from ..timeutil import parse_datetime

class HistoricalAnalogueEngine:
    def __init__(self,settings:Settings):self.settings=settings;self.path=settings.data_dir/'brain'/'intelligence.sqlite3'
    def find(self,event:Any,limit:int=10,cutoff:float|None=None)->list[dict[str,Any]]:
        asset_ids={str(x['asset_id']) for x in event.assets}
        if cutoff is None:
            dt=parse_datetime(getattr(event,'observed_at',None)) or parse_datetime(getattr(event,'occurred_at',None))
            cutoff=dt.timestamp() if dt else time.time()
        with connect(self.path,self.settings) as con:
            rows=con.execute('SELECT * FROM events WHERE event_type=? AND created_at<? ORDER BY id DESC LIMIT 500',(event.event_type,float(cutoff))).fetchall();out=[]
            for row in rows:
                assets={str(x['asset_id']) for x in con.execute('SELECT asset_id FROM event_assets WHERE event_id=?',(row['id'],)).fetchall()};union=assets|asset_ids
                asset_similarity=len(assets&asset_ids)/len(union) if union else 0;surprise_similarity=1-min(1,abs(float(row['surprise'] or 0)-float(event.surprise or 0)))
                score=.45+.25*asset_similarity+.12*(str(row['domain'])==event.domain)+.10*surprise_similarity+.08*(str(row['subtype'])==event.subtype)
                out.append({'event_id':int(row['id']),'score':score,'event_type':row['event_type'],'assets':sorted(assets),'created_at':float(row['created_at'])})
        return sorted(out,key=lambda x:x['score'],reverse=True)[:limit]
