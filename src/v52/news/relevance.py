from __future__ import annotations

import hashlib
import os
import re
from typing import Iterable
from urllib.parse import urlsplit

try:
    from langdetect import DetectorFactory, LangDetectException, detect as statistical_detect
    DetectorFactory.seed = 0
except ImportError:
    class LangDetectException(Exception):
        """Compatibility error used only when optional language detection is absent."""
    statistical_detect = None
try:
    from unidecode import unidecode
except ImportError:
    def unidecode(value: str) -> str:
        return value

from ..models import StoryCandidate
from ..timeutil import parse_datetime, utc_now
from .parse import canonical_url, clean_text

_HIGH = {
    "federal reserve", "fed rate", "interest rate", "rate cut", "rate hike", "central bank", "ecb", "bank of england", "boj",
    "inflation", "cpi", "ppi", "payroll", "unemployment", "gdp", "recession", "treasury", "bond yield", "yield curve",
    "sec", "cftc", "regulator", "regulation", "sanction", "ofac", "tariff", "export control", "trade war",
    "opec", "crude oil", "oil production", "lng", "natural gas", "gold", "copper", "commodity",
    "earnings", "guidance", "merger", "acquisition", "bankruptcy", "default", "credit downgrade", "rating downgrade",
    "bitcoin", "ethereum", "solana", "sui", "crypto", "stablecoin", "etf", "exchange", "liquidation", "funding rate",
    "open interest", "whale", "hack", "exploit", "security breach", "outage", "listing", "delisting",
    "war", "ceasefire", "missile", "invasion", "geopolitical", "election", "government shutdown",
}
_MEDIUM = {
    "bank", "liquidity", "money supply", "mortgage", "housing", "manufacturing", "retail sales", "consumer confidence",
    "semiconductor", "chip", "artificial intelligence", "ai", "antitrust", "ipo", "dividend", "buyback", "layoff",
    "shipping", "freight", "port", "power grid", "electricity", "nuclear", "weather", "hurricane", "drought",
}
_ACTION = {
    "announces", "announced", "raises", "cuts", "approves", "rejects", "bans", "sanctions", "launches", "files", "sues",
    "halts", "suspends", "resumes", "acquires", "merges", "defaults", "misses", "beats", "warns", "orders", "signs",
}
_NOISE = {
    "sports", "football", "soccer", "basketball", "tennis", "celebrity", "movie", "music", "recipe", "travel guide",
    "horoscope", "lottery", "fashion", "dating", "gaming review",
}
_CATEGORY_WEIGHT = {
    "ECONOMY": 3, "MONETARY_POLICY": 4, "TREASURY": 4, "SANCTIONS": 5, "EXPORT_CONTROL": 4, "TRADE_POLICY": 4,
    "REGULATION": 4, "CRYPTO": 3, "ENERGY": 3, "COMMODITIES": 3, "BUSINESS": 2, "MARKETS": 3,
    "WORLD": 1, "GLOBAL_NEWS": 1, "POLICY": 3, "FILINGS": 3,
}

# A topical word is not an event.  In particular, broad BUSINESS/CRYPTO feeds
# contain thousands of evergreen articles that happen to say "bank" or "bitcoin".
# Require an identifiable change or release before a news item consumes the
# durable queue and the learner.  This is deliberately conservative for
# unverified aggregators; first-party releases with high materiality may use
# their summary when the headline is generic.
_EVENT_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\b(?:rate (?:cut|hike|decision)|cuts? (?:interest )?rates?|raises? (?:interest )?rates?|policy decision)\b",
    r"\b(?:cpi|ppi|inflation|payrolls?|unemployment|gdp|pmi|jobless claims)\b.{0,65}\b(?:rose|fell|rises|falls|increased|decreased|unexpected|surprise|released|at|to|from|percent|%)\b",
    r"\b(?:fed|federal reserve|ecb|central bank|bank of england|boj)\b.{0,90}\b(?:decides|announces?|cuts?|hikes?|raises?|holds?|emergency|votes?)\b",
    r"\b(?:fed|federal reserve|ecb|central bank|bank of england|boj)\b.{0,90}\bchanges? rates?\b",
    r"\b(?:sanctions?|tariffs?|export controls?|trade restrictions?)\b.{0,75}\b(?:imposes|imposed|announces?|announced|approves?|approved|bans?|banned|raises?|raised|lifts?|lifted|new|takes? effect)\b",
    r"\b(?:imposes|imposed|announces?|announced|approves?|approved|bans?|banned|raises?|raised|lifts?|lifted)\b.{0,75}\b(?:sanctions?|tariffs?|export controls?|trade restrictions?)\b",
    r"\b(?:opec|oil|crude|lng|gas|pipeline|refinery)\b.{0,75}\b(?:cuts? (?:oil )?(?:output|production)|raises? (?:oil )?(?:output|production)|outage|shutdown|disruption|quota|explosion|attack)\b",
    r"\b(?:earnings?|revenue|guidance|profit|sales)\b.{0,75}\b(?:beats?|misses?|raises?|cuts?|warns?|reports?|withdraws?|surprise)\b",
    r"\b(?:beats?|misses?|raises?|cuts?|withdraws?|reports?)\b.{0,75}\b(?:earnings?|revenue|guidance|profit|sales)\b",
    r"\b(?:merger|acquisition|takeover|ipo|buyback)\b.{0,55}\b(?:announced|approved|agreed|closed|completed|filed|launches|launched)\b",
    r"\b(?:announced|approved|agreed|closed|completed|filed|launches|launched)\b.{0,55}\b(?:merger|acquisition|takeover|ipo|buyback)\b",
    r"\b(?:bankruptcy|defaults?|credit downgrade|rating downgrade)\b",
    r"\b(?:bitcoin|ethereum|crypto|stablecoin|etf|exchange|token|protocol)\b.{0,90}\b(?:approved?|rejected?|listed?|delisted?|depeg|exploit|hack|breach|halted?|suspended?|liquidat|unlock|filing|files?)\w*\b",
    r"\b(?:listed?|delisted?|approved?|rejected?|exploit|hack|depeg|liquidat|unlock)\w*\b.{0,90}\b(?:bitcoin|ethereum|crypto|stablecoin|etf|exchange|token|protocol)\b",
    r"\b(?:missile (?:strike|attack)|air ?strike|invasion|ceasefire (?:agreement|declared|signed)|military strike|war declared|government shutdown)\b",
    r"\b(?:sec|cftc|ofac|regulator)\b.{0,75}\b(?:charges?|approves?|rejects?|sues?|fines?|designates?|bans?|orders?)\b",
    r"\b(?:withdrawals? suspended|exchange halted|stablecoin depeg|funds drained|protocol exploit|bond auction|debt ceiling)\b",
))

