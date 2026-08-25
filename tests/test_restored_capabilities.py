from __future__ import annotations

import asyncio
from pathlib import Path

from v52.constants import DEFAULT_SERVICES, NEWS_RAW_PRODUCER_SERVICES
from v52.metrics import Metrics
from v52.news.gdelt import GdeltService, _QUERIES
from v52.news.google_mesh import GoogleMeshService
from v52.news.registry import SourceRegistry
from v52.news.sitemap import Site, parse_sitemap
from v52.news.social_bluesky import BlueskyService
from v52.news.social_x import XSocialService
from v52.structured.institutional import InstitutionalConnectorService
from v52.structured.prediction import polymarket_token_ids


def test_restored_planes_are_default_services() -> None:
    required = {
        "gdelt", "google_mesh", "x_social", "bluesky", "social_pollers", "sitemap",
        "institutional_connectors", "provider_flows", "premium_actions", "prediction",
    }
    assert required.issubset(set(DEFAULT_SERVICES))
    assert len(DEFAULT_SERVICES) == 35
    assert {"intelligence", "market_response", "research", "maintenance"}.issubset(set(DEFAULT_SERVICES))
    assert {"gdelt", "google_mesh", "x_social", "bluesky", "social_pollers", "sitemap"}.issubset(set(NEWS_RAW_PRODUCER_SERVICES))


def test_reference_runtime_coverage_is_not_registry_only(settings) -> None:
    r = SourceRegistry(settings)
    assert len(r.google_locales()) == 78
    assert len(r.x_handles()) >= 150
    assert r.summary()["parser_profiles"] >= 365
    # The parser registry must actually resolve a live domain profile.
    domains = list(r.parser_by_domain)
    assert domains
    assert r.parser_profile(domains[0]) is not None


def test_google_full_locale_topic_mesh(settings) -> None:
    async def run() -> None:
        svc = GoogleMeshService(settings, Metrics("google-mesh-test", settings.state_dir))
        try:
            sources = svc._build_sources()
            # 78 locales * (top + 8 topics) = 702 baseline before publisher/query additions.
            assert len(sources) >= 702
        finally:
            await svc.http.close()
    asyncio.run(run())


def test_sitemap_index_and_news_urlset_are_parsed() -> None:
    site = Site("Example", "https://example.com/", "GLOBAL_NEWS", "GLOBAL", 1)
    index = b'''<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://example.com/news.xml</loc></sitemap></sitemapindex>'''
    nested, items = parse_sitemap(site, "https://example.com/sitemap.xml", index, None)
    assert nested == ["https://example.com/news.xml"]
    assert items == []
    news = b'''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url><loc>https://example.com/news/market-shock</loc><news:news><news:publication_date>2026-08-24T00:00:00Z</news:publication_date><news:title>Central bank changes rates</news:title></news:news></url></urlset>'''
    nested2, items2 = parse_sitemap(site, "https://example.com/news.xml", news, {"parser_id": "fixture"})
    assert not nested2
    assert len(items2) == 1
    assert items2[0].title == "Central bank changes rates"


def test_bluesky_v2_handler_persists_and_cursor_store(settings) -> None:
    async def run() -> None:
        svc = BlueskyService(settings, Metrics("bsky-test", settings.state_dir))
        try:
            seq, inserted = await svc._handle_v2({
                "$type": "message",
                "payload": {
                    "$type": "network.bsky.jetstream.subscribeEvents#commit",
                    "operation": "create", "collection": "app.bsky.feed.post",
                    "did": "did:plc:test", "rkey": "abc", "seq": "123",
                    "record": {"text": "Federal Reserve rate decision moves global markets", "createdAt": "2026-08-24T00:00:00Z"},
                },
            })
            assert seq == "123" and inserted
            await svc.store.set_cursor(svc.cursor_key, "123")
            assert await svc.store.get_cursor(svc.cursor_key) == "123"
        finally:
            await svc.http.close()
    asyncio.run(run())


def test_x_rules_restore_handle_and_topic_coverage(settings) -> None:
    async def run() -> None:
        svc = XSocialService(settings, Metrics("x-test", settings.state_dir))
        try:
            rules = svc._desired_rules()
            assert rules
            joined = " ".join(r["value"] for r in rules)
            assert "from:" in joined
            assert any(r.get("tag", "").startswith("v52-fund-") for r in rules)
        finally:
            await svc.http.close()
    asyncio.run(run())


def test_gdelt_has_fundamental_query_families(settings) -> None:
    async def run() -> None:
        svc = GdeltService(settings, Metrics("gdelt-test", settings.state_dir))
        try:
            assert len(_QUERIES) >= 6
            assert all(str(q).strip() and str(cat).strip() for q, cat in _QUERIES)
        finally:
            await svc.http.close()
    asyncio.run(run())


def test_prediction_token_parser_supports_polymarket_shapes() -> None:
    assert polymarket_token_ids({"clobTokenIds": '["1","2"]'}) == ["1", "2"]
    assert polymarket_token_ids({"clob_token_ids": [3, 4]}) == ["3", "4"]


def test_institutional_registry_contracts_are_restored(settings) -> None:
    async def run() -> None:
        svc = InstitutionalConnectorService(settings, Metrics("inst-test", settings.state_dir))
        try:
            envs = set(svc.connector_envs)
            assert len(envs) >= 100
            assert "LSEG_MRN_CONNECTORS_JSON" in envs
            assert "EVM_NODE_CONNECTORS_JSON" in envs
            assert "SOLANA_NODE_CONNECTORS_JSON" in envs
            assert "BITCOIN_NODE_CONNECTORS_JSON" in envs
        finally:
            await svc.http.close()
    asyncio.run(run())
