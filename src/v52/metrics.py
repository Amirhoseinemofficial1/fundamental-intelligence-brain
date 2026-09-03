from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from .timeutil import iso_now


class Metrics:
    def __init__(self, service: str, state_dir: Path):
        self.service = service
        self.state_dir = state_dir / service
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, Any] = {}
        self.started_at = time.time()

    def inc(self, key: str, amount: float = 1.0) -> None:
        with self._lock:
            self._counters[key] += amount

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._gauges[key] = value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": "52.0-service-status",
                "service": self.service,
                "pid": os.getpid(),
                "at": iso_now(),
                "uptime_seconds": max(0.0, time.time() - self.started_at),
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
            }

    def write_status(self, extra: dict[str, Any] | None = None) -> Path:
        payload = self.snapshot()
        if extra:
            payload["extra"] = extra
        path = self.state_dir / "status.json"
        with self._write_lock:
            tmp = self.state_dir / f"status.{os.getpid()}.{threading.get_ident()}.tmp"
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, path)
        return path
