from __future__ import annotations

import asyncio
import time
from v52.models import StoryCandidate
from v52.storage.control import ControlStore


def candidate(fp='abc'):
    return StoryCandidate(fp,'Federal Reserve cuts rates','Official policy statement','https://example.test/a','MONETARY_POLICY','US','fr','2026-08-24T00:00:00Z','2026-08-24T00:00:01Z',10,['official'],'Fed','https://fed.test',1)


def test_story_jobs_alert_created_once(settings) -> None:
    async def run():
        st=ControlStore(settings)
        args=(candidate(),'https://example.test/a',True,{'kind':'news'},10,time.time()+300,True)
        sid,new=await st.record_story(*args)
        assert new
        sid2,new2=await st.record_story(*args)
        assert sid2==sid and not new2
        counts=await st.queue_counts()
        assert counts['translation_jobs_pending']==1
        assert counts['alerts_pending']==1
        assert counts['enrichment_jobs_pending']==1
    asyncio.run(run())


def test_alert_atomic_claim_prevents_double_claim(settings) -> None:
    async def run():
        st=ControlStore(settings)
        assert await st.enqueue_alert('m1','market',5,{'venue':'TEST'},time.time()+60)
        a,b=await asyncio.gather(st.lease_alerts('a',10,30),st.lease_alerts('b',10,30))
        assert len(a)+len(b)==1
    asyncio.run(run())
