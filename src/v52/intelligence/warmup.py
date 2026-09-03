from __future__ import annotations
import asyncio,time
from ..config import Settings
from ..storage.sqlite import connect,transaction

OBSERVATION_REQUIRED=86400.0
SHADOW_REQUIRED=86400.0

class RuntimeWarmup:
    """Counts only bounded intervals observed while the process is actually ticking."""
    def __init__(self,settings:Settings,max_tick_gap:float=30.0):self.settings=settings;self.path=settings.data_dir/'brain'/'intelligence.sqlite3';self.max_tick_gap=max_tick_gap
    def tick_sync(self,now:float|None=None,replay:bool=False)->dict[str,float]:
        now=float(now or time.time())
        with connect(self.path,self.settings) as con:
            transaction(con)
            try:
                def get(key,default='0'):row=con.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone();return float(row['value'] if row else default)
                observation=get('observation_runtime_seconds');shadow=get('shadow_runtime_seconds');last=get('last_runtime_tick')
                delta=0.0 if replay or last<=0 else max(0.0,min(self.max_tick_gap,now-last))
                if observation<OBSERVATION_REQUIRED:observation=min(OBSERVATION_REQUIRED,observation+delta)
                else:shadow=min(SHADOW_REQUIRED,shadow+delta)
                for key,value in (('observation_runtime_seconds',observation),('shadow_runtime_seconds',shadow),('last_runtime_tick',now)):
                    con.execute("INSERT INTO metadata(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,str(value),now))
                con.execute('COMMIT');return {'observation_runtime_seconds':observation,'shadow_runtime_seconds':shadow,'delta':delta}
            except BaseException:con.execute('ROLLBACK');raise
    async def tick(self,now:float|None=None,replay:bool=False):return await asyncio.to_thread(self.tick_sync,now,replay)
    def status_sync(self)->dict[str,float|str]:
        with connect(self.path,self.settings) as con:
            values={r['key']:r['value'] for r in con.execute("SELECT key,value FROM metadata WHERE key IN ('observation_runtime_seconds','shadow_runtime_seconds','signals_enabled')").fetchall()}
        observation=float(values.get('observation_runtime_seconds','0'));shadow=float(values.get('shadow_runtime_seconds','0'))
        mode='OBSERVATION' if observation<OBSERVATION_REQUIRED else 'SHADOW' if shadow<SHADOW_REQUIRED else 'LIVE' if values.get('signals_enabled')=='1' else 'CONSERVATIVE'
        return {'mode':mode,'observation_runtime_seconds':observation,'shadow_runtime_seconds':shadow,'observation_remaining':max(0,OBSERVATION_REQUIRED-observation),'shadow_remaining':max(0,SHADOW_REQUIRED-shadow)}
    async def status(self):return await asyncio.to_thread(self.status_sync)
