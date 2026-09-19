from __future__ import annotations

import json
import hashlib
import math
import os
import re
import time
from dataclasses import replace
from typing import Any

from ..config import Settings
from ..loggingx import redact_text
from .taxonomy import ASSETS, TAXONOMY
from .types import EventUnderstanding
from ..storage.sqlite import connect


ALLOWED_EVENT_TYPES = {taxon.event_type: taxon for taxon in TAXONOMY}


class OpenAISemanticExtractor:
    """Optional evidence extractor outside the collector hot path.

    Structured output is treated as a proposal. Event types and asset identifiers are
    validated against local canonical registries before they can enter persistent memory.
    It never changes model promotion thresholds or production configuration.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        # V57.3 is deliberately local-only.  This hard boundary prevents a
        # stale server secret or .env override from creating paid API traffic.
        self.enabled = False
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.model = os.getenv("OPENAI_MODEL", "gpt-5.4-mini").strip() or "gpt-5.4-mini"
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.embedding_model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small").strip() or "text-embedding-3-small"
        self.embedding_dimensions = max(32, min(256, int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "64"))))
        self.local_embedding_model = f"local-signed-hash-{self.embedding_dimensions}-v2"
        self._embedding_cache: dict[str, list[float]] = {}
        self.failure_cooldown_seconds = max(
            5.0,
            float(
                os.getenv(
                    "OPENAI_SEMANTICS_FAILURE_COOLDOWN_SECONDS",
                    "60",
                )
            ),
        )
        self._failure_cooldown_until = 0.0
        self.client = None

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()

    @staticmethod
    def _output_text(response: dict[str, Any]) -> str:
        if isinstance(response.get("output_text"), str):
            return response["output_text"]
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    return content["text"]
        return ""

    async def extract(self, story: dict[str, Any], deterministic: EventUnderstanding) -> dict[str, Any] | None:
        if not self.enabled or not self.api_key:
            return None
        if time.time() < self._failure_cooldown_until:
            return None
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "event_type": {"type": "string", "enum": sorted(ALLOWED_EVENT_TYPES)},
                "concise_summary": {"type": "string", "maxLength": 1200},
                "entities": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "asset_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                "asset_candidates": {"type": "array", "maxItems": 12, "items": {"type": "object", "additionalProperties": False,
                    "properties": {"entity_candidate":{"type":["string","null"]},"ticker":{"type":["string","null"]},"company":{"type":["string","null"]},"exchange":{"type":["string","null"]},"sector":{"type":["string","null"]},"industry":{"type":["string","null"]},"token_symbol":{"type":["string","null"]},"token_name":{"type":["string","null"]},"chain":{"type":["string","null"]},"contract":{"type":["string","null"]},"market_pairs":{"type":"array","items":{"type":"string"}},"aliases":{"type":"array","items":{"type":"string"}}},
                    "required":["entity_candidate","ticker","company","exchange","sector","industry","token_symbol","token_name","chain","contract","market_pairs","aliases"]}},
                "first_order_impact": {"type": "string", "maxLength": 1200},
                "second_order_impact": {"type": "string", "maxLength": 1200},
                "surprise": {"type": ["number", "null"], "minimum": -3, "maximum": 3},
                "priced_in": {"type": "string", "enum": ["UNKNOWN", "UNLIKELY", "PARTIAL", "LIKELY"]},
                "source_conflict": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "rationale": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                "causal_chain": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                "counter_evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                "regime_dependencies": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                "time_horizon_rationale": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                "directional_hypotheses": {"type": "array", "maxItems": 12, "items": {"type": "object", "additionalProperties": False,
                    "properties": {"asset_id":{"type":"string"},"direction":{"type":"string","enum":["UP","DOWN","MIXED","NEUTRAL"]},"confidence":{"type":"number","minimum":0,"maximum":1},"mechanism":{"type":"string","maxLength":500}},
                    "required":["asset_id","direction","confidence","mechanism"]}},
            },
            "required": ["event_type", "concise_summary", "entities", "asset_ids", "asset_candidates", "first_order_impact", "second_order_impact", "surprise", "priced_in", "source_conflict", "confidence", "rationale", "causal_chain", "counter_evidence", "regime_dependencies", "time_horizon_rationale", "directional_hypotheses"],
        }
        body = {
            "model": self.model,
            "store": False,
            "instructions": (
                "Extract market-relevant facts only. Do not invent prices, consensus, certainty, or trading advice. "
                "Use only allowed event types. Asset fields are candidates, never registrations; include chain and contract for tokens. State UNKNOWN when evidence is absent."
            ),
            "input": json.dumps({
                "title": story.get("english_title") or story.get("title"),
                "summary": story.get("english_summary") or story.get("summary"),
                "source": story.get("source_name"),
                "published_at": story.get("published_at"),
                "deterministic_event_type": deterministic.event_type,
                "deterministic_assets": [x["asset_id"] for x in deterministic.assets],
            }, ensure_ascii=False),
            "text": {"format": {"type": "json_schema", "name": "v52_market_event", "strict": True, "schema": schema}},
            "max_output_tokens": 1200,
        }
        result = await self.client.post(
            f"{self.base_url}/responses",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json_body=body,
        )
        if not result.ok:
            self._failure_cooldown_until = (
                time.time() + self.failure_cooldown_seconds
            )
            raise RuntimeError(
                redact_text(
                    f"OpenAI semantics failed "
                    f"{result.classification}:{result.status}"
                )
            )
        self._failure_cooldown_until = 0.0
        response = json.loads(result.body)
        text = self._output_text(response)
        payload = json.loads(text)
        if payload.get("event_type") not in ALLOWED_EVENT_TYPES:
            raise ValueError("OpenAI returned event type outside canonical taxonomy")
        return payload

    async def embed(self, story: dict[str, Any], deterministic: EventUnderstanding) -> tuple[str, list[float]] | None:
        """Create one bounded semantic vector per unique material news text."""
        if not self.enabled or not self.api_key:
            return None
        # Responses and embeddings share the same external API budget.  When
        # semantic extraction is rate-limited, do not immediately amplify the
        # failure with a second request for every queued story.
        if time.time() < self._failure_cooldown_until:
            return None
        text = "\n".join((
            str(story.get("english_title") or story.get("title") or ""),
            str(story.get("english_summary") or story.get("summary") or ""),
            f"event={deterministic.event_type}; domain={deterministic.domain}",
            "assets=" + ",".join(str(x.get("asset_id")) for x in deterministic.assets),
        ))[:12_000]
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        cached = self._embedding_cache.get(content_hash)
        if cached is not None:
            return content_hash, list(cached)
        result = await self.client.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json_body={"model": self.embedding_model, "input": text, "encoding_format": "float", "dimensions": self.embedding_dimensions},
        )
        if not result.ok:
            self._failure_cooldown_until = time.time() + self.failure_cooldown_seconds
            raise RuntimeError(redact_text(f"OpenAI embedding failed {result.classification}:{result.status}"))
        response = json.loads(result.body)
        vector = ((response.get("data") or [{}])[0]).get("embedding") or []
        values = [float(value) for value in vector]
        if len(values) != self.embedding_dimensions or not all(math.isfinite(value) for value in values):
            raise ValueError("OpenAI embedding dimension/value validation failed")
        self._failure_cooldown_until = 0.0
        if len(self._embedding_cache) >= 256:
            self._embedding_cache.pop(next(iter(self._embedding_cache)))
        self._embedding_cache[content_hash] = values
        return content_hash, values

    def local_embed(self, story: dict[str, Any], deterministic: EventUnderstanding) -> tuple[str, list[float]]:
        """Bounded multilingual lexical vector used when the external API is unavailable.

        Signed feature hashing keeps a stable neural input contract without a
        vocabulary table or a heavyweight language model.  Delayed barrier
        feedback can therefore learn recurring word/subword patterns even while
        the richer OpenAI representation is rate-limited.
        """
        title = str(story.get("english_title") or story.get("title") or "")
        summary = str(story.get("english_summary") or story.get("summary") or "")
        text = "\n".join((
            title,
            summary,
            f"event {deterministic.event_type} domain {deterministic.domain}",
            "assets " + " ".join(str(x.get("asset_id")) for x in deterministic.assets),
        ))[:12_000].lower()
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        tokens = re.findall(r"[^\W_]+", text, flags=re.UNICODE)[:2048]
        features = list(tokens)
        features.extend(f"{left}::{right}" for left, right in zip(tokens, tokens[1:]))
        title_tokens = re.findall(r"[^\W_]+", title.lower(), flags=re.UNICODE)[:256]
        summary_tokens = re.findall(r"[^\W_]+", summary.lower(), flags=re.UNICODE)[:1024]
        features.extend(f"title::{token}" for token in title_tokens)
        features.extend(f"summary::{token}" for token in summary_tokens)
        features.extend(
            f"tri::{left}::{middle}::{right}"
            for left, middle, right in zip(tokens, tokens[1:], tokens[2:])
        )
        negators = {"not", "no", "never", "without", "نه", "نیست", "نشد", "نمی", "بدون"}
        for index, token in enumerate(tokens):
            if token in negators:
                features.extend(f"neg::{value}" for value in tokens[index + 1:index + 4])
        # Character fragments preserve useful signal for Persian, Arabic and
        # unknown tickers where whitespace tokenization alone is insufficient.
        compact = re.sub(r"\s+", " ", text)
        features.extend(compact[index:index + 3] for index in range(max(0, len(compact) - 2)) if " " not in compact[index:index + 3])
        values = [0.0] * self.embedding_dimensions
        for feature in features[:8192]:
            digest = hashlib.blake2b(feature.encode("utf-8", errors="ignore"), digest_size=8).digest()
            integer = int.from_bytes(digest, "big")
            index = integer % self.embedding_dimensions
            sign = 1.0 if integer & (1 << 63) else -1.0
            values[index] += sign / math.sqrt(1.0 + len(feature))
        norm = math.sqrt(sum(value * value for value in values))
        if norm:
            values = [value / norm for value in values]
        return content_hash, values

    @staticmethod
    def local_analysis(story: dict[str, Any], deterministic: EventUnderstanding) -> dict[str, Any]:
        """Auditable local semantic evidence attached to the learned text vector."""
        text = " ".join((
            str(story.get("english_title") or story.get("title") or ""),
            str(story.get("english_summary") or story.get("summary") or ""),
        )).lower()
        mechanisms = {
            "policy": ("rate", "inflation", "central bank", "نرخ", "تورم", "بانک مرکزی"),
            "supply": ("supply", "inventory", "production", "ذخایر", "تولید", "عرضه"),
            "demand": ("demand", "consumption", "orders", "تقاضا", "مصرف", "سفارش"),
            "regulation": ("sanction", "regulation", "ban", "تحریم", "مقررات", "ممنوع"),
            "earnings": ("earnings", "revenue", "guidance", "سود", "درآمد", "چشم انداز"),
            "risk": ("war", "attack", "default", "جنگ", "حمله", "نکول"),
        }
        matched = [name for name, terms in mechanisms.items() if any(term in text for term in terms)]
        return {
            "engine": "local_deterministic_semantics_v2",
            "event_type": deterministic.event_type,
            "domain": deterministic.domain,
            "subtype": deterministic.subtype,
            "mechanisms": matched,
            "assets": [str(item.get("asset_id")) for item in deterministic.assets],
            "directional_hypotheses": [
                {
                    "asset_id": str(item.get("asset_id")),
                    "direction": str(item.get("directional_bias") or "MIXED"),
                    "confidence": float(item.get("mapping_confidence") or 0.0),
                    "mechanism": str(item.get("mapping_reason") or "canonical_taxonomy"),
                }
                for item in deterministic.assets
            ],
            "rationale": [str(item)[:300] for item in deterministic.rationale[:12]],
            "source": str(story.get("source_name") or ""),
            "source_url": str(story.get("source_url") or story.get("item_url") or ""),
        }

    async def narrate_learning(self, evidence: dict[str, Any]) -> str | None:
        if not self.enabled or not self.api_key:
            return None
        result=await self.client.post(
            f"{self.base_url}/responses",
            headers={"Authorization":f"Bearer {self.api_key}","Content-Type":"application/json"},
            json_body={
                "model":self.model,"store":False,
                "instructions":(
                    "به فارسی روان و حرفه‌ای یک گزارش کوتاه و غیرتکراری از یادگیری مدل بازار بنویس. "
                    "فقط از اعداد ورودی استفاده کن؛ سودآوری یا قطعیت را ادعا نکن. تفاوت الگو، نمونه، بازده R، "
                    "برد/باخت/timeout، دارایی، نوع خبر و سطح درصد را توضیح بده و صریحاً بگو نتیجه فعلاً shadow است."
                ),
                "input":json.dumps(evidence,ensure_ascii=False,default=str),
                "max_output_tokens":700,
            },
        )
        if not result.ok:return None
        response=json.loads(result.body);text=self._output_text(response).strip()
        return text[:3500] or None


def apply_validated_semantics(base: EventUnderstanding, payload: dict[str, Any] | None, universe:Any|None=None) -> EventUnderstanding:
    if not payload or float(payload.get("confidence") or 0.0) < .70:
        return base
    taxon = ALLOWED_EVENT_TYPES[str(payload["event_type"])]
    proposed=set(payload.get("asset_ids") or [])
    asset_ids={asset for asset in proposed if asset in ASSETS}
    if universe is not None:
        asset_ids.update(universe.discover({'asset_candidates':payload.get('asset_candidates') or []}))
        with connect(universe.path,universe.settings) as con:
            asset_ids.update(asset for asset in proposed if con.execute('SELECT 1 FROM assets WHERE asset_id=?',(asset,)).fetchone())
    existing = {str(x["asset_id"]): dict(x) for x in base.assets}
    for asset_id in asset_ids:
        if asset_id not in existing:
            if universe is None:continue
            with connect(universe.path,universe.settings) as con:row=con.execute('SELECT * FROM assets WHERE asset_id=?',(asset_id,)).fetchone()
            if not row:continue
            existing[asset_id] = {
                "symbol":row['symbol'],"name":row['name'],"asset_class":row['asset_class'],"market_symbol":row['market_symbol'], "asset_id": asset_id, "impact_order": 1,
                "directional_bias": "MIXED", "mapping_confidence": float(payload["confidence"]),
                "mapping_reason": "openai_validated_semantics",
            }
    rationale = list(base.rationale)
    rationale.extend(str(x)[:300] for x in payload.get("rationale") or [])
    rationale.extend("cause=" + str(x)[:260] for x in payload.get("causal_chain") or [])
    rationale.extend("counter=" + str(x)[:260] for x in payload.get("counter_evidence") or [])
    rationale.extend("regime=" + str(x)[:260] for x in payload.get("regime_dependencies") or [])
    rationale.extend("horizon=" + str(x)[:260] for x in payload.get("time_horizon_rationale") or [])
    rationale.extend(
        f"hypothesis={x.get('asset_id')}:{x.get('direction')}:{float(x.get('confidence') or 0):.2f}:{str(x.get('mechanism') or '')[:180]}"
        for x in payload.get("directional_hypotheses") or [] if isinstance(x, dict)
    )
    rationale.extend((f"priced_in={payload.get('priced_in')}", f"source_conflict={bool(payload.get('source_conflict'))}"))
    return replace(
        base,
        event_type=taxon.event_type,
        domain=taxon.domain,
        summary=str(payload.get("concise_summary") or base.summary)[:1200],
        assets=tuple(existing.values()),
        surprise=payload.get("surprise") if payload.get("surprise") is not None else base.surprise,
        source_credibility=base.source_credibility * (.8 if payload.get("source_conflict") else 1.0),
        expected_horizons=taxon.horizons,
        rationale=tuple(rationale),
        extraction_method="deterministic+openai_structured",
        extraction_version="52.0-taxonomy-1+responses-json-schema",
    )
