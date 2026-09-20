from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from lxml import etree

from ..models import NewsSource, RawNewsItem
from ..timeutil import iso_now, parse_datetime, to_iso

_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"}
_WS = re.compile(r"\s+")


def clean_text(value: Any, limit: int = 20_000) -> str:
    s = html.unescape(str(value or ""))
    s = BeautifulSoup(s, "lxml").get_text(" ", strip=True) if "<" in s and ">" in s else s
    return _WS.sub(" ", s).strip()[:limit]


def canonical_url(url: str) -> str:
    from urllib.parse import parse_qsl, urlencode
    try:
        p = urlsplit(url.strip())
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in _TRACKING]
        path = p.path or "/"
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(query), ""))
    except Exception:
        return url.strip()


def _raw_key(source: NewsSource, guid: str, link: str, title: str, published: str | None) -> str:
    basis = guid or link or f"{title}|{published or ''}"
    return hashlib.sha256(f"{source.key}|{basis}".encode("utf-8", errors="ignore")).hexdigest()


def _xml_text(node: Any, names: tuple[str, ...]) -> str:
    for name in names:
        rows = node.xpath(f"./*[local-name()='{name}']")
        if not rows:
            continue
        child = rows[0]
        if name == "link":
            href = child.get("href")
            if href:
                return str(href)
        text = "".join(child.itertext()).strip()
        if text:
            return text
    return ""


def parse_feed(source: NewsSource, body: bytes, profile: dict[str, Any] | None = None) -> list[RawNewsItem]:
    """Parse RSS/Atom with libxml recovery mode and external entities/network disabled."""
    observed = iso_now()
    out: list[RawNewsItem] = []
    try:
        parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False)
        root = etree.fromstring(body, parser=parser)
    except (etree.XMLSyntaxError, ValueError, TypeError):
        return out
    if root is None:
        return out
    entries = root.xpath("//*[local-name()='item'] | //*[local-name()='entry']")
    for entry in entries[:200]:
        title = clean_text(_xml_text(entry, ("title",)), 1200)
        link = canonical_url(_xml_text(entry, ("link", "url")))
        guid = _xml_text(entry, ("guid", "id"))
        if not link and guid.startswith(("http://", "https://")):
            link = canonical_url(guid)
        if not title or not link:
            continue
        summary = clean_text(_xml_text(entry, ("summary", "description", "content", "encoded")), 12_000)
        # Atom <updated> is an edit time and can make an old article look new.
        published_raw = _xml_text(entry, ("pubDate", "published", "created", "date"))
        published = to_iso(parse_datetime(published_raw))
        raw_key = _raw_key(source, guid, link, title, published)
        out.append(
            RawNewsItem(
                raw_key=raw_key, source_key=source.key, source_name=source.name, source_url=source.url,
                item_url=link, title=title, summary=summary, category=source.category, region=source.region,
                language=source.language, published_at=published, observed_at=observed,
                priority=max(0, 10 - source.tier * 3 + int(source.official) * 2),
                payload={"mode": source.mode, "guid": guid, "parser": "lxml_xml_recover", "parser_profile": (profile or {}).get("parser_id"), "parser_stack": (profile or {}).get("parser_stack", [])},
            )
        )
    return out


def _iter_jsonld_articles(soup: BeautifulSoup) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        text = node.string or node.get_text("", strip=True)
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        for item in stack:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(x for x in graph if isinstance(x, dict))
            typ = item.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if any(str(x).lower() in {"newsarticle", "article", "reportagenewsarticle", "pressrelease"} for x in types):
                out.append(item)
    return out


