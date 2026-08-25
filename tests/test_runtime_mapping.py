from __future__ import annotations

import asyncio

from v52.constants import DEFAULT_SERVICES, MARKET_SERVICES
from v52.metrics import Metrics
from v52.runtime.service import build_runner


def test_every_default_service_has_a_runtime_mapping(settings) -> None:
    async def run() -> None:
        created = []
        try:
            for service in DEFAULT_SERVICES:
                metrics = Metrics(f"mapping_{service}", settings.state_dir)
                runner = build_runner(service, settings, metrics)
                assert runner is not None, service
                created.append(runner)
        finally:
            for runner in created:
                http = getattr(runner, "http", None)
                if http is not None:
                    await http.close()
                session = getattr(runner, "session", None)
                if session is not None and not session.closed:
                    await session.close()
    asyncio.run(run())


def test_market_response_is_downstream_not_exchange_runtime() -> None:
    assert "market_response" not in MARKET_SERVICES
