from __future__ import annotations

import asyncio
from aiohttp import web
from v52.net.http import ResilientHttpClient


def test_http_classification_and_retry_after(settings) -> None:
    async def run():
        app=web.Application()
        async def ok_handler(_request):
            return web.Response(text='ok')
        async def rate_handler(_request):
            return web.Response(status=429, headers={'Retry-After':'0.01'})
        app.router.add_get('/ok', ok_handler)
        app.router.add_get('/rate', rate_handler)
        runner=web.AppRunner(app); await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0); await site.start()
        port=site._server.sockets[0].getsockname()[1]
        client=ResilientHttpClient(settings,name='test',retries=0,total_timeout=2)
        try:
            ok=await client.get(f'http://127.0.0.1:{port}/ok')
            rate=await client.get(f'http://127.0.0.1:{port}/rate')
            assert ok.ok and ok.classification=='ok'
            assert not rate.ok and rate.classification=='rate_limited'
            assert rate.retry_after is not None
        finally:
            await client.close(); await runner.cleanup()
    asyncio.run(run())