def parse_priority_page(source: NewsSource, body: bytes, final_url: str | None = None, profile: dict[str, Any] | None = None) -> list[RawNewsItem]:
    text = body.decode("utf-8", errors="replace")
    soup = BeautifulSoup(text, "lxml")
    observed = iso_now()
    base = final_url or source.url
    out: list[RawNewsItem] = []
    seen: set[str] = set()

    for item in _iter_jsonld_articles(soup)[:100]:
        title = clean_text(item.get("headline") or item.get("name"), 1200)
        url = item.get("url") or item.get("mainEntityOfPage")
        if isinstance(url, dict):
            url = url.get("@id") or url.get("url")
        link = canonical_url(urljoin(base, str(url or "")))
        if not title or not link or link in seen:
            continue
        seen.add(link)
        published = to_iso(parse_datetime(item.get("datePublished") or item.get("dateModified")))
        summary = clean_text(item.get("description"), 12_000)
        out.append(
            RawNewsItem(
                raw_key=_raw_key(source, "", link, title, published),
                source_key=source.key, source_name=source.name, source_url=source.url, item_url=link,
                title=title, summary=summary, category=source.category, region=source.region,
                language=source.language, published_at=published, observed_at=observed,
                priority=max(0, 11 - source.tier * 3), payload={"mode": "jsonld_page", "parser_profile": (profile or {}).get("parser_id"), "parser_stack": (profile or {}).get("parser_stack", [])},
            )
        )

    # OpenGraph/article metadata is an explicit part of every V37 parser profile and often
    # survives when JSON-LD is absent or paywall shells hide the main DOM.
    if len(out) < 5:
        def meta_value(*keys: str) -> str:
            for key in keys:
                node = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
                if node and node.get("content"):
                    return str(node.get("content") or "")
            return ""
        og_title = clean_text(meta_value("og:title", "twitter:title"), 1200)
        og_url = canonical_url(urljoin(base, meta_value("og:url") or base))
        og_pub = to_iso(parse_datetime(meta_value("article:published_time", "datePublished", "date")))
        og_summary = clean_text(meta_value("og:description", "description", "twitter:description"), 12000)
        if og_title and og_url and og_url not in seen:
            seen.add(og_url)
            out.append(RawNewsItem(
                raw_key=_raw_key(source, "", og_url, og_title, og_pub),
                source_key=source.key, source_name=source.name, source_url=source.url, item_url=og_url,
                title=og_title, summary=og_summary, category=source.category, region=source.region,
                language=source.language, published_at=og_pub, observed_at=observed,
                priority=max(0, 10 - source.tier * 3),
                payload={"mode": "opengraph_page", "parser_profile": (profile or {}).get("parser_id"), "parser_stack": (profile or {}).get("parser_stack", [])},
            ))

    # Generic fallback: only likely content links. Untimed rows are archived but later excluded from realtime alerts.
    if len(out) < 5:
        for a in soup.select("article a[href], main a[href], [role=main] a[href]")[:250]:
            title = clean_text(a.get_text(" ", strip=True), 600)
            href = str(a.get("href") or "")
            if len(title) < 12 or not href:
                continue
            link = canonical_url(urljoin(base, href))
            if link in seen or urlsplit(link).netloc != urlsplit(base).netloc:
                continue
            low = f"{title} {link}".lower()
            if any(x in low for x in ("privacy", "cookie", "subscribe", "sign in", "login", "contact", "about us", "terms of")):
                continue
            seen.add(link)
            out.append(
                RawNewsItem(
                    raw_key=_raw_key(source, "", link, title, None),
                    source_key=source.key, source_name=source.name, source_url=source.url, item_url=link,
                    title=title, summary="", category=source.category, region=source.region,
                    language=source.language, published_at=None, observed_at=observed,
                    priority=max(0, 8 - source.tier * 3), payload={"mode": "html_link_fallback", "untimed": True, "parser_profile": (profile or {}).get("parser_id"), "parser_stack": (profile or {}).get("parser_stack", [])},
                )
            )
            if len(out) >= 30:
                break
    return out


def discover_feed_links(page_url: str, body: bytes) -> list[str]:
    text = body.decode("utf-8", errors="replace")
    soup = BeautifulSoup(text, "lxml")
    found: set[str] = set()
    for link in soup.find_all("link", href=True):
        typ = str(link.get("type") or "").lower()
        rel = " ".join(link.get("rel") or []).lower()
        if "rss" in typ or "atom" in typ or "alternate" in rel and ("xml" in typ or "rss" in typ):
            found.add(canonical_url(urljoin(page_url, link["href"])))
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        low = href.lower()
        if any(x in low for x in ("/rss", "/feed", "/atom", "rss.xml", "feed.xml", "atom.xml")):
            found.add(canonical_url(urljoin(page_url, href)))
    return sorted(x for x in found if x.startswith(("http://", "https://")))[:30]
