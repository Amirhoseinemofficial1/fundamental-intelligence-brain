from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
from typing import Any

from bs4 import BeautifulSoup

from ..config import Settings
from ..loggingx import redact_text
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..storage.news import NewsStore
from .external_common import make_raw_news

logger = logging.getLogger(__name__)

_QUERIES = (
    ("ECONOMY", '"central bank" OR inflation OR "interest rate" OR treasury'),
    ("SANCTIONS", 'sanctions OR tariff OR "export controls"'),
    ("MARKETS", 'earnings OR merger OR acquisition OR bankruptcy OR downgrade'),
    ("GEOPOLITICS", 'ceasefire OR missile OR airstrike OR blockade'),
    ("ENERGY", 'OPEC OR oil OR LNG OR refinery OR copper OR gold'),
    ("CRYPTO", 'bitcoin OR ethereum OR stablecoin OR ETF OR liquidation OR hack'),
)


def _strip_html(value: str) -> str:
    return " ".join(BeautifulSoup(value or "", "lxml").get_text(" ", strip=True).split())


class SocialPollerService:
    """Credentialed/authorized social and broad-news pollers restored from V50.

    Reddit, Mastodon and NewsAPI are kept off the hot direct-feed process, so provider rate limits
    cannot delay first-party feeds. Missing credentials disable only the corresponding adapter.
    """

    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.store = NewsStore(settings)
        self.http = ResilientHttpClient(settings, name="social-pollers", concurrency=6, per_host_limit=2, retries=2, total_timeout=20.0, max_body_bytes=8_000_000)

    async def _reddit_token(self) -> str:
        if not self.settings.reddit_client_id or not self.settings.reddit_client_secret:
            return ""
        auth = base64.b64encode(f"{self.settings.reddit_client_id}:{self.settings.reddit_client_secret}".encode()).decode()
        result = await self.http.post(
            "https://www.reddit.com/api/v1/access_token",
            headers={"Authorization": f"Basic {auth}", "User-Agent": self.settings.user_agent},
            data={"grant_type": "client_credentials"},
        )
        if not result.ok:
            self.metrics.inc(f"reddit_auth_{result.classification}")
            return ""
        try:
            obj = json.loads(result.body)
            return str(obj.get("access_token") or "")
        except Exception:
            self.metrics.inc("reddit_auth_parse_error")
            return ""

    async def _reddit_loop(self, stop_event: asyncio.Event) -> None:
        if not self.settings.reddit_enabled:
            return
        token = ""
        token_fail_wait = 60.0
        qidx = 0
        while not stop_event.is_set():
            if not token:
                token = await self._reddit_token()
                if not token:
                    await self.store.source_result("reddit", "Reddit OAuth", "https://oauth.reddit.com/", ok=False, status="disabled_or_auth_failed", http_status=None, latency_ms=0.0, error="OAuth token unavailable")
                    try: await asyncio.wait_for(stop_event.wait(), timeout=token_fail_wait)
                    except asyncio.TimeoutError:
                        token_fail_wait = min(1800.0, token_fail_wait * 2.0); continue
            cat, query = _QUERIES[qidx % len(_QUERIES)]; qidx += 1
            result = await self.http.get(
                "https://oauth.reddit.com/search",
                headers={"Authorization": f"Bearer {token}", "User-Agent": self.settings.user_agent},
                params={"q": query, "sort": "new", "t": "day", "limit": "100", "type": "link", "raw_json": "1"},
            )
            wait = self.settings.reddit_poll_seconds
            if result.ok:
                try:
                    obj = json.loads(result.body)
                    children = (((obj.get("data") or {}).get("children")) or []) if isinstance(obj, dict) else []
                    items = []
                    for child in children:
                        row = child.get("data") if isinstance(child, dict) else None
                        if not isinstance(row, dict): continue
                        pid = str(row.get("id") or row.get("name") or "")
                        permalink = str(row.get("permalink") or "")
                        url = "https://www.reddit.com" + permalink if permalink.startswith("/") else str(row.get("url") or permalink)
                        item = make_raw_news(
                            source_key="reddit", source_name=f"r/{row.get('subreddit') or 'reddit'}", source_url="https://oauth.reddit.com/search",
                            item_url=url, title=str(row.get("title") or ""), summary=str(row.get("selftext") or ""), category=cat,
                            region="GLOBAL", language="en", published_at=row.get("created_utc"), external_id=pid, priority=5,
                            payload={"source_type":"REDDIT_API","subreddit":row.get("subreddit"),"author":row.get("author"),"score":row.get("score"),"num_comments":row.get("num_comments")},
                        )
                        if item: items.append(item)
                    inserted = await self.store.insert_raw_many(items)
                    self.metrics.inc("news_raw_inserted", inserted); self.metrics.inc("reddit_rows", len(items)); self.metrics.inc("reddit_duplicates", max(0, len(items)-inserted))
                    await self.store.source_result("reddit", "Reddit search", "https://oauth.reddit.com/search", ok=True, status="ok", http_status=result.status, latency_ms=result.latency_ms, parsed=len(items), new_items=inserted)
                except Exception as exc:
                    self.metrics.inc("reddit_parse_error"); logger.warning("Reddit parse failed error=%s", exc)
            else:
                self.metrics.inc(f"reddit_{result.classification}")
                if result.status == 401:
                    token = ""
                wait = max(wait, result.retry_after or 0.0)
                await self.store.source_result("reddit", "Reddit search", "https://oauth.reddit.com/search", ok=False, status=result.classification, http_status=result.status, latency_ms=result.latency_ms, rate_limited=result.classification=="rate_limited", error=result.error)
            try: await asyncio.wait_for(stop_event.wait(), timeout=wait + random.random())
            except asyncio.TimeoutError: continue

    async def _mastodon_loop(self, stop_event: asyncio.Event) -> None:
        base = self.settings.mastodon_base_url.rstrip("/")
        if not self.settings.mastodon_enabled or not base:
            return
        headers = {"Authorization": f"Bearer {self.settings.mastodon_access_token}"} if self.settings.mastodon_access_token else {}
        qidx = 0
        while not stop_event.is_set():
            cat, query = _QUERIES[qidx % len(_QUERIES)]; qidx += 1
            url = base + "/api/v2/search"
            result = await self.http.get(url, headers=headers or None, params={"q": query, "type": "statuses", "limit": "40"})
            wait = self.settings.mastodon_poll_seconds
            if result.ok:
                try:
                    obj = json.loads(result.body)
                    statuses = obj.get("statuses") if isinstance(obj, dict) else []
                    items = []
                    for row in (statuses or []):
                        if not isinstance(row, dict): continue
                        acct = row.get("account") if isinstance(row.get("account"), dict) else {}
                        text = _strip_html(str(row.get("content") or ""))
                        item = make_raw_news(
                            source_key=f"mastodon:{url}", source_name=str(acct.get("acct") or "Mastodon"), source_url=url,
                            item_url=str(row.get("url") or row.get("uri") or ""), title=text[:1000], summary=text, category=cat,
                            region="GLOBAL", language=str(row.get("language") or ""), published_at=row.get("created_at"),
                            external_id=str(row.get("id") or row.get("uri") or ""), priority=5,
                            payload={"source_type":"MASTODON_API","account":acct.get("acct"),"reblogs_count":row.get("reblogs_count"),"favourites_count":row.get("favourites_count")},
                        )
                        if item: items.append(item)
                    inserted = await self.store.insert_raw_many(items)
                    self.metrics.inc("news_raw_inserted", inserted); self.metrics.inc("mastodon_rows", len(items)); self.metrics.inc("mastodon_duplicates", max(0,len(items)-inserted))
                    await self.store.source_result("mastodon", "Mastodon search", url, ok=True, status="ok", http_status=result.status, latency_ms=result.latency_ms, parsed=len(items), new_items=inserted)
                except Exception as exc:
                    self.metrics.inc("mastodon_parse_error"); logger.warning("Mastodon parse failed error=%s", exc)
            else:
                self.metrics.inc(f"mastodon_{result.classification}"); wait=max(wait,result.retry_after or 0.0)
                await self.store.source_result("mastodon", "Mastodon search", url, ok=False, status=result.classification, http_status=result.status, latency_ms=result.latency_ms, rate_limited=result.classification=="rate_limited", error=result.error)
            try: await asyncio.wait_for(stop_event.wait(), timeout=wait + random.random())
            except asyncio.TimeoutError: continue

    async def _newsapi_loop(self, stop_event: asyncio.Event) -> None:
        if not self.settings.newsapi_enabled or not self.settings.newsapi_key:
            return
        idx=0
        url="https://newsapi.org/v2/everything"
        while not stop_event.is_set():
            cat,query=_QUERIES[idx%len(_QUERIES)]; idx+=1
            result=await self.http.get(url,params={"q":query,"sortBy":"publishedAt","pageSize":"100","language":"en","apiKey":self.settings.newsapi_key})
            wait=self.settings.newsapi_poll_seconds
            if result.ok:
                try:
                    obj=json.loads(result.body); rows=obj.get("articles") if isinstance(obj,dict) else []
                    items=[]
                    for row in (rows or []):
                        if not isinstance(row,dict):continue
                        source=row.get("source") if isinstance(row.get("source"),dict) else {}
                        item=make_raw_news(source_key="newsapi",source_name=str(source.get("name") or "NewsAPI"),source_url=url,item_url=str(row.get("url") or ""),title=str(row.get("title") or ""),summary=str(row.get("description") or row.get("content") or ""),category=cat,region="GLOBAL",language="en",published_at=row.get("publishedAt"),external_id=str(row.get("url") or ""),priority=5,payload={"source_type":"NEWSAPI","query":query})
                        if item:items.append(item)
                    inserted=await self.store.insert_raw_many(items);self.metrics.inc("news_raw_inserted",inserted);self.metrics.inc("newsapi_rows",len(items));self.metrics.inc("newsapi_duplicates",max(0,len(items)-inserted))
                    await self.store.source_result("newsapi","NewsAPI",url,ok=True,status="ok",http_status=result.status,latency_ms=result.latency_ms,parsed=len(items),new_items=inserted)
                except Exception as exc:
                    self.metrics.inc("newsapi_parse_error");logger.warning("NewsAPI parse failed error=%s",exc)
            else:
                self.metrics.inc(f"newsapi_{result.classification}");wait=max(wait,result.retry_after or 0.0)
                await self.store.source_result("newsapi","NewsAPI",url,ok=False,status=result.classification,http_status=result.status,latency_ms=result.latency_ms,rate_limited=result.classification=="rate_limited",error=result.error)
            try:await asyncio.wait_for(stop_event.wait(),timeout=wait+random.random())
            except asyncio.TimeoutError:continue

    async def run(self, stop_event: asyncio.Event) -> None:
        tasks=[]
        if self.settings.reddit_enabled: tasks.append(asyncio.create_task(self._reddit_loop(stop_event),name="social:reddit"))
        if self.settings.mastodon_enabled and self.settings.mastodon_base_url: tasks.append(asyncio.create_task(self._mastodon_loop(stop_event),name="social:mastodon"))
        if self.settings.newsapi_enabled and self.settings.newsapi_key: tasks.append(asyncio.create_task(self._newsapi_loop(stop_event),name="social:newsapi"))
        self.metrics.set("social_poller_tasks",len(tasks))
        if not tasks:
            logger.info("social/news provider pollers have no enabled credentials/configuration")
            await self.http.close();await stop_event.wait();return
        try:
            await stop_event.wait()
        finally:
            for task in tasks:task.cancel()
            results=await asyncio.gather(*tasks,return_exceptions=True)
            for result in results:
                if isinstance(result,BaseException) and not isinstance(result,asyncio.CancelledError):
                    self.metrics.inc("social_poller_shutdown_error");logger.error("social poller task error=%s",redact_text(str(result)))
            await self.http.close()