_SPECULATIVE_HEADLINE = re.compile(
    r"\b(?:week ahead|preview|what to know|may|might|could|expected|expects|"
    r"rumou?r|odds of|chances of|plans to|considering|opinion|analysis|reflects|"
    r"day\s+[\d,]+|explained)\b",
    re.IGNORECASE,
)


def has_event_evidence(title: str, summary: str = "", *, official: bool = False) -> bool:
    headline = unidecode(clean_text(title, 1200))
    if _SPECULATIVE_HEADLINE.search(headline):
        return False
    # Aggregator snippets often mix unrelated recommended articles into a
    # summary.  Only trusted first-party releases can qualify by summary alone.
    text = f"{headline} {unidecode(clean_text(summary, 1500))}" if official else headline
    return any(pattern.search(text) for pattern in _EVENT_PATTERNS)


def detect_language(title: str, summary: str, declared: str = "") -> str:
    """Deterministic, dependency-free language routing.

    Declared source metadata wins. Script detection handles non-Latin languages, while small
    stop-word profiles distinguish the common Latin languages. Unknown text is returned as
    ``und`` and is deliberately eligible for centralized auto-translation rather than silently
    assumed to be English.
    """
    d = (declared or "").strip().lower().split("-")[0]
    if d and d not in {"und", "unknown"}:
        return d
    text = f"{title} {summary}".strip().lower()
    if len(text) < 8:
        return "und"
    if re.search(r"[\u0600-\u06ff]", text): return "ar"
    if re.search(r"[\u0590-\u05ff]", text): return "he"
    if re.search(r"[\u0400-\u04ff]", text): return "ru"
    if re.search(r"[\u3040-\u30ff]", text): return "ja"
    if re.search(r"[\uac00-\ud7af]", text): return "ko"
    if re.search(r"[\u4e00-\u9fff]", text): return "zh"
    profiles = {
        "en": {"the","and","of","to","in","for","on","with","as","from","at","is","are","will","says"},
        "fr": {"le","la","les","des","de","du","et","dans","pour","sur","avec","est","une","un"},
        "es": {"el","la","los","las","de","del","y","en","para","con","una","un","por","es"},
        "de": {"der","die","das","den","dem","des","und","in","für","mit","auf","ist","von","zu"},
        "it": {"il","lo","la","gli","le","di","del","e","in","per","con","una","un","è"},
        "pt": {"o","a","os","as","de","do","da","e","em","para","com","uma","um","por","é"},
    }
    tokens = re.findall(r"[^\W\d_]+", text, flags=re.UNICODE)
    scores = {lang: sum(1 for t in tokens if t in words) for lang, words in profiles.items()}
    lang, score = max(scores.items(), key=lambda kv: kv[1])
    if score >= 2:
        return lang
    # Plain ASCII news text with no strong contrary signal is overwhelmingly English for
    # undeclared feeds in this source universe. Non-ASCII Latin remains undetermined.
    letters = [c for c in text if c.isalpha()]
    if letters and sum(ord(c) < 128 for c in letters) / len(letters) >= 0.98:
        return "en"
    # Restore V50 statistical language coverage for Latin/mixed scripts. The deterministic
    # script/stop-word checks above stay first so routing is stable for obvious cases.
    if statistical_detect is not None:
        try:
            detected = statistical_detect(text[:5000])
            if detected:
                return str(detected).split("-")[0].lower()
        except LangDetectException:
            return "und"
    return "und"


