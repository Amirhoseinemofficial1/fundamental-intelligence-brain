from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import time
from typing import Any
from urllib.parse import quote, urlsplit

from ..config import Settings
from ..metrics import Metrics
from ..storage.control import ControlStore
from .features import FeatureStore
from .inference import RuntimeInferenceEngine
from .impact_router import fanout_budget, route_story
from .learning import PolicyEngine
from .observation import DynamicAssetUniverse, ObservationBridge, PublicEquityDirectoryProvider, PublicTokenDirectoryProvider
from .openai_semantics import OpenAISemanticExtractor, apply_validated_semantics
from .store import BrainStore
from .taxonomy import classify_story
from .warmup import RuntimeWarmup
from .analogues import HistoricalAnalogueEngine
from .reference_market import ActiveAssetWatchlist
from .signal_consolidation import SignalConsolidator
from .active_signal_agent import ActiveSignalAgent
from .semantic_engines import SurpriseEngine,PricedInEngine
from .context_builder import RuntimeSemanticContextBuilder
from .telegram_producers import TelegramIntelligenceProducers
from ..storage.sqlite import connect
from ..timeutil import parse_datetime


logger = logging.getLogger(__name__)

# These provider snapshots lack a verified surprise, affected asset and
# direction. The bridge keeps them in the raw source DB; this is also a
# defensive gate for callers that submit observations directly.
FEATURE_ONLY_OBSERVATION_TYPES = frozenset({
    'PREDICTION_MARKET_STATE', 'PREDICTION_MARKET_UPDATE',
    'PREDICTION_MARKET_REALTIME', 'SEC_CURRENT_FILING',
})


def live_news_freshness_ok(story: dict[str, Any], *, now: float | None = None) -> bool:
    """Only timed first-party news can create a live, time-sensitive signal.

    Aggregator timestamps can describe when the aggregator saw or edited a
    story, rather than when the publisher made the event public. Such stories
    remain useful for discovery and delayed evaluation, not live execution.
    """
    if story.get("_structured_observation"):
        return True
    source_host = (urlsplit(str(story.get("source_url") or "")).hostname or "").lower()
    if not source_host or source_host in {
        "news.google.com", "api.gdeltproject.org", "x.com", "twitter.com",
        "bsky.app", "reddit.com", "www.reddit.com",
    } or source_host.endswith((".bsky.social", ".reddit.com")):
        return False
    published = parse_datetime(story.get("published_at"))
    observed = parse_datetime(story.get("first_observed_at") or story.get("observed_at"))
    if published is None or observed is None:
        return False
    current = time.time() if now is None else now
    age = current - published.timestamp()
    detection_age = (observed - published).total_seconds()
    return 0 <= age <= max(30, int(os.getenv("NEWS_LIVE_SIGNAL_MAX_AGE_SECONDS", "60"))) and -30 <= detection_age <= 60


