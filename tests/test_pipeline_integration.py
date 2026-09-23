from __future__ import annotations

import asyncio
import json
import sqlite3
import time

from aiohttp import web

from v52.alerts.service import AlertService
from v52.config import Settings
from v52.metrics import Metrics
from v52.models import RawNewsItem, StoryCandidate
from v52.news.processor import NewsProcessor
from v52.storage.control import ControlStore
from v52.storage.news import NewsStore
from v52.translation.service import TranslationService


def _raw() -> RawNewsItem:
    return RawNewsItem(
        'pipe1','official-fed','Federal Reserve','https://www.federalreserve.gov/feeds/press_all.xml',
        'https://www.federalreserve.gov/newsevents/pressreleases/test.htm',
        'Federal Reserve announces interest rate cut',
        'The central bank announced a policy rate cut after the meeting.',
        'MONETARY_POLICY','US','en','2026-08-24T00:00:00Z','2026-08-24T00:00:01Z',10,{'official':True},
    )


def test_raw_to_story_processor_transaction(settings) -> None:
    async def run():
        news=NewsStore(settings)
        assert await news.insert_raw(_raw())
        metrics=Metrics('news_processor',settings.state_dir)
        proc=NewsProcessor(settings,metrics)
        stop=asyncio.Event()
        task=asyncio.create_task(proc.run(stop))
        for _ in range(100):
            if await news.pending_count()==0:
                break
            await asyncio.sleep(0.01)
        stop.set(); await asyncio.wait_for(task,2)
        assert await news.pending_count()==0
        con=sqlite3.connect(settings.data_dir/'db'/'control.sqlite3')
        try:
            row=con.execute('select title,materiality from stories').fetchone()
            assert row and row[0].startswith('Federal Reserve') and row[1]>=3
        finally: con.close()
    asyncio.run(run())


def test_central_translation_service_with_local_provider(settings, monkeypatch) -> None:
    async def run():
        app=web.Application()
        async def languages(request): return web.json_response([{'code':'en'},{'code':'fr'}])
        async def translate(request):
            data=await request.json()
            q=str(data.get('q') or '')
            return web.json_response({'translatedText': 'EN:'+q})
        app.router.add_get('/languages',languages)
        app.router.add_post('/translate',translate)
        runner=web.AppRunner(app); await runner.setup(); site=web.TCPSite(runner,'127.0.0.1',0); await site.start()
        port=site._server.sockets[0].getsockname()[1]
        monkeypatch.setenv('TRANSLATION_ENABLED','1')
        monkeypatch.setenv('LIBRETRANSLATE_URL',f'http://127.0.0.1:{port}')
        monkeypatch.setenv('TRANSLATION_CONCURRENCY','1')
        s=Settings.from_env(); st=ControlStore(s)
        c=StoryCandidate('tr1','La banque centrale baisse les taux','Décision officielle de politique monétaire','https://example.test/fr','MONETARY_POLICY','FR','fr','2026-08-24T00:00:00Z','2026-08-24T00:00:01Z',10,['official'],'Source','https://example.test',101)
        sid,is_new=await st.record_story(c,c.canonical_url,True,None,0,None,False)
        assert is_new
        metrics=Metrics('translation',s.state_dir); svc=TranslationService(s,metrics); stop=asyncio.Event()
        task=asyncio.create_task(svc.run(stop))
        for _ in range(200):
            story=await st.get_story(sid)
            if story and story.get('english_title'):
                break
            await asyncio.sleep(0.01)
        stop.set(); await asyncio.wait_for(task,3)
        story=await st.get_story(sid)
        assert story and str(story['english_title']).startswith('EN:')
        counts=await st.queue_counts(); assert counts['translation_jobs_done']==1
        await runner.cleanup()
    asyncio.run(run())


def test_durable_alert_claim_send_ack_local_telegram(settings, monkeypatch) -> None:
    async def run():
        received=[]
        app=web.Application()
        async def send(request):
            received.append(await request.json())
            return web.json_response({'ok':True,'result':{'message_id':777}})
        async def get_me(_request):
            return web.json_response({'ok':True,'result':{'id':123,'is_bot':True,'username':'v52_test_bot'}})
        async def get_chat(request):
            body=await request.json()
            return web.json_response({'ok':True,'result':{'id':int(body['chat_id']),'type':'private'}})
        token='123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'
        app.router.add_post(f'/bot{token}/sendMessage',send)
        app.router.add_get(f'/bot{token}/getMe',get_me)
        app.router.add_post(f'/bot{token}/getChat',get_chat)
        runner=web.AppRunner(app); await runner.setup(); site=web.TCPSite(runner,'127.0.0.1',0); await site.start()
        port=site._server.sockets[0].getsockname()[1]
        monkeypatch.setenv('TELEGRAM_ENABLED','1')
        monkeypatch.setenv('TELEGRAM_BOT_TOKEN',token)
        monkeypatch.setenv('TELEGRAM_CHAT_ID','42')
        monkeypatch.setenv('TELEGRAM_API_BASE',f'http://127.0.0.1:{port}')
        monkeypatch.setenv('TELEGRAM_PER_CHAT_MIN_GAP_SECONDS','0.03')
        monkeypatch.setenv('TELEGRAM_GLOBAL_RPS','30')
        s=Settings.from_env(); st=ControlStore(s)
        assert await st.enqueue_alert('intelligence:test:1','intelligence_signal',9,{'kind':'intelligence_signal','event_type':'LIQUIDATION_EVENT','event_title':'Large liquidation with confirmed impact','asset_id':'BTC','asset_name':'Bitcoin','action':'SELL_SHORT','horizon_seconds':300,'probability':.72,'calibrated_confidence':.64,'uncertainty':.2,'risk_score':.3,'entry_logic':'Confirmation','invalidation_logic':'Adverse move','target_logic':'Empirical target','exit_logic':'Regime change'},time.time()+60)
        metrics=Metrics('alerts',s.state_dir); svc=AlertService(s,metrics); stop=asyncio.Event()
        task=asyncio.create_task(svc.run(stop))
        for _ in range(200):
            counts=await st.queue_counts()
            if counts.get('alerts_sent',0)==1:
                break
            await asyncio.sleep(0.01)
        stop.set(); await asyncio.wait_for(task,3)
        counts=await st.queue_counts()
        assert counts['alerts_sent']==1 and counts['alerts_pending']==0 and counts['alerts_leased']==0
        assert len(received)==1 and received[0]['chat_id']=='42' and 'سیگنال فاندامنتال' in received[0]['text']
        await runner.cleanup()
    asyncio.run(run())
