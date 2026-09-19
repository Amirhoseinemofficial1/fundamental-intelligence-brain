from __future__ import annotations

import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

from .timeutil import iso_now

_SECRET_PATTERNS = [
    re.compile(r"(bot)[0-9]{6,}:[A-Za-z0-9_-]{20,}", re.I),
    re.compile(r"\b[0-9]{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{16,})\b", re.I),
    re.compile(r"([?&](?:api[_-]?key|apikey|app[_-]?id|token|access[_-]?token|client[_-]?secret|subscription-key|secret|key)=)[^&\s]+", re.I),
    re.compile(r"((?:authorization|x-api-key|subscription-key)\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^,;\s]+", re.I),
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_=.\-]{12,}", re.I),
]
_SECRET_KEYS = {"token", "api_key", "apikey", "secret", "password", "authorization", "telegram_bot_token"}


def redact_text(value: str) -> str:
    out = value
    for pat in _SECRET_PATTERNS:
        out = pat.sub("***REDACTED***", out)
    return out


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for k, v in obj.items():
            if k.lower() in _SECRET_KEYS or any(x in k.lower() for x in ("token", "secret", "password", "api_key")):
                result[k] = "***" if v else v
            else:
                result[k] = redact_obj(v)
        return result
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(x) for x in obj)
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


class JsonLineHandler(logging.Handler):
    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max(1_048_576, int(os.getenv("LOG_MAX_BYTES", "8388608")))
        self._fh = self.path.open("a", encoding="utf-8", buffering=1)

    def _rotate_if_needed(self) -> None:
        if self._fh.tell() < self.max_bytes:
            return
        self._fh.flush()
        self._fh.close()
        backup = self.path.with_suffix(self.path.suffix + ".1")
        if backup.exists():
            backup.unlink()
        if self.path.exists():
            os.replace(self.path, backup)
        self._fh = self.path.open("a", encoding="utf-8", buffering=1)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._rotate_if_needed()
            payload = {
                "ts": iso_now(),
                "level": record.levelname,
                "logger": record.name,
                "message": redact_text(record.getMessage()),
                "pid": os.getpid(),
            }
            if record.exc_info:
                payload["exception"] = redact_text("".join(traceback.format_exception(*record.exc_info)))
            self._fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
        finally:
            super().close()


def configure_logging(service: str, log_dir: Path, level: str = "INFO") -> logging.Logger:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if os.getenv("V52_CONSOLE_LOGGING", "1").strip().lower() in {"1", "true", "yes", "on"}:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(console)
    root.addHandler(JsonLineHandler(log_dir / f"{service}.jsonl"))
    return logging.getLogger(service)


def log_exception(logger: logging.Logger, message: str, **fields: Any) -> None:
    logger.exception("%s fields=%s", message, json.dumps(redact_obj(fields), ensure_ascii=False, default=str))
