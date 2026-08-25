from __future__ import annotations

import inspect

from v52.news.collector import AdaptiveNewsCollector
from v52.runtime.supervisor import Supervisor


def test_news_collector_shutdown_never_waits_on_abandoned_poll_queue() -> None:
    src = inspect.getsource(AdaptiveNewsCollector.run)
    assert "await self.queue.join()" not in src
    assert "collector_shutdown_poll_intents_discarded" in src
    assert "collector_shutdown_inflight_cancel" in src


def test_supervisor_waits_for_run_identity_heartbeats_before_timer() -> None:
    src = inspect.getsource(Supervisor.run)
    ready = src.index("await self.wait_for_startup_ready()")
    timer = src.index("if self.run_seconds > 0")
    assert ready < timer
