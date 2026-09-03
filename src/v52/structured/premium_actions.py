from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from typing import Any

import aiohttp

from ..config import Settings
from ..loggingx import redact_text
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..storage.structured import StructuredStore

logger = logging.getLogger(__name__)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return max(minimum, default)


def _records(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if not isinstance(obj, dict):
        return []
    for key in ("data", "results", "result", "items", "trades", "alerts", "transactions", "records"):
        val = obj.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            nested = _records(val)
            if nested:
                return nested
    return [obj] if obj else []


class PremiumActionsService:
    """Authorized V52-native Wall-Street/whale provider plane.

    This restores the V33/V50 Unusual Whales and Whale Alert contracts without importing the
    legacy runtime. Missing credentials disable only the corresponding provider. Every received
    record is deduplicated durably by StructuredStore payload hash; no in-memory-only seen set is
    required for correctness.
    """

    UW_ENDPOINTS: tuple[dict[str, Any], ...] = (
        {"path":"/api/socket/flow_alerts","record_type":"UW_OPTION_FLOW_ALERT","lane":"WALL_STREET_FLOW","poll_env":"UNUSUAL_WHALES_FLOW_POLL_SECONDS","poll":2.0},
        {"path":"/api/darkpool/recent","record_type":"UW_DARKPOOL_PRINT","lane":"WALL_STREET_FLOW","poll_env":"UNUSUAL_WHALES_DARKPOOL_POLL_SECONDS","poll":3.0},
        {"path":"/api/insider/transactions","record_type":"UW_INSIDER_DISCLOSURE","lane":"WALL_STREET_ACTIONS","poll_env":"UNUSUAL_WHALES_INSIDER_POLL_SECONDS","poll":30.0},
        {"path":"/api/congress/congress-trader","record_type":"UW_CONGRESS_DISCLOSURE","lane":"POLITICAL_MONEY","poll_env":"UNUSUAL_WHALES_CONGRESS_POLL_SECONDS","poll":60.0},
        {"path":"/api/crypto/whale-transactions","record_type":"UW_CRYPTO_WHALE_TRANSACTION","lane":"WHALE_FLOW","poll_env":"UNUSUAL_WHALES_CRYPTO_POLL_SECONDS","poll":4.0},
        {"path":"/api/crypto/whales/recent","record_type":"UW_CRYPTO_WHALE_RECENT","lane":"WHALE_FLOW","poll_env":"UNUSUAL_WHALES_CRYPTO_POLL_SECONDS","poll":4.0},
    )

    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings=settings; self.metrics=metrics; self.store=StructuredStore(settings)
        self.http=ResilientHttpClient(settings,name="premium-actions",concurrency=8,per_host_limit=3,retries=2,total_timeout=20.0,max_body_bytes=20_000_000)
        self.uw_token=os.getenv("UNUSUAL_WHALES_TOKEN","").strip()
        self.uw_base=os.getenv("UNUSUAL_WHALES_BASE","https://api.unusualwhales.com").rstrip("/")
        self.wa_key=os.getenv("WHALE_ALERT_API_KEY","").strip()
        self.wa_ws=os.getenv("WHALE_ALERT_WS","wss://leviathan.whale-alert.io/ws").strip()

    def _uw_params(self, spec: dict[str, Any]) -> dict[str, Any]:
        rt=str(spec["record_type"])
        if rt=="UW_DARKPOOL_PRINT": return {"limit":"200","min_premium":str(_float("UNUSUAL_WHALES_MIN_DARKPOOL_PREMIUM",250000.0))}
        if rt=="UW_CONGRESS_DISCLOSURE": return {"limit":"200"}
        if rt in {"UW_INSIDER_DISCLOSURE","UW_CRYPTO_WHALE_TRANSACTION","UW_CRYPTO_WHALE_RECENT"}: return {"limit":"500"}
        return {}

    async def _uw_loop(self, spec: dict[str, Any], stop_event: asyncio.Event) -> None:
        provider="unusual_whales:"+str(spec["record_type"]).lower(); url=self.uw_base+str(spec["path"])
        headers={"Authorization":f"Bearer {self.uw_token}","Accept":"application/json"}
        poll=_float(str(spec["poll_env"]),float(spec["poll"]),1.0)
        while not stop_event.is_set():
            result=await self.http.get(url,headers=headers,params=self._uw_params(spec),retries=2)
            if result.ok:
                try:
                    obj=json.loads(result.body); rows=_records(obj)
                    changed=await self.store.insert_snapshots(provider,str(spec["lane"]),str(spec["record_type"]),rows[:5000])
                    await self.store.status(provider,"ok",result.status); self.metrics.inc("premium_uw_seen",len(rows)); self.metrics.inc("premium_uw_changed",changed)
                except Exception as exc:
                    await self.store.status(provider,"parse_error",result.status,error=redact_text(f"{type(exc).__name__}:{exc}")); self.metrics.inc("premium_uw_parse_error")
            else:
                await self.store.status(provider,result.classification,result.status,result.error,result.classification=="rate_limited"); self.metrics.inc(f"premium_uw_{result.classification}")
            wait=max(poll,result.retry_after or 0.0)
            try: await asyncio.wait_for(stop_event.wait(),timeout=wait+random.random()*0.3)
            except asyncio.TimeoutError: continue

    def _wa_subscription(self) -> dict[str, Any]:
        sub={"type":"subscribe_alerts","id":"v52-global-whale-alert","min_value_usd":_float("WHALE_ALERT_MIN_VALUE_USD",1_000_000.0,100_000.0)}
        for env,key,upper in (("WHALE_ALERT_BLOCKCHAINS","blockchains",False),("WHALE_ALERT_SYMBOLS","symbols",True),("WHALE_ALERT_TX_TYPES","tx_types",False)):
            vals=[x.strip().upper() if upper else x.strip() for x in os.getenv(env,"").split(",") if x.strip()]
            if vals: sub[key]=vals
        return sub

    async def _whale_alert_loop(self, stop_event: asyncio.Event) -> None:
        provider="whale_alert"; backoff=1.0
        while not stop_event.is_set():
            # API key remains in the wire URL as required by this provider contract; logging redaction
            # removes query secrets and we never persist this authenticated URL.
            url=self.wa_ws+("&" if "?" in self.wa_ws else "?")+"api_key="+self.wa_key
            try:
                timeout=aiohttp.ClientTimeout(total=None,connect=15,sock_read=90)
                async with self.http.session.ws_connect(url,heartbeat=25,receive_timeout=90,autoping=True,max_msg_size=8_000_000,timeout=timeout) as ws:
                    await ws.send_json(self._wa_subscription())
                    if _bool("WHALE_ALERT_SOCIALS",True): await ws.send_json({"type":"subscribe_socials","id":"v52-whale-alert-socials"})
                    await self.store.status(provider,"ok",101); self.metrics.inc("premium_whale_alert_connected"); backoff=1.0
                    async for msg in ws:
                        if stop_event.is_set(): return
                        if msg.type != aiohttp.WSMsgType.TEXT: continue
                        try: obj=json.loads(msg.data)
                        except Exception as exc:
                            self.metrics.inc("premium_whale_alert_parse_error"); logger.warning("Whale Alert JSON parse error=%s",redact_text(str(exc))); continue
                        if not isinstance(obj,dict): continue
                        typ=str(obj.get("type") or "").lower()
                        rtype="WHALE_ALERT_SOCIAL" if typ in {"social","socials","social_alert"} else "WHALE_ALERT_TRANSACTION"
                        lane="SOCIAL_INTELLIGENCE" if rtype.endswith("SOCIAL") else "WHALE_FLOW"
                        changed=await self.store.insert_snapshot(provider,lane,rtype,obj)
                        self.metrics.inc("premium_whale_alert_changed",int(changed))
            except asyncio.CancelledError: raise
            except Exception as exc:
                err=redact_text(f"{type(exc).__name__}:{exc}")
                await self.store.status(provider,"stream_error",error=err); self.metrics.inc("premium_whale_alert_reconnect"); logger.warning("Whale Alert reconnect error=%s",err)
                try: await asyncio.wait_for(stop_event.wait(),timeout=min(90.0,backoff)+random.random())
                except asyncio.TimeoutError: backoff=min(90.0,backoff*2.0)

    async def run(self, stop_event: asyncio.Event) -> None:
        if not _bool("PREMIUM_ACTIONS_ENABLED",True):
            await self.store.status("premium_actions","disabled_config",error="PREMIUM_ACTIONS_ENABLED=0")
            await self.http.close(); await stop_event.wait(); return
        tasks=[]
        if self.uw_token:
            tasks.extend(asyncio.create_task(self._uw_loop(spec,stop_event),name=f"premium:uw:{spec['record_type']}") for spec in self.UW_ENDPOINTS)
        else:
            await self.store.status("unusual_whales","disabled_missing_key",error="UNUSUAL_WHALES_TOKEN not configured")
        if self.wa_key:
            tasks.append(asyncio.create_task(self._whale_alert_loop(stop_event),name="premium:whale-alert"))
        else:
            await self.store.status("whale_alert","disabled_missing_key",error="WHALE_ALERT_API_KEY not configured")
        self.metrics.set("premium_active_tasks",len(tasks))
        try: await stop_event.wait()
        finally:
            for t in tasks: t.cancel()
            results=await asyncio.gather(*tasks,return_exceptions=True)
            for x in results:
                if isinstance(x,BaseException) and not isinstance(x,asyncio.CancelledError):
                    self.metrics.inc("premium_shutdown_error"); logger.error("premium task shutdown error=%s",redact_text(str(x)))
            await self.http.close()
