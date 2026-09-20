from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlsplit

from ..config import Settings
from ..models import NewsSource


def _key(name: str, url: str, mode: str) -> str:
    return hashlib.sha1(f"{mode}|{name}|{url}".encode("utf-8", errors="ignore")).hexdigest()


def _domain(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


class SourceRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        ref = settings.reference_dir
        self.fundamental = json.loads((ref / "fundamental_sources.json").read_text(encoding="utf-8"))
        self.newswire = json.loads((ref / "V37_PROFESSIONAL_NEWSWIRE_MESH.json").read_text(encoding="utf-8"))
        self.parsers = json.loads((ref / "V37_SOURCE_PARSER_REGISTRY.json").read_text(encoding="utf-8"))
        self.free_api = json.loads((ref / "FREE_API_PROVIDER_REGISTRY_V36.json").read_text(encoding="utf-8"))
        self.discovery_seeds = json.loads((ref / "SOURCE_DISCOVERY_SEEDS_V35.json").read_text(encoding="utf-8"))
        self.tier_by_domain: dict[str, int] = {}
        self.parser_by_domain: dict[str, dict[str, Any]] = {}
        for profile in self.parsers.get("profiles", []):
            if not isinstance(profile, dict):
                continue
            domain = str(profile.get("domain") or "").lower().removeprefix("www.")
            if domain:
                self.parser_by_domain[domain] = dict(profile)
        for src in self.newswire.get("sources", []):
            domain = _domain(str(src.get("url") or ""))
            if domain:
                self.tier_by_domain[domain] = min(int(src.get("tier", 2)), self.tier_by_domain.get(domain, 99))

    def _tier(self, url: str, official: bool, base_poll: float) -> int:
        domain = _domain(url)
        if domain in self.tier_by_domain:
            return self.tier_by_domain[domain]
        if official and base_poll <= 10:
            return 0
        if official:
            return 1
        return 2

    def direct_sources(self) -> list[NewsSource]:
        out: list[NewsSource] = []
        for row in self.fundamental.get("direct_feeds", []):
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            if not self.settings.gdelt_enabled and (
                "gdelt" in str(row.get("name") or "").lower()
                or _domain(url).endswith("gdeltproject.org")
            ):
                continue
            poll = float(row.get("poll_seconds") or 60.0)
            official = bool(row.get("official", False))
            out.append(
                NewsSource(
                    key=_key(str(row.get("name") or url), url, "feed"),
                    name=str(row.get("name") or url),
                    url=url,
                    category=str(row.get("category") or "GENERAL"),
                    region=str(row.get("region") or "GLOBAL"),
                    language=str(row.get("language") or ""),
                    official=official,
                    tier=self._tier(url, official, poll),
                    mode="feed",
                    base_poll_seconds=poll,
                )
            )
        return out

    def priority_sources(self) -> list[NewsSource]:
        out: list[NewsSource] = []
        for row in self.fundamental.get("priority_pages", []):
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            if not self.settings.gdelt_enabled and (
                "gdelt" in str(row.get("name") or "").lower()
                or _domain(url).endswith("gdeltproject.org")
            ):
                continue
            poll = float(row.get("poll_seconds") or 30.0)
            out.append(
                NewsSource(
                    key=_key(str(row.get("name") or url), url, "page"),
                    name=str(row.get("name") or url),
                    url=url,
                    category=str(row.get("category") or "GENERAL"),
                    region=str(row.get("region") or "GLOBAL"),
                    language=str(row.get("language") or ""),
                    official=True,
                    tier=self._tier(url, True, poll),
                    mode="page",
                    base_poll_seconds=poll,
                )
            )
        return out

    def google_tier0_sources(self) -> list[NewsSource]:
        if not self.settings.news_google_fallback_enabled:
            return []
        out: list[NewsSource] = []
        seen: set[str] = set()
        for row in self.newswire.get("sources", []):
            if int(row.get("tier", 2)) != 0:
                continue
            url = str(row.get("url") or "")
            domain = _domain(url)
            if not domain or domain in seen:
                continue
            seen.add(domain)
            q = quote_plus(f"site:{domain}")
            gurl = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
            out.append(
                NewsSource(
                    key=_key(f"Google fallback {domain}", gurl, "google_feed"),
                    name=f"Google News fallback: {row.get('name') or domain}",
                    url=gurl,
                    category=str(row.get("category") or "GLOBAL_NEWS"),
                    region=str(row.get("region") or "GLOBAL"),
                    language="en",
                    official=False,
                    tier=0,
                    mode="google_feed",
                    base_poll_seconds=self.settings.news_google_tier0_poll_seconds,
                )
            )
        return out

    def hot_sources(self) -> list[NewsSource]:
        merged: dict[str, NewsSource] = {}
        for src in self.direct_sources():
            if src.tier == 0 or src.base_poll_seconds <= 10.0:
                merged[src.key] = src
        for src in self.priority_sources():
            if src.tier == 0 and src.base_poll_seconds <= 15.0:
                merged[src.key] = src
        for src in self.google_tier0_sources():
            merged[src.key] = src
        return list(merged.values())

    def coverage_sources(self) -> list[NewsSource]:
        hot = {x.key for x in self.hot_sources()}
        merged: dict[str, NewsSource] = {}
        for src in self.direct_sources() + self.priority_sources():
            if src.key not in hot:
                merged[src.key] = src
        return list(merged.values())

    def discovery_sites(self) -> list[dict[str, Any]]:
        rows = list(self.fundamental.get("discovery_sites", []))
        for row in self.discovery_seeds.get("seeds", []):
            if isinstance(row, dict):
                rows.append(row)
            elif isinstance(row, str):
                rows.append({"name": row, "url": row})
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            url = str(row.get("url") or "").strip()
            if url:
                unique[url] = row
        return list(unique.values())

    def parser_profile(self, url_or_domain: str) -> dict[str, Any] | None:
        domain = _domain(url_or_domain) if "://" in url_or_domain else url_or_domain.lower().removeprefix("www.")
        if not domain:
            return None
        profile = self.parser_by_domain.get(domain)
        if profile is not None:
            return dict(profile)
        # Some registries use a parent domain while the live URL uses a locale/subdomain.
        parts = domain.split(".")
        for i in range(1, max(1, len(parts) - 1)):
            candidate = ".".join(parts[i:])
            if candidate in self.parser_by_domain:
                return dict(self.parser_by_domain[candidate])
        return None

    def x_handles(self) -> list[str]:
        return [str(x).lstrip("@").strip() for x in self.fundamental.get("x_handles", []) if str(x).strip()]

    def google_locales(self) -> list[list[str]]:
        return [list(x) for x in self.fundamental.get("google_locales", []) if isinstance(x, list) and len(x) >= 4]

    def structured_providers(self) -> list[dict[str, Any]]:
        return [dict(x) for x in self.free_api.get("providers", [])]

    def summary(self) -> dict[str, int]:
        return {
            "direct": len(self.direct_sources()),
            "priority": len(self.priority_sources()),
            "google_tier0": len(self.google_tier0_sources()),
            "hot": len(self.hot_sources()),
            "coverage": len(self.coverage_sources()),
            "discovery_sites": len(self.discovery_sites()),
            "structured_providers": len(self.structured_providers()),
            "parser_profiles": len(self.parsers.get("profiles", [])),
        }
