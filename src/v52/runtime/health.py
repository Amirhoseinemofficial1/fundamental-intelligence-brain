from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


def proc_start_ticks(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        return int(fields[21])
    except (OSError, ValueError, IndexError):
        return None


def _kib_value(text: str) -> int | None:
    try:
        first = text.strip().split()[0]
        return int(first) * 1024
    except (ValueError, IndexError):
        return None


def proc_stats(pid: int | None = None) -> dict[str, Any]:
    pid = pid or os.getpid()
    out: dict[str, Any] = {"pid": pid}
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        for key in ("VmRSS", "VmHWM", "Threads", "FDSize"):
            m = re.search(rf"^{key}:\s+([^\n]+)", status, re.M)
            if not m:
                continue
            value = m.group(1).strip()
            out[key] = value
            if key in {"VmRSS", "VmHWM"}:
                parsed = _kib_value(value)
                if parsed is not None:
                    out[f"{key}_bytes"] = parsed
            elif key in {"Threads", "FDSize"}:
                try:
                    out[f"{key}_count"] = int(value.split()[0])
                except (ValueError, IndexError):
                    out[f"{key}_parse_error"] = value
        out["fd_count"] = len(list(Path(f"/proc/{pid}/fd").iterdir()))
        out["start_ticks"] = proc_start_ticks(pid)
    except OSError as exc:
        out["proc_error"] = f"{type(exc).__name__}:{exc}"
    return out


def cpu_psi() -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        text = Path("/proc/pressure/cpu").read_text()
        for line in text.splitlines():
            parts = line.split()
            if not parts:
                continue
            prefix = parts[0]
            bucket: dict[str, float] = {}
            for token in parts[1:]:
                if "=" not in token:
                    continue
                k, v = token.split("=", 1)
                if k.startswith("avg"):
                    bucket[k] = float(v)
            if bucket:
                out[prefix] = bucket
    except (OSError, ValueError) as exc:
        out["error"] = f"{type(exc).__name__}:{exc}"
    return out


def memory_info() -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            if k in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                try:
                    out[k] = int(v.strip().split()[0]) * 1024
                except (ValueError, IndexError):
                    out[f"{k}_parse_error"] = v.strip()
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}:{exc}"
    return out
