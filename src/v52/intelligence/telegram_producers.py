from __future__ import annotations

import json,os,time
from typing import Any

from ..storage.control import ControlStore
from ..storage.sqlite import connect
from ..alerts.telegram import render_batch


FINAL_INTELLIGENCE_KINDS=(
    'intelligence_signal','provisional_signal','active_update','signal_exit','invalidation','pump_warning',
    'dump_warning','fundamental_intelligence','validated_learning_update',
    'daily_learning_report','critical_system_warning',
)
ADAPTIVE_INTELLIGENCE_KINDS=('adaptive_learning_report',)


class TelegramIntelligenceProducers:
    """Typed final-intelligence producers; raw news/market payloads are rejected."""

    def __init__(self,control:ControlStore):self.control=control
    async def emit(self,kind:str,key:str,payload:dict[str,Any],priority:int=7,ttl_seconds:int=1800)->bool:
        if kind not in (*FINAL_INTELLIGENCE_KINDS,*ADAPTIVE_INTELLIGENCE_KINDS):raise ValueError(f'non-intelligence Telegram producer rejected: {kind}')
        if payload.get('raw_news') is not None or payload.get('raw_market_data') is not None:raise ValueError('raw data is brain input, never Telegram output')
        return await self.control.enqueue_alert(key,kind,max(1,min(10,int(priority))),{'kind':kind,**payload},time.time()+max(60,int(ttl_seconds)))

    def _signal_limit_allows(self,payload:dict[str,Any])->bool:
        quality=float(payload.get('quality_score') or 0.0);minimum=float(os.getenv('TELEGRAM_PROVISIONAL_MIN_QUALITY','.38') if payload.get('provisional_gate') else os.getenv('TELEGRAM_MINIMUM_SIGNAL_QUALITY','.65'))
        if quality<minimum:return False
        event_id=str(payload.get('event_id') or '');per_event=max(1,int(os.getenv('TELEGRAM_MAX_SIGNALS_PER_EVENT','2')))
        per_hour=max(1,int(os.getenv('TELEGRAM_MAX_PROVISIONAL_SIGNALS_PER_HOUR','4') if payload.get('provisional_gate') else os.getenv('TELEGRAM_MAX_SIGNALS_PER_HOUR','12')))
        with connect(self.control.path,self.control.settings) as con:
            rows=con.execute("SELECT payload_json,created_at FROM alerts WHERE kind IN ('intelligence_signal','provisional_signal') AND created_at>?",(time.time()-3600,)).fetchall()
        if len(rows)>=per_hour:return False
        same=0
        for row in rows:
            try:same+=str(json.loads(row['payload_json']).get('event_id') or '')==event_id
            except (TypeError,ValueError):continue
        return same<per_event

    @staticmethod
    def conditions(snapshot:dict[str,Any])->list[tuple[str,str,int]]:
        result=[]
        live_gate=bool(snapshot.get('live_gate'))
        if live_gate and snapshot.get('action') not in {None,'NO_TRADE','WAIT'} and float(snapshot.get('quality_score') or 0)>=.65:result.append(('intelligence_signal','FINAL_SIGNAL',10))
        if snapshot.get('provisional_gate') and snapshot.get('action') in {'WATCH_BUY','WATCH_SELL'} and float(snapshot.get('quality_score') or 0)>=float(os.getenv('TELEGRAM_PROVISIONAL_MIN_QUALITY','.38')):result.append(('provisional_signal','POST_48H_PROVISIONAL_SIGNAL',8))
        if live_gate and snapshot.get('state_changed') and snapshot.get('signal_state') in {'ACTIVE','UPDATED','PARTIAL_EXIT'}:result.append(('active_update','ACTIVE_UPDATE',8))
        if live_gate and snapshot.get('signal_state')=='EXITED':result.append(('signal_exit','EXIT',10))
        if live_gate and snapshot.get('signal_state')=='INVALIDATED':result.append(('invalidation','INVALIDATION',10))
        if live_gate and float(snapshot.get('pump_probability') or 0)>=.75:result.append(('pump_warning','PUMP_WARNING',9))
        if live_gate and float(snapshot.get('dump_probability') or 0)>=.75:result.append(('dump_warning','DUMP_WARNING',9))
        if float(snapshot.get('materiality') or 0)>=8 and snapshot.get('fundamental_only'):result.append(('fundamental_intelligence','FUNDAMENTAL_INTELLIGENCE',7))
        if snapshot.get('knowledge_promoted') and snapshot.get('oos_validated'):result.append(('validated_learning_update','VALIDATED_LEARNING',6))
        if snapshot.get('daily_report_ready'):result.append(('daily_learning_report','DAILY_REPORT',4))
        if snapshot.get('learning_report_ready'):result.append(('adaptive_learning_report','ADAPTIVE_LEARNING',6))
        if snapshot.get('critical_health_failure'):result.append(('critical_system_warning','CRITICAL_WARNING',10))
        return result

    async def produce(self,snapshot:dict[str,Any])->list[dict[str,Any]]:
        produced=[]
        for kind,condition,priority in self.conditions(snapshot):
            if kind in {'intelligence_signal','provisional_signal'} and not self._signal_limit_allows(snapshot):continue
            identity=snapshot.get('signal_id') or snapshot.get('event_id') or snapshot.get('report_date') or int(time.time())
            if kind=='active_update':identity=f"{identity}:{snapshot.get('signal_state')}:{snapshot.get('action') or snapshot.get('summary')}"
            payload={**snapshot,'producer_condition':condition}
            inserted=await self.emit(kind,f'producer:{kind}:{identity}',payload,priority)
            if inserted:
                rendered=render_batch([({}, {'kind':kind,**payload})])
                produced.append({'kind':kind,'condition':condition,'queued':True,'rendered':rendered})
        return produced