class IntelligenceService:
    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.control = ControlStore(settings)
        self.brain = BrainStore(settings)
        self.semantics = OpenAISemanticExtractor(settings)
        self.observations = ObservationBridge(settings)
        self.universe = DynamicAssetUniverse(settings)
        self.equity_directory = PublicEquityDirectoryProvider(settings)
        self.token_directory = PublicTokenDirectoryProvider(settings)
        self.features = FeatureStore(settings)
        self.inference = RuntimeInferenceEngine(settings)
        self.policy = PolicyEngine(settings)
        self.warmup = RuntimeWarmup(settings)
        self.analogues = HistoricalAnalogueEngine(settings)
        self.watchlist = ActiveAssetWatchlist(settings)
        self.consolidator = SignalConsolidator()
        self.active_signals = ActiveSignalAgent(settings)
        self.telegram = TelegramIntelligenceProducers(self.control)
        self.semantic_context = RuntimeSemanticContextBuilder(settings)
        self.minimum_samples = max(5, int(os.getenv("INTELLIGENCE_MIN_HISTORICAL_SAMPLES", "30")))
        self.openai_min_materiality = max(0, min(10, int(os.getenv("OPENAI_SEMANTICS_MIN_MATERIALITY", "8"))))
        self.signal_expiry = max(60, int(os.getenv("INTELLIGENCE_SIGNAL_EXPIRY_SECONDS", "1800")))
        self.structured_batch = max(
            1,
            min(
                500,
                int(os.getenv("INTELLIGENCE_STRUCTURED_BATCH", "100"))
            )
        )
        self.structured_general_triggers = (
            os.getenv(
                "INTELLIGENCE_STRUCTURED_GENERAL_TRIGGERS",
                "0"
            ).strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.max_assets_per_event=max(1,min(8,int(os.getenv('INTELLIGENCE_MAX_ASSETS_PER_EVENT','3'))))
        self.max_horizons_per_event=max(1,min(6,int(os.getenv('INTELLIGENCE_MAX_HORIZONS_PER_EVENT','3'))))
        self.min_prediction_importance=max(0.0,min(1.0,float(os.getenv('INTELLIGENCE_MIN_PREDICTION_IMPORTANCE','.55'))))
        self.local_semantic_min_materiality=max(3,min(10,int(os.getenv('LOCAL_SEMANTICS_MIN_MATERIALITY','4'))))
        self.fastlane_batch=max(1,min(100,int(os.getenv('INTELLIGENCE_FASTLANE_BATCH','20'))))

    def _bounded_horizons(self, expected: tuple[int,...], observation: bool, limit: int | None = None) -> tuple[int,...]:
        ordered=tuple(sorted(set(int(value) for value in expected if int(value)>0)))
        if not ordered:return (300,)
        if observation:
            return (min(ordered,key=lambda value:abs(value-300)),)
        maximum=self.max_horizons_per_event if limit is None else max(0,int(limit))
        if maximum == 0:return ()
        anchors=(300,3600,86400)
        selected=[]
        for anchor in anchors:
            candidate=min(ordered,key=lambda value:abs(value-anchor))
            if candidate not in selected:selected.append(candidate)
        for value in ordered:
            if len(selected)>=maximum:break
            if value not in selected:selected.append(value)
        return tuple(sorted(selected[:maximum]))

    async def _directory_discovery(self,story:dict[str,Any], understanding:Any, discovered:tuple[str,...])->tuple[str,...]:
        """Resolve unknown issuers/tokens on the normal runtime path without LLM.

        Queries are generated deterministically from structured entities first and
        then from conservative headline noun phrases; the full headline is only a
        last fallback.
        """
        if discovered:return discovered
        text=" ".join(str(story.get(key) or "") for key in ("title","summary","category"));upper=text.upper()
        queries=[]
        explicit=story.get('entity_candidates') or story.get('asset_candidates') or story.get('token_candidates') or []
        for candidate in explicit if isinstance(explicit,list) else []:
            if isinstance(candidate,dict):
                query=candidate.get('company') or candidate.get('issuer') or candidate.get('project') or candidate.get('token_name') or candidate.get('name')
                if query:queries.append(str(query))
        for entity in getattr(understanding,'entities',()) or ():
            if isinstance(entity,dict) and entity.get('name'):queries.append(str(entity['name']))
        for key in ('company','issuer','project','token_name','name'):
            if story.get(key):queries.append(str(story[key]))
        title=str(story.get('title') or '').strip()
        if title:
            import re
            trigger=r'\b(?:reports?|announces?|raises?|cuts?|launches?|lists?|files?|beats?|misses?|guidance|earnings|revenue|shares?|stock|token|protocol|airdrop|dex|acquires?|merges?)\b'
            match=re.search(trigger,title,flags=re.I)
            if match and match.start()>1:queries.append(title[:match.start()].strip(' :-—|'))
            queries.append(title)
        # Stable dedupe; reject obviously useless one-character/numeric queries.
        seen=set();queries=[q for q in queries if len(q.strip())>=2 and not (q.upper() in seen or seen.add(q.upper()))]
        token_like=any(word in upper for word in (" TOKEN","PROTOCOL","AIRDROP","DEX","CONTRACT","LISTING")) or bool(story.get('contract') or story.get('contract_address'))
        equity_like=any(word in upper for word in ("COMPANY","EARNINGS","GUIDANCE","REVENUE","SHARES","STOCK"))
        provider=self.token_directory if token_like else self.equity_directory if equity_like else None
        if provider is None:return discovered
        for query in queries[:8]:
            try:
                resolved=await asyncio.to_thread(self.universe.resolve_directory,query,provider)
                if resolved:return resolved
            except asyncio.CancelledError:raise
            except Exception as exc:
                self.metrics.inc('asset_directory_resolution_failed');logger.warning('deterministic asset directory lookup failed type=%s',type(exc).__name__)
        return discovered

    def _live_artifact_gate_sync(self) -> bool:
        with connect(self.brain.path,self.settings) as con:
            enabled=con.execute("SELECT value FROM metadata WHERE key='signals_enabled'").fetchone()
            meta=con.execute("SELECT artifact_id FROM model_artifacts WHERE expert='meta' AND state='CHAMPION' AND promoted_at IS NOT NULL").fetchone()
            if not enabled or str(enabled['value'])!='1' or not meta:return False
            minimum_shadow=max(10,int(os.getenv('MODEL_MIN_SHADOW_OUTCOMES','30')))
            minimum_specialists=max(1,int(os.getenv('QUALIFICATION_MIN_SPECIALIST_CHAMPIONS','4')))
            specialists=int(con.execute("SELECT COUNT(*) count FROM model_artifacts WHERE expert NOT IN ('meta','entry','exit','risk','regime_transition','magnitude_return','magnitude_mfe','magnitude_mae','time_to_peak') AND expert NOT LIKE 'horizon_score:%' AND state='CHAMPION'").fetchone()['count'])
            validated=con.execute("SELECT 1 FROM improvement_records WHERE model_version=? AND validation_window='unseen_runtime_shadow' AND sample_size>=?",(meta['artifact_id'],minimum_shadow)).fetchone() is not None
            return validated and specialists>=minimum_specialists

    async def _understand(self, story: dict[str, Any], deterministic_first: bool = False):
        preliminary = classify_story(story)
        similarity = 0.0 if deterministic_first else await self.brain.historical_similarity(preliminary.event_type, preliminary.title)
        understanding = classify_story(story, similarity)
        semantic_route=story.get('_semantic_route') or {}
        if semantic_route:
            understanding=dataclasses.replace(
                understanding,
                event_type=str(semantic_route.get('event_type') or understanding.event_type),
                domain=str(semantic_route.get('domain') or understanding.domain),
                subtype=str(semantic_route.get('subtype') or understanding.subtype),
                expected_horizons=tuple(int(x) for x in (semantic_route.get('horizons') or understanding.expected_horizons)),
                rationale=understanding.rationale+(f"semantic_route={','.join(semantic_route.get('experts') or ())}",),
                extraction_method='deterministic_semantic_router',
                extraction_version='54.0-routing-1',
            )
        observed_dt=parse_datetime(story.get('first_observed_at') or story.get('observed_at') or understanding.observed_at)
        occurred_dt=parse_datetime(understanding.occurred_at) or parse_datetime(story.get('published_at')) or observed_dt
        decision_cutoff=(observed_dt.timestamp() if observed_dt else time.time())
        event_anchor=min(occurred_dt.timestamp() if occurred_dt else decision_cutoff,decision_cutoff)
        analogues = [] if deterministic_first else await asyncio.to_thread(self.analogues.find, understanding, 10, event_anchor)
        semantic_payload={key:story.get(key) for key in ('actual','forecast','consensus','previous','revision','unit','surprise_unit','surprise_family','pre_event_price_move','prior_related_stories','rumors','positioning_zscore','anticipation_score') if story.get(key) is not None}
        try:
            summary_payload=json.loads(story.get('summary') or '{}')
            if isinstance(summary_payload,dict):semantic_payload.update(summary_payload)
        except (TypeError,ValueError):semantic_payload={key:value for key,value in semantic_payload.items()}
        preliminary_assets=[str(item.get('asset_id')) for item in understanding.assets if item.get('asset_id')]
        if not deterministic_first:
            semantic_payload=await asyncio.to_thread(self.semantic_context.build,understanding.event_type,preliminary_assets,semantic_payload,analogues,event_anchor=event_anchor,decision_cutoff=decision_cutoff)
        surprise=SurpriseEngine.evaluate(semantic_payload,semantic_payload.get('historical_surprise_distribution'))
        semantic_payload.update({'raw_surprise':surprise.get('raw_surprise'),'standardized_surprise':surprise.get('standardized_surprise'),'surprise_unit':semantic_payload.get('surprise_unit') or semantic_payload.get('unit') or 'native','surprise_family':semantic_payload.get('surprise_family') or understanding.event_type})
        story['_v57_semantic_context']=semantic_payload
        story['_v56_semantic_context']=semantic_payload
        priced_in=PricedInEngine.evaluate(semantic_payload,analogues)
        understanding = dataclasses.replace(understanding,historical_analogues=tuple(int(x['event_id']) for x in analogues),surprise=surprise['standardized_surprise'] if surprise['standardized_surprise'] is not None else surprise['raw_surprise'] if surprise['raw_surprise'] is not None else understanding.surprise,priced_in_score=max(understanding.priced_in_score,priced_in['score'],max((float(x['score']) for x in analogues),default=0)*.7),rationale=understanding.rationale+(f"surprise_context={surprise['valid']}",f"priced_in_components={priced_in['valid_components']}"))
        discovered = self.universe.discover(story)
        if not deterministic_first:
            discovered = await self._directory_discovery(story,understanding,discovered)
        known = {str(x['asset_id']) for x in understanding.assets}
        if set(discovered) - known:
            extra=[]
            with connect(self.brain.path,self.settings) as con:
                for asset_id in sorted(set(discovered)-known):
                    row=con.execute('SELECT * FROM assets WHERE asset_id=?',(asset_id,)).fetchone()
                    if row:extra.append({'asset_id':asset_id,'symbol':row['symbol'],'name':row['name'],'asset_class':row['asset_class'],'market_symbol':row['market_symbol'],'impact_order':1,'directional_bias':'MIXED','mapping_confidence':.88,'mapping_reason':'dynamic_universe'})
            understanding=dataclasses.replace(understanding,assets=understanding.assets+tuple(extra))
        # Structured provider observations already carry deterministic semantic
        # routing. They can arrive hundreds/thousands of times per minute and must
        # never serialize the ingestion hot path through an LLM call.
        if (
            not deterministic_first
            and not story.get('_structured_observation')
            and int(story.get("materiality") or 0) >= self.local_semantic_min_materiality
        ):
            proposal=None
            if self.semantics.enabled:
                try:
                    proposal = await self.semantics.extract(story, understanding)
                    understanding = apply_validated_semantics(understanding, proposal, self.universe)
                    if proposal:
                        self.metrics.inc("openai_semantic_completed")
                        story['_openai_semantics']=proposal
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.metrics.inc("openai_semantic_fallback")
                    logger.warning(
                        "OpenAI semantic enrichment failed; deterministic extraction retained error=%s detail=%s",
                        type(exc).__name__,
                        str(exc),
                    )
            embedded=None
            if self.semantics.enabled:
                try:
                    embedded=await self.semantics.embed(story,understanding)
                    if embedded:
                        story['_semantic_embedding_hash'],story['_semantic_embedding']=embedded
                        story['_semantic_embedding_model']=self.semantics.embedding_model
                        self.metrics.inc('openai_embedding_completed')
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.metrics.inc('openai_embedding_fallback')
                    logger.warning('OpenAI semantic embedding failed; structured understanding retained error=%s detail=%s',type(exc).__name__,str(exc))
            if not embedded:
                story['_semantic_embedding_hash'],story['_semantic_embedding']=self.semantics.local_embed(story,understanding)
                story['_semantic_embedding_model']=self.semantics.local_embedding_model
                story['_local_semantics']=self.semantics.local_analysis(story,understanding)
                self.metrics.inc('local_semantic_embedding_completed')
        return understanding

    @staticmethod
    def _signal_payload(event: dict[str, Any], asset: dict[str, Any], decision, signal_id: int) -> dict[str, Any]:
        return {
            "kind": "intelligence_signal",
            "signal_id": signal_id,
            "event_id": event["id"],
            "event_type": event["event_type"],
            "event_title": event["title"],
            "asset_id": asset["asset_id"],
            "asset_name": asset["name"],
            "action": decision.action,
            "horizon_seconds": decision.horizon_seconds,
            "probability": decision.probability,
            "calibrated_confidence": decision.calibrated_confidence,
            "expected_magnitude": decision.expected_magnitude,
            "uncertainty": decision.uncertainty,
            "risk_score": decision.risk_score,
            "expected_value": decision.expected_value,
            "entry_logic": decision.entry_logic,
            "invalidation_logic": decision.invalidation_logic,
            "target_logic": decision.target_logic,
            "exit_logic": decision.exit_logic,
            "expert_agreement": decision.expert_agreement,
            "evidence_count": decision.evidence_count,
            "mode": decision.mode,
            "reasons": decision.reasons,
            "sources": event.get("sources", []),
            "event_summary": event.get("summary", ""),
            "evaluation_levels_pct": [0.1,0.3,0.5,1,3,5,10],
            "evaluation_risk_reward": "1:1",
            "risk_disclaimer": "Probabilistic intelligence, not guaranteed profit. Use bounded risk.",
        }

    def _reference_quote_sync(self,asset_id:str,cutoff:float)->dict[str,Any]|None:
        maximum=max(60,int(os.getenv('PROVISIONAL_PRICE_MAX_AGE_SECONDS','900')))
        with connect(self.brain.path,self.settings) as con:
            row=con.execute("SELECT provider,observed_at,available_at,price,volume,quality,metadata_json FROM reference_prices WHERE asset_id=? AND available_at<=? ORDER BY observed_at DESC,available_at DESC LIMIT 1",(asset_id,cutoff)).fetchone()
        if not row:return None
        result=dict(row);result['age_seconds']=max(0.0,cutoff-float(result['observed_at']))
        if float(result['price'] or 0)<=0 or result['age_seconds']>maximum:return None
        return result

    @staticmethod
    def _market_verification_url(asset:dict[str,Any])->str:
        asset_id=str(asset.get('asset_id') or '')
        symbol=str(asset.get('market_symbol') or asset.get('symbol') or asset_id).upper().replace('/','').replace('-','')
        exact={'BTC':'BINANCE:BTCUSDT','ETH':'BINANCE:ETHUSDT','BTCUSDT':'BINANCE:BTCUSDT','ETHUSDT':'BINANCE:ETHUSDT',
               'XAUUSD':'OANDA:XAUUSD','WTI':'TVC:USOIL','CL':'NYMEX:CL1!','BRENT':'TVC:UKOIL','BRN':'TVC:UKOIL',
               'EURUSD':'OANDA:EURUSD','USDJPY':'OANDA:USDJPY','GBPUSD':'OANDA:GBPUSD','DXY':'TVC:DXY',
               'SPX':'SP:SPX','NDX':'NASDAQ:NDX','DJI':'DJ:DJI','RUT':'TVC:RUT','VIX':'CBOE:VIX'}
        if asset_id.startswith('equity:XNAS:'):symbol='NASDAQ:'+symbol
        elif asset_id.startswith('equity:XNYS:'):symbol='NYSE:'+symbol
        elif asset_id.startswith('equity:ARCX:'):symbol='AMEX:'+symbol
        else:symbol=exact.get(symbol,exact.get(asset_id,symbol))
        return 'https://www.tradingview.com/chart/?symbol='+quote(symbol,safe='') if symbol else ''

    @staticmethod
    def _signal_asset_allowed(asset:dict[str,Any])->bool:
        asset_id=str(asset.get('asset_id') or '').upper();symbol=str(asset.get('symbol') or asset_id).upper()
        klass=str(asset.get('asset_class') or '').upper()
        if klass=='CRYPTO':return asset_id in {'BTC','ETH'} or symbol in {'BTC','ETH'}
        if klass=='COMMODITY':return asset_id in {'XAUUSD','WTI','BRENT'} or symbol in {'XAUUSD','WTI','BRENT'}
        if klass=='FOREX':return True
        if klass=='INDEX':return asset_id in {'SPX','NDX','DJI','RUT','VIX','DXY'} or symbol in {'SPX','NDX','DJI','RUT','VIX','DXY'}
        if klass=='EQUITY':return asset_id.startswith(('EQUITY:XNAS:','EQUITY:XNYS:','EQUITY:ARCX:','EQUITY:BATS:'))
        return False

    @staticmethod
    def _provisional_action(decision,feature:dict[str,Any],mode:str,news_fresh:bool,reference:dict[str,Any]|None,event:dict[str,Any],asset:dict[str,Any])->dict[str,str]|None:
        if os.getenv('TELEGRAM_PROVISIONAL_AFTER_WARMUP','1').strip().lower() not in {'1','true','yes','on'}:return None
        if mode!='CONSERVATIVE' or not news_fresh or reference is None:return None
        if float(feature.get('quality') or 0)<float(os.getenv('TELEGRAM_PROVISIONAL_MIN_QUALITY','.38')):return None
        if float(asset.get('mapping_confidence') or 0)<.75:return None
        mapping=str(asset.get('mapping_reason') or '')
        if not (mapping in {'explicit_mention','dynamic_universe'} or mapping.startswith('taxonomy:')):return None
        if not any(urlsplit(str(x.get('source_url') or '')).scheme in {'http','https'} for x in event.get('sources',[]) if isinstance(x,dict)):return None
        bias=str(asset.get('directional_bias') or 'MIXED').upper()
        event_type=str(event.get('event_type') or '').upper()
        allowed={'CENTRAL_BANK_DECISION','WAR_ESCALATION','CEASEFIRE','SANCTIONS','OPEC_DECISION',
                 'ENERGY_SUPPLY_DISRUPTION','EARNINGS','GUIDANCE','REGULATORY_ACTION','EXCHANGE_LISTING',
                 'EXCHANGE_DELISTING','PROTOCOL_EXPLOIT','EXCHANGE_HACK','ETF_DECISION','TOKEN_UNLOCK',
                 'TOKEN_SUPPLY_CHANGE','PROTOCOL_UPGRADE'}
        if event_type not in allowed:return None
        text=' '.join((str(event.get('title') or ''),str(event.get('summary') or ''))).lower()
        if bias not in {'UP','DOWN'}:
            if event_type in {'EXCHANGE_LISTING','PROTOCOL_UPGRADE'}:bias='UP'
            elif event_type in {'EXCHANGE_DELISTING','PROTOCOL_EXPLOIT','EXCHANGE_HACK','TOKEN_UNLOCK'}:bias='DOWN'
            elif event_type=='TOKEN_SUPPLY_CHANGE':
                if 'burn' in text:bias='UP'
                elif 'mint' in text:bias='DOWN'
            elif event_type=='ETF_DECISION':
                if 'approved' in text:bias='UP'
                elif 'rejected' in text:bias='DOWN'
            elif event_type=='EARNINGS':
                if 'beats estimates' in text:bias='UP'
                elif 'misses estimates' in text:bias='DOWN'
            elif event_type=='GUIDANCE':
                if 'raises guidance' in text:bias='UP'
                elif any(x in text for x in ('cuts guidance','withdraws guidance','profit warning')):bias='DOWN'
            elif event_type=='REGULATORY_ACTION':
                if 'regulatory approval' in text:bias='UP'
                elif any(x in text for x in ('license revoked','regulator fines','sec charges')):bias='DOWN'
            elif event_type=='CENTRAL_BANK_DECISION' and any(x in text for x in ('federal reserve','fomc',' fed ')):
                if any(x in text for x in ('rate hike','raises interest rate','raised interest rate')):bias='DOWN'
                elif any(x in text for x in ('rate cut','cuts interest rate','cut interest rate')):bias='UP'
        if bias not in {'UP','DOWN'}:return None
        return {'action':'WATCH_BUY' if bias=='UP' else 'WATCH_SELL','direction':bias,
                'basis':f'DETERMINISTIC_EVENT_MECHANISM:{event_type}:{mapping}'}

    @staticmethod
    def _human_signal_drivers(event:dict[str,Any],asset:dict[str,Any],feature:dict[str,Any])->list[str]:
        bias=str(asset.get('directional_bias') or 'MIXED').upper();mapping=str(asset.get('mapping_reason') or 'unknown')
        return [
            f"رویداد {event.get('event_type')} با اهمیت {float(event.get('importance') or 0):.0%} و تازگی {float(event.get('novelty') or 0):.0%} ثبت شده است.",
            f"ارتباط خبر با {asset.get('symbol') or asset.get('asset_id')} از مسیر {mapping} با اطمینان {float(asset.get('mapping_confidence') or 0):.0%} تأیید شده است.",
            f"اثر بنیادی استخراج‌شده: {bias}; کیفیت داده در لحظه تصمیم {float(feature.get('quality') or 0):.0%} است.",
            f"تعداد منابع مستقل ثبت‌شده برای رویداد: {len(event.get('sources') or [])}.",
        ]

    @staticmethod
    def _risk_warning_payloads(event:dict[str,Any],asset:dict[str,Any],votes:list[Any])->list[dict[str,Any]]:
        warnings=[];pump=max((float(v.direction_probability) for v in votes if v.expert=='pump_risk'),default=0.0);dump=max((float(v.direction_probability) for v in votes if v.expert=='dump_risk'),default=0.0)
        if pump>=.75:warnings.append({'kind':'pump_warning','asset_id':asset['asset_id'],'event_title':event['title'],'confidence':pump,'summary':'Pump-risk Champion detected abnormal listing/flow/liquidity interaction.'})
        if dump>=.75:warnings.append({'kind':'dump_warning','asset_id':asset['asset_id'],'event_title':event['title'],'confidence':dump,'summary':'Dump-risk Champion detected adverse unlock/flow/liquidity interaction.'})
        return warnings

    async def process_story(
        self,
        story: dict[str, Any],
        stop_event: asyncio.Event | None = None,
        *,
        fast_lane: bool = False,
        refinement_only: bool = False,
    ) -> tuple[int, list[int]]:
        is_replay=bool(story.get('_replay'))
        impact = story.get('_impact_route')
        if not isinstance(impact,dict) or not impact.get('priority'):
            impact = route_story(story).as_dict()
            story['_impact_route'] = impact
        # Keep the normal call shape stable for existing integrations that
        # replace ``_understand`` with a one-argument test/provider adapter.
        understanding = (
            await self._understand(story, deterministic_first=True)
            if fast_lane else await self._understand(story)
        )
        impact_priority=str(impact.get('priority') or understanding.priority)
        understanding=dataclasses.replace(
            understanding,
            importance=max(float(understanding.importance),float(impact.get('score') or 0)/100.0),
            priority=impact_priority,
            rationale=understanding.rationale+(
                f"global_impact_score={int(impact.get('score') or 0)}",
                f"impact_lane={impact_priority}",
                f"deterministic_first={fast_lane}",
            ),
        )
        if stop_event is not None and stop_event.is_set():
            return 0, []
        mode = 'REPLAY' if is_replay else str((await self.warmup.status())["mode"])
        asset_budget,horizon_budget=fanout_budget(
            understanding.priority,len(understanding.assets),len(understanding.expected_horizons)
        )
        bounded_assets = tuple(sorted(
            understanding.assets,
            key=lambda item: (
                int(item.get('impact_order') or 99),
                -float(item.get('mapping_confidence') or 0),
                str(item.get('asset_id') or ''),
            ),
        )[:asset_budget])
        bounded_horizons = self._bounded_horizons(
            tuple(int(x) for x in understanding.expected_horizons),
            mode == 'OBSERVATION',
            horizon_budget,
        )
        understanding = dataclasses.replace(
            understanding,
            assets=bounded_assets,
            expected_horizons=bounded_horizons,
        )
        story['_create_response_targets'] = bool(
            float(understanding.importance or 0) >= self.min_prediction_importance
            or story.get('_structured_observation')
        )
        event_id, is_new, is_update = await self.brain.ingest_event(understanding, story)
        # A high-frequency provider/market update is still recorded as an
        # event update and canonical feature. Repeating the full prediction
        # fan-out for every identical cluster inflated the production brain
        # by tens of thousands of NO_TRADE records in a few hours.
        high_frequency = bool(story.get('_structured_observation')) or urlsplit(str(story.get('source_url') or '')).scheme == 'market'
        if high_frequency and not is_new and not is_replay:
            last_prediction = await self.brain.latest_prediction_at(event_id)
            min_gap = 300.0 if mode in {'LIVE', 'SHADOW'} else 3600.0
            if last_prediction is not None and time.time() - last_prediction < min_gap:
                self.metrics.inc('brain_repeated_update_feature_only')
                return event_id, []
        embedding=story.get('_semantic_embedding')
        if isinstance(embedding,list) and story.get('_semantic_embedding_hash'):
            await self.brain.record_semantic_memory(
                event_id,str(story['_semantic_embedding_hash']),str(story.get('_semantic_embedding_model') or self.semantics.embedding_model),embedding,
                story.get('_openai_semantics') if isinstance(story.get('_openai_semantics'),dict)
                else story.get('_local_semantics') if isinstance(story.get('_local_semantics'),dict) else {},
                "\n".join((str(story.get('english_title') or story.get('title') or ''),str(story.get('english_summary') or story.get('summary') or ''))),
            )
        semantic_context=story.get('_v57_semantic_context') or story.get('_v56_semantic_context') or {}
        if semantic_context:
            with connect(self.brain.path,self.settings) as con:
                con.execute("INSERT OR REPLACE INTO event_semantic_context(event_id,context_json,provenance_json,created_at) VALUES(?,?,?,?)",
                    (event_id,json.dumps(semantic_context,sort_keys=True,default=str),json.dumps(semantic_context.get('context_provenance') or [],sort_keys=True,default=str),time.time()))
                event_cols={str(r['name']) for r in con.execute('PRAGMA table_info(events)').fetchall()}
                if {'raw_surprise','standardized_surprise','surprise_unit','surprise_family'}<=event_cols:
                    con.execute("UPDATE events SET raw_surprise=?,standardized_surprise=?,surprise_unit=?,surprise_family=? WHERE id=?",
                        (semantic_context.get('raw_surprise'),semantic_context.get('standardized_surprise'),str(semantic_context.get('surprise_unit') or 'native'),str(semantic_context.get('surprise_family') or understanding.event_type or 'GENERAL'),event_id))
        if refinement_only:
            await self.brain.audit(
                "intelligence", "FASTLANE_REFINEMENT", "story", str(story["id"]),
                {"event_id": event_id, "priority": understanding.priority},
            )
            return event_id, []
        self.metrics.inc("brain_event_new" if is_new else "brain_event_update" if is_update else "brain_event_replay")
        event = await self.brain.event_context(event_id)
        if not event:
            raise RuntimeError(f"event disappeared after ingest id={event_id}")
        # Event updates retain old mappings for audit. Inference and Telegram must
        # use only the assets resolved from the current story, otherwise a prior
        # false alias match can leak into a later update of the same cluster.
        current_asset_ids={str(item.get('asset_id')) for item in understanding.assets if item.get('asset_id')}
        event['assets']=[item for item in event.get('assets',[]) if str(item.get('asset_id')) in current_asset_ids]
        news_fresh = live_news_freshness_ok(story)
        live_qualified = not is_replay and mode == 'LIVE' and news_fresh and await asyncio.to_thread(self._live_artifact_gate_sync)
        if not news_fresh and not is_replay:
            self.metrics.inc('signal_stale_or_unverified_news_suppressed')
        expected_horizons=tuple(int(x) for x in understanding.expected_horizons)
        # Observation hours collect all response targets but require no multi-horizon
        # prediction fan-out. One representative internal snapshot protects the hot
        # acquisition path; SHADOW/LIVE execute the complete horizon stream.
        runtime_horizons=self._bounded_horizons(expected_horizons,mode=='OBSERVATION',horizon_budget)
        signal_ids: list[int] = []
        telegram_signal_candidates: list[dict[str,Any]] = []
        scoped_assets=[item for item in event["assets"] if self._signal_asset_allowed(item)]
        if len(scoped_assets)!=len(event["assets"]):self.metrics.inc('signal_asset_scope_suppressed',len(event["assets"])-len(scoped_assets))
        ranked_assets=sorted(scoped_assets,key=lambda item:(int(item.get('impact_order') or 99),-float(item.get('mapping_confidence') or 0),str(item.get('asset_id'))))[:asset_budget]
        # Curated structured releases (macro prints, filings, positioning and
        # official records) have already passed a provider-specific quality
        # gate.  Their normalized importance can be deliberately conservative,
        # so keep one bounded prediction path for them.  The materiality floor
        # applies to the much noisier general-news stream.
        if (
            float(event.get('importance') or 0) < self.min_prediction_importance
            and not bool(story.get('_structured_observation'))
        ):
            ranked_assets=[]
            self.metrics.inc('brain_low_importance_prediction_filtered')
        for asset in ranked_assets:
            if stop_event is not None and stop_event.is_set():
                break
            candidates=[]
            if not is_replay:await asyncio.to_thread(self.watchlist.activate,str(asset['asset_id']),f"event:{event_id}:{event['event_type']}",event_id)
            regime = await self.brain.latest_regime(str(asset["asset_id"]))
            for horizon in runtime_horizons:
                if stop_event is not None and stop_event.is_set():
                    break
                outcomes = await self.brain.historical_outcomes(str(event["event_type"]), str(asset["asset_id"]), int(horizon))
                feature = await self.features.build(event, asset, int(horizon), replay=bool(story.get('_replay')))
                routed_experts=tuple(str(x) for x in (impact.get('relevant_experts') or ()))
                votes, decision, inference = await asyncio.to_thread(
                    self.inference.infer, event, asset, int(horizon), feature, mode,
                    outcomes, True, expected_horizons, routed_experts or None,
                )
                policy = await asyncio.to_thread(self.policy.decide, feature, decision.probability, inference.get('decision_models'))
                decision = dataclasses.replace(decision, entry_logic=f"{policy['entry_style']}: {decision.entry_logic}", exit_logic=f"{decision.exit_logic} policy={policy['exit_probability'] if policy['exit_probability'] is not None else 'bootstrap'}")
                inference['policy'] = policy
                inference['live_qualified'] = live_qualified
                if is_replay:
                    signal_id=-1
                else:
                    _prediction_id, signal_id = await self.brain.record_decision(event_id, votes, decision, int(event.get("update_count") or 1), inference, feature)
                    signal_ids.append(signal_id)
                candidates.append({'votes':votes,'decision':decision,'inference':inference,'policy':policy,'feature':feature,'signal_id':signal_id})
            consolidated=self.consolidator.consolidate(candidates)
            if consolidated and not is_replay:
                selected=consolidated.item;decision=selected['decision'];policy=selected['policy'];inference=selected['inference'];feature=selected['feature'];signal_id=selected['signal_id'];votes=selected['votes']
                reference=await asyncio.to_thread(self._reference_quote_sync,str(asset['asset_id']),float(feature['prediction_time']))
                provisional_action=self._provisional_action(decision,feature,mode,news_fresh,reference,event,asset)
                exits=await asyncio.to_thread(self.active_signals.reevaluate,str(asset['asset_id']),float(decision.probability),float(decision.risk_score),policy.get('exit_probability'),{'event_id':event_id,'feature_id':feature['feature_id'],'controlling_artifact':inference.get('controlling_artifact_id')})
                if live_qualified and self.settings.telegram_enabled:
                    for exit_action in exits:
                        await self.telegram.produce({'signal_id':exit_action['signal_id'],'event_id':event_id,'asset_id':asset['asset_id'],'signal_state':'UPDATED','state_changed':True,'live_gate':True,'action':'EXIT','summary':exit_action['reason']})
                    for warning in self._risk_warning_payloads(event,asset,votes):
                        await self.telegram.produce({'live_gate':True,'event_id':event_id,'asset_id':asset['asset_id'],'pump_probability':warning['confidence'] if warning['kind']=='pump_warning' else 0.0,'dump_probability':warning['confidence'] if warning['kind']=='dump_warning' else 0.0,'summary':warning.get('event_title')})
                if ((live_qualified and decision.action not in {"NO_TRADE", "WAIT"}) or provisional_action) and self.settings.telegram_enabled:
                    payload = self._signal_payload(event, asset, decision, signal_id)
                    from .barrier_evaluation import SignalEvaluator
                    if not hasattr(self,'_barrier_policy'):self._barrier_policy=SignalEvaluator(self.settings)
                    evaluator=self._barrier_policy
                    payload['adaptive_evaluation']=await asyncio.to_thread(evaluator.policy,str(asset['asset_id']),str(event['event_type']),int(decision.horizon_seconds),1 if decision.probability>.5 else -1,feature.get('values',{}))
                    market_url=self._market_verification_url(asset);sources=list(payload.get('sources') or [])
                    if market_url:sources.append({'source_name':'Live market verification','source_url':market_url,'title':str(asset.get('market_symbol') or asset['asset_id']),'summary':'Independent market page for manual price and liquidity verification.','observed_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())})
                    signal_direction=provisional_action['direction'] if provisional_action else ('UP' if decision.probability>.5 else 'DOWN')
                    payload.update({'market':asset.get('market_symbol'),'direction':signal_direction,
                        'signal_type':policy.get('entry_style'),'quality_score':feature.get('quality'),'current_regime':(regime or {}).get('regime','UNKNOWN'),
                        'main_drivers':self._human_signal_drivers(event,asset,feature),'model_audit_reasons':list(decision.reasons)[:8],
                        'champion_model':inference.get('artifact_ids',[])[-1] if inference.get('artifact_ids') else 'BOOTSTRAP_FALLBACK',
                        'suppressed_horizons':consolidated.suppressed,'consolidation_score':consolidated.score,'timestamp':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'sources':sources})
                    if reference:
                        level=max(.1,min(5.0,float(os.getenv('PROVISIONAL_REFERENCE_MOVE_PCT','.5'))));direction=1 if signal_direction=='UP' else -1;price=float(reference['price'])
                        payload.update({'reference_price':price,'reference_price_provider':reference['provider'],'reference_price_observed_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(float(reference['observed_at']))),'reference_price_age_seconds':reference['age_seconds'],'reference_entry':price,'reference_target':price*(1+direction*level/100),'reference_invalidation':price*(1-direction*level/100),'reference_move_pct':level})
                    if provisional_action:
                        payload.update({'action':provisional_action['action'],'provisional_basis':provisional_action['basis'],
                            'model_probability_unqualified':float(decision.probability),'provisional_gate':True,'live_gate':False,
                            'qualification_status':'GLOBAL_GATE_FAILED','risk_disclaimer':'PROVISIONAL RESEARCH ONLY: direction is a deterministic event mechanism, not a qualified model forecast. The 48-hour warmup passed, but calibration/drift qualification failed. Verify every linked source and live price before action.'})
                    else:payload.update({'live_gate':True,'provisional_gate':False,'qualification_status':'FINAL_VALIDATED'})
                    telegram_signal_candidates.append({'payload':payload,'score':consolidated.score})
                else:
                    self.metrics.inc(f"signal_{mode.lower()}_not_delivered")
                    if news_fresh and self.settings.telegram_enabled and understanding.priority in {'P0','P1'} and feature.get('quality',0)>=.65:
                        await self.telegram.produce({'event_id':event_id,'asset_id':asset['asset_id'],'materiality':10 if understanding.priority=='P0' else 8,'fundamental_only':True,'summary':event['summary'],'quality_score':feature.get('quality')})
        # Rank every asset/horizon candidate globally for this event before any
        # per-event Telegram cap is applied. Internal predictions remain intact.
        max_event=max(1,int(os.getenv('TELEGRAM_MAX_SIGNALS_PER_EVENT','2')))
        for candidate in sorted(telegram_signal_candidates,key=lambda item:float(item['score']),reverse=True)[:max_event]:
            produced=await self.telegram.produce(candidate['payload'])
            if produced:self.metrics.inc("intelligence_telegram_enqueued")
        await self.brain.audit("intelligence", "REPLAY_STORY_PROCESSED" if is_replay else "FASTLANE_STORY_PROCESSED" if fast_lane else "STORY_PROCESSED", "story", str(story["id"]), {"event_id": event_id, "mode": mode, "signals": signal_ids,"production_decisions_written":not is_replay,"priority":understanding.priority,"impact_score":impact.get('score'),"asset_budget":asset_budget,"horizon_budget":horizon_budget})
        return event_id, signal_ids

    async def _drain_fastlane(
        self, stop_event: asyncio.Event, limit: int | None = None,
    ) -> int:
        candidates = await self.control.fastlane_candidates(limit or self.fastlane_batch)
        processed = 0
        for story in candidates:
            if stop_event.is_set():
                break
            try:
                event_id, _signals = await self.process_story(
                    story, stop_event, fast_lane=True,
                )
                if stop_event.is_set() or not event_id:
                    break
                change_id = int(story["_change_id"])
                route = story.get("_impact_route") or {}
                await self.control.record_fastlane_receipt(
                    change_id, int(story["id"]),
                    str(route.get("priority") or story.get("_impact_priority") or "P1"),
                    int(route.get("score") or story.get("_impact_score") or 0),
                    event_id,
                )
                previous = int(await self.brain.get_meta("fast_lane_cursor", "0"))
                await self.brain.set_meta("fast_lane_cursor", str(max(previous, change_id)))
                processed += 1
                self.metrics.inc("brain_fastlane_processed")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.metrics.inc("brain_fastlane_error")
                logger.exception(
                    "fast-lane processing failed story_id=%s change_id=%s",
                    story.get("id"), story.get("_change_id"),
                )
                break
        if processed:
            self.metrics.set("brain_fastlane_receipts", await self.control.fastlane_summary())
        return processed

    async def process_observation(
        self,
        observation,
        stop_event: asyncio.Event | None = None,
    ) -> tuple[int, list[int]]:
        change_id = int(hashlib.sha256(observation.observation_id.encode()).hexdigest()[:14], 16)
        payload = observation.metadata.get('payload') or {}
        story = {'id':-change_id,'_change_id':change_id,'_change_type':'INITIAL','_replay':observation.replay,'_structured_observation':True,
                 '_semantic_route':observation.metadata.get('semantic_route') or {},
                 'fingerprint':observation.observation_id,'title':observation.observation_type.replace('_',' '),
                 'summary':json.dumps(payload,ensure_ascii=False,default=str)[:3000],'category':observation.observation_type,
                 'materiality':max(3,min(10,int(observation.quality*10))),'source_name':observation.source_id,
                 'source_url':observation.raw_reference,'published_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(observation.event_time)),
                 'first_observed_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(observation.available_time)),
                 'official':float(observation.source_quality)>=.9}
        return await self.process_story(story, stop_event)

    async def run(self, stop_event: asyncio.Event) -> None:
        cursor = int(await self.brain.get_meta("story_change_cursor", "0"))
        observation_cursor = int(await self.brain.get_meta("structured_observation_cursor", "0"))
        logger.info("intelligence service started cursor=%s", cursor)
        try:
            while not stop_event.is_set():
                await self.warmup.tick()
                # This lane is queried independently of the FIFO cursor. A P0/P1
                # receipt is committed only after successful processing.
                await self._drain_fastlane(stop_event)
                batch_end_cursor, observations = await self.observations.drain(
                    observation_cursor,
                    self.structured_batch,
                )

                if not observations and batch_end_cursor != observation_cursor:
                    observation_cursor = batch_end_cursor
                    await self.brain.set_meta(
                        'structured_observation_cursor',
                        str(observation_cursor),
                    )

                for observation in observations:
                    if stop_event.is_set():
                        break

                    snapshot_id = int(
                        observation.metadata.get(
                            '_structured_snapshot_id'
                        ) or 0
                    )

                    family = str(
                        observation.observation_family or 'GENERAL'
                    ).upper()

                    # GENERAL high-frequency structured rows remain fully persisted
                    # and available to FeatureStore, ActiveSignalMonitor and learning,
                    # but do not create a complete event/prediction fan-out.
                    should_trigger = (
                        observation.observation_type.upper() not in FEATURE_ONLY_OBSERVATION_TYPES
                        and (family != 'GENERAL' or self.structured_general_triggers)
                    )

                    if should_trigger:
                        try:
                            await self.process_observation(
                                observation,
                                stop_event,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            self.metrics.inc('brain_observation_error')
                            logger.exception(
                                'canonical observation processing failed id=%s',
                                observation.observation_id,
                            )
                    else:
                        self.metrics.inc(
                            'brain_observation_feature_only'
                        )

                    # One structured DB row can expand into many canonical rows.
                    # Commit the source cursor only after the final child.
                    last_child = (
                        int(
                            observation.metadata.get(
                                'batch_index'
                            ) or 0
                        )
                        >=
                        int(
                            observation.metadata.get(
                                'batch_size'
                            ) or 1
                        ) - 1
                    )

                    if (
                        not stop_event.is_set()
                        and snapshot_id
                        and last_child
                    ):
                        observation_cursor = snapshot_id
                        await self.brain.set_meta(
                            'structured_observation_cursor',
                            str(observation_cursor),
                        )

                # A batch can end with raw-only snapshots. Commit their
                # cursor once every returned observation has been handled.
                if not stop_event.is_set() and batch_end_cursor > observation_cursor:
                    observation_cursor = batch_end_cursor
                    await self.brain.set_meta(
                        'structured_observation_cursor',
                        str(observation_cursor),
                    )

                if stop_event.is_set():
                    break

                stories = await self.control.story_changes_after(
                    cursor,
                    100,
                )
                if not stories:
                    self.metrics.set("brain", await self.brain.summary())
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=.25)
                    except asyncio.TimeoutError:
                        continue
                    break
                for story in stories:
                    if stop_event.is_set():
                        break
                    try:
                        receipt = await self.control.fastlane_receipt(int(story["_change_id"]))
                        if receipt:
                            # Fast output already exists. The FIFO lane performs
                            # only delayed semantic/analogue refinement, never a
                            # duplicate prediction fan-out.
                            if receipt.get("refined_at") is None:
                                await self.process_story(
                                    story, stop_event, refinement_only=True,
                                )
                                if not stop_event.is_set():
                                    await self.control.mark_fastlane_refined(
                                        int(story["_change_id"])
                                    )
                            self.metrics.inc("brain_fastlane_duplicate_skipped")
                        else:
                            await self.process_story(
                                story,
                                stop_event,
                            )
                        if stop_event.is_set():
                            break
                        cursor = int(story["_change_id"])
                        await self.brain.set_meta("story_change_cursor", str(cursor))
                        # Check between normal events so an arriving critical
                        # story cannot wait for the remainder of a FIFO batch.
                        await self._drain_fastlane(stop_event, limit=1)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self.metrics.inc("brain_story_error")
                        logger.exception("intelligence story processing failed story_id=%s", story.get("id"))
                        await asyncio.sleep(.5)
                        break
                self.metrics.set("brain_story_cursor", cursor)
        finally:
            await self.semantics.close()
