from __future__ import annotations

import asyncio
import json

from v52.metrics import Metrics
from v52.storage.structured import StructuredStore
from v52.structured.service import StructuredProviderRunner


class Result:
    ok=True; status=200; classification="ok"; error=""; retry_after=0.0
    body=json.dumps({"access_token":"oauth-token","expires_in":300}).encode()


class FakeHTTP:
    def __init__(self): self.calls=[]
    async def request(self, method, url, **kwargs):
        self.calls.append((method,url,kwargs)); return Result()


def test_oauth_client_credentials_and_static_headers(settings, monkeypatch) -> None:
    monkeypatch.setenv("OAUTH_ID", "client")
    monkeypatch.setenv("OAUTH_SECRET", "secret")
    provider={
        "slug":"oauth_fixture",
        "oauth":{"token_url":"https://auth.example/token","grant_type":"client_credentials","client_id_env":"OAUTH_ID","client_secret_env":"OAUTH_SECRET"},
        "request":{"url":"https://api.example/data","headers":{"X-Static":"yes"},"auth":{"type":"oauth_bearer"}},
    }
    async def run():
        fake=FakeHTTP()
        runner=StructuredProviderRunner(settings, Metrics("oauth-test", settings.state_dir), provider, fake, StructuredStore(settings))
        key, enabled=runner._credential(); assert enabled
        method,url,params,headers,body,data=await runner._request_parts(key)
        assert method=="GET" and url=="https://api.example/data"
        assert headers["X-Static"]=="yes"
        assert headers["Authorization"]=="Bearer oauth-token"
        assert fake.calls and fake.calls[0][1]=="https://auth.example/token"
    asyncio.run(run())


def test_bearer_auth_is_restored(settings, monkeypatch) -> None:
    monkeypatch.setenv("FIX_KEY", "abc")
    provider={"slug":"bearer_fixture","key_env":"FIX_KEY","request":{"url":"https://api.example/data","auth":{"type":"bearer"}}}
    async def run():
        runner=StructuredProviderRunner(settings, Metrics("bearer-test", settings.state_dir), provider, FakeHTTP(), StructuredStore(settings))
        key,enabled=runner._credential(); assert enabled
        *_,headers,body,data=await runner._request_parts(key)
        assert headers["Authorization"]=="Bearer abc"
    asyncio.run(run())
