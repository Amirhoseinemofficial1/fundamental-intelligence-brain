from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any

from ..config import Settings
from ..metrics import Metrics
from ..net.http import ResilientHttpClient
from ..net.rate import AsyncRateLimiter
from ..storage.control import ControlStore
from .telegram import render_batch, full_signal_report
import aiohttp

logger = logging.getLogger(__name__)


class AlertService:
    def __init__(self, settings: Settings, metrics: Metrics):
        self.settings = settings
        self.metrics = metrics
        self.store = ControlStore(settings)
        self.owner = f"alerts:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.http = ResilientHttpClient(settings, name="telegram", concurrency=2, per_host_limit=2, retries=1, total_timeout=30.0, max_body_bytes=1_000_000)
        self.per_chat = AsyncRateLimiter(settings.telegram_per_chat_min_gap_seconds)
        self.global_rate = AsyncRateLimiter(1.0 / settings.telegram_global_rps)
        self._cooldown_until = 0.0

    @staticmethod
    def _confirmed_message(result) -> tuple[bool, str, str]:
        if not result.ok:
            return False, "", f"{result.classification}:{result.status}:{result.error}"
        try:
            obj = json.loads(result.body)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return False, "", f"invalid_json:{type(exc).__name__}"
        message_id = str((obj.get("result") or {}).get("message_id") or "")
        if obj.get("ok") is not True or not message_id:
            return False, "", f"telegram_rejected:{obj.get('error_code') or 'missing_message_id'}"
        return True, message_id, ""

    async def _validate_target(self) -> None:
        base = f"{self.settings.telegram_api_base}/bot{self.settings.telegram_bot_token}"
        me = await self.http.get(f"{base}/getMe", retries=0)
        chat = await self.http.post(f"{base}/getChat", json_body={"chat_id": self.settings.telegram_chat_id}, retries=0)
        for name, result in (("getMe", me), ("getChat", chat)):
            try:
                obj = json.loads(result.body) if result.ok else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                obj = {}
            if not result.ok or obj.get("ok") is not True or not isinstance(obj.get("result"), dict):
                raise RuntimeError(f"Telegram startup validation failed at {name}: {result.classification}:{result.status}")
        self.metrics.set("telegram_startup_validated", 1)

    async def _send_batch(self, alerts: list[dict[str, Any]]) -> None:
        valid: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for alert in alerts:
            if alert.get("expires_at") and float(alert["expires_at"]) < time.time():
                await self.store.retry_job("alerts", int(alert["id"]), "expired_before_send", 0.0, dead=True)
                self.metrics.inc("telegram_expired_before_send")
                continue
            story = await self.store.get_story(int(alert["story_id"])) if alert.get("story_id") else {}
            if alert.get("story_id") and not story:
                await self.store.retry_job("alerts", int(alert["id"]), "story_missing", 0.0, dead=True)
                continue
            payload = json.loads(alert.get("payload_json") or "{}")
            valid.append((alert, story or {}, payload))
        if not valid:
            return
        report_only=len(valid)==1 and valid[0][2].get('kind')=='intelligence_report'
        text = '' if report_only else render_batch([(story, payload) for _, story, payload in valid])
        ids = [int(a["id"]) for a, _, _ in valid]
        attempt_id = uuid.uuid4().hex
        await self.store.mark_alert_sending(ids, attempt_id, self.settings.telegram_lease_seconds)
        cooldown = self._cooldown_until - time.monotonic()
        if cooldown > 0:
            await asyncio.sleep(cooldown)
        await self.global_rate.wait()
        await self.per_chat.wait()
        url = f"{self.settings.telegram_api_base}/bot{self.settings.telegram_bot_token}/sendMessage"
        if report_only:
            payload=valid[0][2]
            form=aiohttp.FormData()
            form.add_field('chat_id',self.settings.telegram_chat_id)
            form.add_field('caption',f"Full analysis · {payload.get('asset_id')} · signal #{payload.get('signal_id')}")
            form.add_field('document',full_signal_report(payload),filename=f"signal-{int(payload.get('signal_id') or 0)}.html",content_type='text/html')
            result=await self.http.post(url.replace('/sendMessage','/sendDocument'),data=form,retries=0)
        else:
            result = await self.http.post(
                url,
                json_body={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                retries=0,
            )
        confirmed, message_id, telegram_error = self._confirmed_message(result)
        if confirmed:
            await self.store.complete_alerts(ids, message_id)
            from ..intelligence.signal_state import SignalStateMachine
            machine=SignalStateMachine(self.settings)
            for _alert,_story,payload in valid:
                if payload.get('kind')=='intelligence_signal' and payload.get('signal_id'):
                    signal_id=int(payload['signal_id'])
                    for state in ('SENT','ENTRY_PENDING','ACTIVE'):
                        await machine.transition(signal_id,state,f'telegram_confirmed:{message_id}',{'telegram_message_id':message_id})
            self.metrics.inc("telegram_calls")
            self.metrics.inc("telegram_items_delivered_confirmed", len(ids))
            return
        attempts = max(int(x[0].get("attempts") or 1) for x in valid)
        dead = attempts >= self.settings.telegram_max_attempts or result.classification in {"blocked_or_auth", "not_found"}
        delay = result.retry_after or min(1800.0, 2.0 * (2 ** min(9, attempts)))
        if result.classification == "rate_limited":
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + delay)
        for alert, _, _ in valid:
            error = telegram_error or f"{result.classification}:{result.status}:{result.error}"
            await self.store.retry_job("alerts", int(alert["id"]), error, delay, dead)
        self.metrics.inc("telegram_send_failure")
        if result.classification == "rate_limited":
            self.metrics.inc("telegram_429")
        logger.warning("telegram batch failed items=%s classification=%s status=%s delay=%s", len(ids), result.classification, result.status, delay)

    async def run(self, stop_event: asyncio.Event) -> None:
        if not self.settings.telegram_enabled:
            logger.info("telegram disabled")
            await self.http.close()
            await stop_event.wait()
            return
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            raise RuntimeError("TELEGRAM_ENABLED=1 requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        try:
            await self._validate_target()
            while not stop_event.is_set():
                expired = await self.store.expire_alerts()
                if expired:
                    self.metrics.inc("telegram_expired", expired)
                rows = await self.store.lease_alerts(self.owner, max(10, self.settings.telegram_batch_items * 3), self.settings.telegram_lease_seconds)
                if not rows:
                    self.metrics.set("alert_queues", await self.store.queue_counts())
                    await asyncio.sleep(0.25)
                    continue
                # Highest priority first; keep API payload well below Telegram's message limit.
                rows.sort(key=lambda x: (-int(x.get("priority") or 0), int(x["id"])))
                for i in range(0, len(rows), self.settings.telegram_batch_items):
                    await self._send_batch(rows[i:i + self.settings.telegram_batch_items])
                self.metrics.set("alert_queues", await self.store.queue_counts())
        finally:
            await self.http.close()