def _contains(text: str, terms: Iterable[str]) -> list[str]:
    matched: list[str] = []
    for term in terms:
        # Word boundaries prevent short financial terms such as ``ai``, ``sec`` and
        # ``war`` from matching chair, second or aware. Phrases retain whitespace.
        pattern = r"(?<!\w)" + re.escape(term).replace(r"\ ", r"\s+") + r"(?!\w)"
        if re.search(pattern, text, flags=re.IGNORECASE):
            matched.append(term)
    return matched


def materiality_score(title: str, summary: str, category: str, official: bool = False) -> tuple[int, list[str]]:
    text = f"{title} {summary}".lower()
    transliterated = unidecode(text)
    if transliterated != text:
        text = f"{text} {transliterated}"
    reasons: list[str] = []
    score = _CATEGORY_WEIGHT.get(category.upper(), 0)
    if score:
        reasons.append(f"category:{category}")
    high = _contains(text, _HIGH)
    med = _contains(text, _MEDIUM)
    actions = _contains(text, _ACTION)
    noise = _contains(text, _NOISE)
    if high:
        score += min(5, 2 + len(high) // 2)
        reasons.append("high:" + ",".join(high[:4]))
    if med:
        score += min(2, len(med))
        reasons.append("medium:" + ",".join(med[:3]))
    if actions:
        score += 1
        reasons.append("action:" + ",".join(actions[:2]))
    if has_event_evidence(title, summary, official=official):
        score += 2
        reasons.append("specific_event")
    if official:
        score += 2
        reasons.append("official")
    if noise and not high:
        score -= 6
        reasons.append("noise:" + ",".join(noise[:3]))
    if len(title) < 10:
        score -= 3
        reasons.append("short_title")
    return max(0, min(10, score)), reasons


def is_fundamental(score: int, title: str, category: str, summary: str = "", *, official: bool = False) -> bool:
    return score >= 4 and has_event_evidence(title, summary, official=official)


def fundamental_relevant(category: str, title: str, summary: str = "", *, official: bool = False) -> bool:
    """Fast shared fundamental gate for live external/social collectors.

    This intentionally delegates to the canonical materiality model so GDELT, Bluesky and
    other restored planes cannot drift into a separate filtering policy.
    """
    score, _reasons = materiality_score(title, summary, category, official=official)
    return is_fundamental(score, title, category, summary, official=official)


def admission_eligible(item: object, *, official: bool = False) -> bool:
    """Apply the same quality and event-time boundary before durable admission.

    A publisher timestamp is evidence about freshness, not a guarantee of
    upstream delivery time. Unknown timestamps require stronger materiality.
    """
    title = str(getattr(item, "title", ""))
    summary = str(getattr(item, "summary", ""))
    category = str(getattr(item, "category", ""))
    score, _ = materiality_score(title, summary, category, official=official)
    floor = max(4, int(os.getenv("NEWS_DURABLE_MATERIALITY_MIN", "4")))
    published = parse_datetime(getattr(item, "published_at", None))
    observed = parse_datetime(getattr(item, "observed_at", None)) or utc_now()
    if score < floor or not is_fundamental(score, title, category, summary, official=official and published is not None):
        return False
    if published is None:
        return score >= max(floor, int(os.getenv("NEWS_UNTIMED_MATERIALITY_MIN", "7")))
    age = (observed - published).total_seconds()
    return -30 <= age <= max(60, int(os.getenv("NEWS_MAX_STORY_AGE_SECONDS", "3600")))


def story_fingerprint(title: str, url: str) -> str:
    c = canonical_url(url)
    p = urlsplit(c)
    if p.scheme and p.netloc and p.path not in {"", "/"}:
        basis = c.lower()
    else:
        norm = re.sub(r"[^a-z0-9]+", " ", clean_text(title).lower()).strip()
        basis = norm
    return hashlib.sha256(basis.encode("utf-8", errors="ignore")).hexdigest()


def candidate_from_raw(row: dict, official: bool = False) -> tuple[StoryCandidate, str]:
    title = clean_text(row.get("title"), 1200)
    summary = clean_text(row.get("summary"), 12_000)
    category = str(row.get("category") or "GENERAL")
    lang = detect_language(title, summary, str(row.get("language") or ""))
    score, reasons = materiality_score(title, summary, category, official=official)
    url = canonical_url(str(row.get("item_url") or ""))
    return StoryCandidate(
        fingerprint=story_fingerprint(title, url),
        title=title,
        summary=summary,
        canonical_url=url,
        category=category,
        region=str(row.get("region") or "GLOBAL"),
        language=lang,
        published_at=row.get("published_at"),
        observed_at=str(row.get("observed_at")),
        materiality=score,
        reasons=reasons,
        source_name=str(row.get("source_name") or ""),
        source_url=str(row.get("source_url") or ""),
        raw_id=int(row["id"]),
    ), lang
