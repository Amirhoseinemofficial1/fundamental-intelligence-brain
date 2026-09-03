from __future__ import annotations
import json,time
from typing import Any
from ..config import Settings
from ..storage.sqlite import connect,transaction

STATES=('CREATED','QUALIFIED','SENT','ENTRY_PENDING','ACTIVE','UPDATED','PARTIAL_EXIT','EXIT_PENDING','EXITED','POST_EXIT','EVALUATED','LEARNED','INVALIDATED')
ALLOWED={
 'CREATED':{'QUALIFIED','INVALIDATED'},'QUALIFIED':{'SENT','INVALIDATED'},'SENT':{'ENTRY_PENDING','INVALIDATED'},
 'ENTRY_PENDING':{'ACTIVE','INVALIDATED'},'ACTIVE':{'UPDATED','EXIT_PENDING','INVALIDATED'},'UPDATED':{'UPDATED','PARTIAL_EXIT','EXIT_PENDING','INVALIDATED'},
 'PARTIAL_EXIT':{'ACTIVE','UPDATED','EXIT_PENDING','INVALIDATED'},
 'EXIT_PENDING':{'EXITED','INVALIDATED'},'EXITED':{'POST_EXIT'},'POST_EXIT':{'EVALUATED'},'EVALUATED':{'LEARNED'},'LEARNED':set(),'INVALIDATED':set(),
}
class SignalStateMachine:
    def __init__(self,settings:Settings):self.settings=settings;self.path=settings.data_dir/'brain'/'intelligence.sqlite3'
    def transition_sync(self,signal_id:int,target:str,reason:str,context:dict[str,Any]|None=None)->str:
        if target not in STATES:raise ValueError(f'unknown signal state {target}')
        with connect(self.path,self.settings) as con:
            transaction(con)
            try:
                row=con.execute('SELECT state FROM signals WHERE id=?',(signal_id,)).fetchone()
                if not row:raise KeyError(signal_id)
                current=str(row['state'])
                if target==current:con.execute('COMMIT');return current
                if target not in ALLOWED.get(current,set()):raise ValueError(f'invalid signal transition {current}->{target}')
                effective=(context or {}).get('effective_at');now=float(effective) if effective is not None and float(effective)>0 else time.time();closed=now if target in {'EXITED','INVALIDATED'} else None
                con.execute('UPDATE signals SET state=?,closed_at=COALESCE(?,closed_at),close_reason=CASE WHEN ? IS NULL THEN close_reason ELSE ? END,last_updated_at=? WHERE id=?',(target,closed,closed,reason,now,signal_id))
                con.execute('INSERT INTO signal_lifecycle(signal_id,state,reason,market_context_json,created_at) VALUES(?,?,?,?,?)',(signal_id,target,reason,json.dumps(context or {},default=str),now));con.execute('COMMIT');return target
            except BaseException:con.execute('ROLLBACK');raise
    async def transition(self,*args,**kwargs):
        import asyncio
        return await asyncio.to_thread(self.transition_sync,*args,**kwargs)
