"""Evidence-triggered, non-spammy adaptive-learning reports."""
from __future__ import annotations

import json
import os
import time
from typing import Any

from ..config import Settings
from ..storage.control import ControlStore
from ..storage.sqlite import connect
from .openai_semantics import OpenAISemanticExtractor
from .store import BrainStore
from .telegram_producers import TelegramIntelligenceProducers


class LearningInsightEngine:
    def __init__(self,settings:Settings):
        self.settings=settings;self.brain=BrainStore(settings);self.path=self.brain.path
        # The HTTP connector must be created inside a running event loop.  The
        # response service is also instantiated by synchronous diagnostics and
        # fixtures, so delay connector creation until a report is actually due.
        self.semantic:OpenAISemanticExtractor|None=None;self.telegram=TelegramIntelligenceProducers(ControlStore(settings))
        self.minimum=max(20,int(os.getenv('LEARNING_REPORT_MIN_NEW_UPDATES','100')))
        self.interval=max(900,float(os.getenv('LEARNING_REPORT_MIN_INTERVAL_SECONDS','21600')))

    async def close(self):
        if self.semantic is not None:await self.semantic.close()

    def _candidate(self)->dict[str,Any]|None:
        now=time.time()
        with connect(self.path,self.settings) as con:
            meta={r['key']:r['value'] for r in con.execute("SELECT key,value FROM metadata WHERE key IN ('learning_report_version','learning_report_at')").fetchall()}
            last_version=int(float(meta.get('learning_report_version','0')));last_at=float(meta.get('learning_report_at','0'))
            state=con.execute('SELECT state_json FROM neural_bandit_state WHERE id=1').fetchone()
            if not state:return None
            model=json.loads(state['state_json']);version=int(model.get('version',0));new=version-last_version
            if new<self.minimum or now-last_at<self.interval:return None
            rows=[dict(r) for r in con.execute('''SELECT e.event_type,p.asset_id,sb.level,COUNT(*) samples,
                AVG(sb.reward_r) mean_reward_r,SUM(sb.state='WIN') wins,SUM(sb.state='LOSS') losses,
                SUM(sb.state='TIMEOUT') timeouts,AVG(json_extract(sb.detail_json,'$.mfe')) mean_mfe,
                AVG(json_extract(sb.detail_json,'$.mae')) mean_mae
                FROM neural_bandit_updates nu JOIN signal_barriers sb ON sb.signal_id=nu.signal_id AND sb.level=nu.level
                JOIN signals s ON s.id=nu.signal_id JOIN predictions p ON p.id=s.prediction_id JOIN events e ON e.id=p.event_id
                WHERE nu.version>? GROUP BY e.event_type,p.asset_id,sb.level HAVING COUNT(*)>=3
                ORDER BY samples DESC,mean_reward_r DESC LIMIT 12''',(last_version,)).fetchall()]
            if not rows:return None
        return {'neural_version_before':last_version,'neural_version_after':version,'new_delayed_updates':new,
                'architecture':model.get('architecture',{}),'patterns':rows,'generated_at':now,
                'interpretation_limit':'paper 1:1 barrier outcomes; shadow evidence; not guaranteed profit'}

    def _commit(self,evidence:dict[str,Any]):
        now=time.time()
        with connect(self.path,self.settings) as con:
            for key,value in (('learning_report_version',evidence['neural_version_after']),('learning_report_at',now)):
                con.execute("INSERT INTO metadata(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,str(value),now))

    @staticmethod
    def _fallback(evidence:dict[str,Any])->str:
        best=max(evidence['patterns'],key=lambda row:float(row.get('mean_reward_r') or -999))
        return (f"شبکه از {evidence['new_delayed_updates']} بازخورد کندلی تازه یاد گرفت و نسخه آن از "
                f"{evidence['neural_version_before']} به {evidence['neural_version_after']} رسید. قوی‌ترین الگوی فعلی "
                f"{best['event_type']} برای {best['asset_id']} در آستانه {float(best['level'])*100:g}% است؛ "
                f"میانگین بازده {float(best['mean_reward_r']):.3f}R روی {best['samples']} نمونه ثبت شد "
                f"(برد {best['wins']}، باخت {best['losses']}، پایان زمان {best['timeouts']}). "
                "این نتیجه هنوز در حالت shadow است و تا اعتبارسنجی خارج از نمونه مجوز سیگنال زنده نیست.")

    async def maybe_report(self)->dict[str,Any]:
        evidence=self._candidate()
        if not evidence:return {'reported':False}
        if self.semantic is None:self.semantic=OpenAISemanticExtractor(self.settings)
        try:narrative=await self.semantic.narrate_learning(evidence)
        except Exception:narrative=None
        narrative=narrative or self._fallback(evidence)
        produced=await self.telegram.produce({'learning_report_ready':True,'event_id':f"learning:{evidence['neural_version_after']}",
            'asset_id':'MULTI_ASSET','summary':narrative,'learning_evidence':evidence,'confidence':None})
        if produced:self._commit(evidence)
        return {'reported':bool(produced),'version':evidence['neural_version_after'],'new_updates':evidence['new_delayed_updates']}
