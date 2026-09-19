"""Single-process database bootstrap before the supervisor fans out.

Every service still verifies its own schema, but systemd invokes this module
first so fresh databases and migrations are complete before 29 child processes
open shared SQLite files.
"""
from __future__ import annotations

import json

from ..config import Settings
from ..constants import MARKET_SERVICES
from ..intelligence.barrier_evaluation import SignalEvaluator
from ..intelligence.store import BrainStore
from ..storage.control import ControlStore
from ..storage.market import MarketSemanticStore
from ..storage.news import NewsStore
from ..storage.structured import StructuredStore


def bootstrap() -> dict[str, object]:
    settings = Settings.from_env()
    settings.ensure_directories()
    NewsStore(settings)
    ControlStore(settings)
    StructuredStore(settings)
    BrainStore(settings)
    SignalEvaluator(settings)
    markets = [name for name in settings.supervisor_services if name in MARKET_SERVICES]
    for service in markets:
        MarketSemanticStore(settings, service)
    return {
        "ok": True,
        "data_dir": str(settings.data_dir),
        "shared_databases": 4,
        "market_databases": markets,
    }


def main() -> int:
    print(json.dumps(bootstrap(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
