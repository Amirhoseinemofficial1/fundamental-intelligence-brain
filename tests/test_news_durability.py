from __future__ import annotations

import asyncio
from v52.models import RawNewsItem
from v52.storage.news import NewsStore


def item(key: str) -> RawNewsItem:
    return RawNewsItem(key,'src','Source','https://source.test/feed','https://source.test/a','Fed cuts interest rate','Policy decision','MONETARY_POLICY','US','en','2026-08-24T00:00:00Z','2026-08-24T00:00:01Z',10,{'official':True})


def test_raw_news_idempotent_lease_retry_ack(settings) -> None:
    async def run():
        store=NewsStore(settings)
        assert await store.insert_raw(item('k1')) is True
        assert await store.insert_raw(item('k1')) is False
        assert await store.pending_count() == 1
        rows=await store.lease_raw('owner1',10,0.05)
        assert len(rows)==1 and rows[0]['state']=='leased'
        await store.retry_raw(rows[0]['id'],'temporary',0)
        rows2=await store.lease_raw('owner2',10,1)
        assert len(rows2)==1
        await store.ack_raw(rows2[0]['id'])
        assert await store.pending_count()==0
    asyncio.run(run())


def test_expired_raw_lease_is_reclaimed(settings) -> None:
    async def run():
        store=NewsStore(settings)
        await store.insert_raw(item('k2'))
        first=await store.lease_raw('dead-worker',1,0.01)
        assert first
        await asyncio.sleep(0.03)
        second=await store.lease_raw('recovery-worker',1,1)
        assert second and second[0]['id']==first[0]['id']
    asyncio.run(run())
