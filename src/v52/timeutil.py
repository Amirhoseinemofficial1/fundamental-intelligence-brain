from __future__ import annotations

import datetime as dt
import email.utils
import time
from typing import Any

UTC = dt.timezone.utc


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def unix_now() -> float:
    return time.time()


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def to_iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
    if isinstance(value, (int, float)):
        n = float(value)
        if n > 1e14:
            n /= 1_000_000.0
        elif n > 1e11:
            n /= 1_000.0
        try:
            return dt.datetime.fromtimestamp(n, UTC)
        except (ValueError, OSError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return parse_datetime(int(s))
    normalized = s.replace("Z", "+00:00")
    try:
        x = dt.datetime.fromisoformat(normalized)
    except ValueError:
        x = None
    if x is not None:
        if x.tzinfo is None:
            x = x.replace(tzinfo=UTC)
        return x.astimezone(UTC)
    try:
        x = email.utils.parsedate_to_datetime(s)
        if x is not None:
            if x.tzinfo is None:
                x = x.replace(tzinfo=UTC)
            return x.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None
    return None


def age_seconds(published: dt.datetime | None, observed: dt.datetime | None = None) -> float | None:
    if published is None:
        return None
    observed = observed or utc_now()
    return max(0.0, (observed - published).total_seconds())
