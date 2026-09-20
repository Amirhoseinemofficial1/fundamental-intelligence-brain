from __future__ import annotations

import argparse
import base64
import importlib
import importlib.metadata as importlib_metadata
import json
import os
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..config import Settings, load_schema
from ..constants import DEFAULT_SERVICES, MARKET_SERVICES
from ..news.registry import SourceRegistry
from ..timeutil import iso_now

REQUIRED_IMPORTS = {
    "aiohttp": "aiohttp",
    "beautifulsoup4": "bs4",
    "lxml": "lxml",
    "Brotli": "brotli",
    "pypdf": "pypdf",
    "Unidecode": "unidecode",
}
OPTIONAL_ACCELERATOR_IMPORTS = {
    "trafilatura": "trafilatura",
    "langdetect": "langdetect",
    "opencc-python-reimplemented": "opencc",
}
REQUIRED_REFERENCE_FILES = (
    "fundamental_sources.json",
    "SOURCE_UNIVERSE_V36.json",
    "FREE_API_PROVIDER_REGISTRY_V36.json",
    "V37_PROFESSIONAL_NEWSWIRE_MESH.json",
    "V37_SOURCE_PARSER_REGISTRY.json",
    "SOURCE_DISCOVERY_SEEDS_V35.json",
    "FUNDAMENTAL_TAXONOMY_V36.json",
    "PROVIDER_CAPABILITY_MATRIX_V36.json",
    "V36_LICENSE_PROVENANCE.json",
    "V34_18_LAYER_IMPLEMENTATION.json",
    "V35_30_LAYER_IMPLEMENTATION.json",
    "V36_30_FINAL_GAP_CLOSURE_IMPLEMENTATION.json",
    "FREE_API_QUERY_EXAMPLES_V36.json",
    "WALL_STREET_WHALE_INTELLIGENCE_V33.json",
)


def _check_imports() -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for distribution, module in REQUIRED_IMPORTS.items():
        try:
            mod = importlib.import_module(module)
            version = getattr(mod, "__version__", "installed")
            rows.append({"distribution": distribution, "module": module, "ok": True, "version": str(version)})
        except BaseException as exc:
            errors.append(f"missing/import-failed dependency {distribution} ({module}): {type(exc).__name__}: {exc}")
            rows.append({"distribution": distribution, "module": module, "ok": False, "error": str(exc)})
    return rows, errors


def _check_optional_imports() -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for distribution, module in OPTIONAL_ACCELERATOR_IMPORTS.items():
        try:
            mod = importlib.import_module(module)
            version = getattr(mod, "__version__", "installed")
            rows.append({"distribution": distribution, "module": module, "ok": True, "version": str(version), "fallback_safe": True})
        except BaseException as exc:
            warnings.append(f"optional enrichment accelerator unavailable {distribution} ({module}); deterministic fallback remains active: {type(exc).__name__}: {exc}")
            rows.append({"distribution": distribution, "module": module, "ok": False, "fallback_safe": True, "error": str(exc)})
    return rows, warnings


def _canonical_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name).strip()).lower()


def _requirement_name(text: str) -> str | None:
    raw = str(text).strip()
    try:
        req = Requirement(raw)
        if req.marker is not None and not req.marker.evaluate({"extra": ""}):
            return None
        return _canonical_distribution(req.name)
    except (InvalidRequirement, ValueError, KeyError):
        match = re.match(r"\s*([A-Za-z0-9_.-]+)", raw)
        return _canonical_distribution(match.group(1)) if match else None


def _project_requirement_roots() -> set[str]:
    root = Path(__file__).resolve().parents[3]
    seen_files: set[Path] = set()
    names: set[str] = set()

    def visit(path: Path) -> None:
        path = path.resolve()
        if path in seen_files or not path.is_file():
            return
        seen_files.add(path)
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(("-r ", "--requirement ")):
                target = line.split(None, 1)[1].strip()
                visit((path.parent / target).resolve())
                continue
            if line.startswith("-"):
                continue
            name = _requirement_name(line)
            if name:
                names.add(name)

    visit(root / "requirements-full.txt")
    return names


def _project_dependency_closure() -> set[str]:
    closure = set(_project_requirement_roots())
    queue = list(closure)
    while queue:
        name = queue.pop()
        try:
            requirements = importlib_metadata.requires(name) or []
        except importlib_metadata.PackageNotFoundError:
            continue
        except BaseException:
            continue
        for raw in requirements:
            dep = _requirement_name(raw)
            if dep and dep not in closure:
                closure.add(dep)
                queue.append(dep)
    return closure


def _pip_check_line_packages(line: str) -> set[str]:
    packages: set[str] = set()
    first = _requirement_name(line)
    if first:
        packages.add(first)
    for pattern in (r"has requirement\s+([A-Za-z0-9_.-]+)", r"requires\s+([A-Za-z0-9_.-]+)"):
        match = re.search(pattern, line, re.IGNORECASE)
        if match:
            packages.add(_canonical_distribution(match.group(1)))
    return packages


def _check_package_consistency() -> tuple[dict[str, Any], list[str]]:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    detail = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode == 0:
        return {"ok": True, "returncode": 0, "detail": detail}, []

    closure = _project_dependency_closure()
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    # `pip check` reports the distribution whose dependency declaration is broken
    # at the beginning of each line. Only that owning distribution determines
    # whether the conflict belongs to this project. An unrelated package may
    # legitimately depend on a distribution (for example Pillow) that is also
    # present in our dependency closure without making its own conflict ours.
    project_conflicts = [line for line in lines if (_requirement_name(line) or "") in closure]
    unrelated_conflicts = [line for line in lines if line not in project_conflicts]
    errors = [f"project dependency conflict: {line}" for line in project_conflicts]
    return {
        "ok": not project_conflicts,
        "returncode": proc.returncode,
        "detail": detail[:4000],
        "project_conflicts": project_conflicts,
        "ignored_unrelated_conflicts": unrelated_conflicts,
        "project_dependency_closure_size": len(closure),
    }, errors


def _required_services(settings: Settings) -> set[str]:
    required = {"news_hot", "news_coverage", "news_processor", "intelligence", "market_response", "research", "maintenance"}
    flags = {
        "gdelt": settings.gdelt_enabled,
        "google_mesh": settings.google_mesh_enabled,
        "x_social": settings.x_enabled,
        "bluesky": settings.bluesky_enabled,
        "social_pollers": settings.reddit_enabled or settings.mastodon_enabled or settings.newsapi_enabled,
        "sitemap": settings.sitemap_enabled,
        "news_enrichment": settings.news_enrichment_enabled,
        "translation": settings.translation_enabled,
        "alerts": settings.telegram_enabled,
        "structured": settings.structured_enabled,
        "institutional_connectors": settings.institutional_enabled,
        "provider_flows": settings.provider_flow_enabled,
        "premium_actions": os.getenv("PREMIUM_ACTIONS_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"},
        "prediction": settings.prediction_enabled,
        "official_native": settings.public_native_enabled,
        "altdata_native": settings.public_native_enabled,
        "filings_native": settings.public_native_enabled,
        "discovery": settings.discovery_enabled,
    }
    required.update(name for name, enabled in flags.items() if enabled)
    return required


def _env_keys(path: Path) -> list[str]:
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            out.append(key)
    return out


def _check_references(settings: Settings) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    rows: dict[str, Any] = {}
    for name in REQUIRED_REFERENCE_FILES:
        path = settings.reference_dir / name
        if not path.is_file():
            errors.append(f"missing reference file: {name}")
            rows[name] = {"ok": False}
            continue
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            rows[name] = {"ok": True, "bytes": path.stat().st_size, "type": type(obj).__name__}
        except BaseException as exc:
            errors.append(f"invalid reference JSON {name}: {type(exc).__name__}: {exc}")
            rows[name] = {"ok": False, "error": str(exc)}
    try:
        registry = SourceRegistry(settings)
        summary = registry.summary()
        rows["registry_summary"] = summary
        if summary.get("direct", 0) < 50:
            errors.append(f"source registry unexpectedly small direct_feeds={summary.get('direct')}")
        if summary.get("structured_providers", 0) < 80:
            errors.append(f"structured provider registry unexpectedly small providers={summary.get('structured_providers')}")
        if summary.get("parser_profiles", 0) < 365:
            errors.append(f"parser profile registry unexpectedly small profiles={summary.get('parser_profiles')}")
        if len(registry.google_locales()) < 78:
            errors.append(f"Google locale mesh unexpectedly small locales={len(registry.google_locales())}")
        if len(registry.x_handles()) < 150:
            errors.append(f"X handle registry unexpectedly small handles={len(registry.x_handles())}")
        rows["restored_runtime_contracts"] = {
            "google_locales": len(registry.google_locales()),
            "x_handles": len(registry.x_handles()),
            "parser_profiles": summary.get("parser_profiles", 0),
        }
        connector_envs: set[str] = set()
        for filename in ("V34_18_LAYER_IMPLEMENTATION.json", "V35_30_LAYER_IMPLEMENTATION.json", "V36_30_FINAL_GAP_CLOSURE_IMPLEMENTATION.json"):
            obj = json.loads((settings.reference_dir / filename).read_text(encoding="utf-8"))
            stack = [obj]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    value = cur.get("connector_env")
                    if isinstance(value, str) and value.strip():
                        connector_envs.add(value.strip())
                    stack.extend(cur.values())
                elif isinstance(cur, list):
                    stack.extend(cur)
        rows["restored_runtime_contracts"]["institutional_connector_envs"] = len(connector_envs)
        if len(connector_envs) < 100:
            errors.append(f"institutional connector contracts unexpectedly small connectors={len(connector_envs)}")
    except BaseException as exc:
        errors.append(f"source registry load failed: {type(exc).__name__}: {exc}")
    return rows, errors


def _check_sqlite_and_disk(settings: Settings) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    result: dict[str, Any] = {"sqlite_version": sqlite3.sqlite_version}
    settings.ensure_directories()
    try:
        with tempfile.TemporaryDirectory(prefix="v57-preflight-", dir=str(settings.data_dir)) as td:
            base = Path(td)
            db = base / "probe.sqlite3"
            con = sqlite3.connect(db)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
            con.execute("CREATE TABLE probe(id INTEGER PRIMARY KEY, v TEXT NOT NULL)")
            con.execute("INSERT INTO probe(v) VALUES(?)", ("ok",))
            con.commit()
            got = con.execute("SELECT v FROM probe").fetchone()[0]
            con.close()
            if got != "ok":
                raise RuntimeError("SQLite read-after-write mismatch")
            raw = base / "fdatasync-probe.bin"
            fd = os.open(raw, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
            try:
                payload = b"v57-durability-probe\n"
                n = os.write(fd, payload)
                if n != len(payload):
                    raise OSError(f"short write {n}/{len(payload)}")
                if hasattr(os, "fdatasync"):
                    os.fdatasync(fd)
                else:
                    os.fsync(fd)
            finally:
                os.close(fd)
            if raw.read_bytes() != payload:
                raise RuntimeError("fdatasync probe mismatch")
            result.update({"wal": True, "disk_write": True, "fdatasync": True})
    except BaseException as exc:
        errors.append(f"storage durability probe failed: {type(exc).__name__}: {exc}")
        result["error"] = str(exc)
    return result, errors




def _transport_probe(value: str, timeout: float = 8.0) -> dict[str, Any]:
    """Exercise DNS, TCP, TLS and (for WS) the real HTTP upgrade handshake."""
    parsed = urlparse(value)
    host = parsed.hostname
    if not host:
        raise ValueError("missing hostname")
    secure = parsed.scheme in {"https", "wss"}
    port = parsed.port or (443 if secure else 80)
    addresses = sorted({x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM) if x and x[4]})
    started = time.monotonic()
    raw = socket.create_connection((host, port), timeout=timeout)
    sock = raw
    tls = False
    try:
        if secure:
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            tls = True
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        if parsed.scheme in {"ws", "wss"}:
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            request = (
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: V57-Preflight/57.0\r\n\r\n"
            ).encode("ascii")
        else:
            request = f"HEAD {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nUser-Agent: V57-Preflight/57.0\r\n\r\n".encode("ascii")
        sock.sendall(request)
        first = sock.recv(4096).split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
        parts = first.split()
        status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        ws_ok = parsed.scheme not in {"ws", "wss"} or status == 101
        return {
            "ok": bool(addresses) and status > 0 and ws_ok,
            "host": host, "port": port, "addresses": addresses[:8], "tcp": True, "tls": tls,
            "http_status": status, "websocket_upgrade": status == 101 if parsed.scheme in {"ws", "wss"} else None,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }
    finally:
        sock.close()


def _json_auth_probe(url: str, headers: dict[str, str] | None = None, payload: bytes | None = None, timeout: float = 10.0) -> dict[str, Any]:
    request = Request(url, data=payload, headers=headers or {}, method="POST" if payload is not None else "GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            obj = json.loads(response.read(1_000_000))
            return {"ok": response.status == 200 and obj.get("ok", True) is not False, "http_status": response.status, "authenticated": True}
    except HTTPError as exc:
        return {"ok": False, "http_status": exc.code, "authenticated": False, "error": "HTTPError"}
    except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "authenticated": False, "error": type(exc).__name__}


def _check_network(settings: Settings) -> tuple[dict[str, Any], list[str]]:
    targets: dict[str, str] = {
        "bybit": os.getenv("BYBIT_WS", "wss://stream.bybit.com/v5/public/linear"),
        "binance_public": os.getenv("BINANCE_FUTURES_PUBLIC_WS", "wss://fstream.binance.com/public/stream"),
        "binance_market": os.getenv("BINANCE_FUTURES_MARKET_WS", "wss://fstream.binance.com/market/stream"),
        "mexc": os.getenv("MEXC_FUTURES_WS", "wss://contract.mexc.com/edge"),
        "dydx": os.getenv("DYDX_WS", "wss://indexer.dydx.trade/v4/ws"),
    }
    if settings.telegram_enabled:
        targets["telegram"] = settings.telegram_api_base
    if settings.translation_enabled and not settings.libretranslate_url.startswith(("http://127.0.0.1", "http://localhost")):
        targets["translation"] = settings.libretranslate_url
    rows: dict[str, Any] = {}
    errors: list[str] = []
    for name, value in targets.items():
        try:
            rows[name] = _transport_probe(value)
            if not rows[name]["ok"]:
                errors.append(f"transport probe failed for {name}: HTTP/WS status={rows[name].get('http_status')}")
        except (OSError, ValueError, ssl.SSLError) as exc:
            rows[name] = {"ok": False, "host": urlparse(value).hostname, "error": type(exc).__name__}
            errors.append(f"transport probe failed for {name}: {type(exc).__name__}")
    if settings.telegram_enabled and settings.telegram_bot_token and settings.telegram_chat_id:
        base = f"{settings.telegram_api_base.rstrip('/')}/bot{settings.telegram_bot_token}"
        rows["telegram_auth"] = _json_auth_probe(f"{base}/getMe")
        payload = json.dumps({"chat_id": settings.telegram_chat_id}).encode("utf-8")
        rows["telegram_chat"] = _json_auth_probe(f"{base}/getChat", {"Content-Type": "application/json"}, payload)
        if not rows["telegram_auth"]["ok"] or not rows["telegram_chat"]["ok"]:
            errors.append("Telegram credential/chat validation failed")
    if os.getenv("OPENAI_SEMANTICS_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}:
        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            errors.append("OPENAI_SEMANTICS_ENABLED=1 requires OPENAI_API_KEY")
            rows["openai_auth"] = {"ok": False, "authenticated": False, "error": "missing_credential"}
        else:
            rows["openai_auth"] = _json_auth_probe("https://api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})
            if not rows["openai_auth"]["ok"]:
                errors.append("OpenAI API authentication probe failed")
    return rows, errors

def run_preflight(settings: Settings) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    deps, dep_errors = _check_imports()
    errors.extend(dep_errors)
    optional_deps, optional_warnings = _check_optional_imports()
    warnings.extend(optional_warnings)
    package_consistency, package_errors = _check_package_consistency()
    errors.extend(package_errors)

    env_path = settings.package_root / ".env.example"
    schema_path = settings.package_root / "config" / "settings.schema.json"
    try:
        env_keys = _env_keys(env_path)
        duplicates = sorted({x for x in env_keys if env_keys.count(x) > 1})
        if duplicates:
            errors.append(f"duplicate .env.example keys: {duplicates}")
        schema = load_schema(settings.package_root)
        schema_settings = schema.get("settings", [])
        schema_keys = [str(x.get("name")) for x in schema_settings if isinstance(x, dict) and x.get("name")]
        if set(env_keys) != set(schema_keys):
            errors.append(".env.example and settings.schema.json key sets differ")
        env_result = {"count": len(env_keys), "unique": len(set(env_keys)), "schema_count": len(schema_keys), "duplicates": duplicates}
    except BaseException as exc:
        errors.append(f"config schema validation failed: {type(exc).__name__}: {exc}")
        env_result = {"error": str(exc)}

    refs, ref_errors = _check_references(settings)
    errors.extend(ref_errors)
    storage, storage_errors = _check_sqlite_and_disk(settings)
    errors.extend(storage_errors)
    network: dict[str, Any] = {"mode": "disabled"}
    if settings.preflight_strict_network:
        network, network_errors = _check_network(settings)
        errors.extend(network_errors)

    unknown = [s for s in settings.supervisor_services if s not in DEFAULT_SERVICES]
    if unknown:
        errors.append(f"unknown SUPERVISOR_SERVICES: {unknown}")
    if len(settings.supervisor_services) != len(set(settings.supervisor_services)):
        errors.append("duplicate services in SUPERVISOR_SERVICES")
    required_services = _required_services(settings)
    missing_services = sorted(required_services.difference(settings.supervisor_services))
    if missing_services:
        errors.append(f"enabled features require missing services: {missing_services}")
    configured_market_services = sorted(set(settings.supervisor_services).intersection(MARKET_SERVICES))
    if settings.market_enabled and not configured_market_services:
        errors.append("MARKET_ENABLED=1 requires at least one market collector in SUPERVISOR_SERVICES")
    if settings.telegram_enabled and (not settings.telegram_bot_token or not settings.telegram_chat_id):
        errors.append("TELEGRAM_ENABLED=1 requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    if not settings.user_agent or "operator-contact-required" in settings.user_agent:
        warnings.append("Set HTTP_USER_AGENT to a real operator contact before internet production use")
    if settings.translation_enabled and not settings.libretranslate_url:
        errors.append("TRANSLATION_ENABLED=1 requires LIBRETRANSLATE_URL")
    if not settings.market_enabled and configured_market_services:
        warnings.append("MARKET_ENABLED=0: market_* services will self-disable; remove them from SUPERVISOR_SERVICES to save process overhead")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        warnings.append("Running V57 as root is not recommended; install the systemd service with the dedicated v57fund user")

    disk = shutil.disk_usage(settings.data_dir)
    result = {
        "schema": "59.0-preflight",
        "at": iso_now(),
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "python": {"version": platform.python_version(), "executable": os.path.realpath(os.sys.executable), "implementation": platform.python_implementation()},
        "dependencies": deps,
        "optional_accelerators": optional_deps,
        "package_consistency": package_consistency,
        "config": env_result,
        "references": refs,
        "storage": storage,
        "network": network,
        "filesystem": {"data_dir": str(settings.data_dir), "free_bytes": disk.free, "total_bytes": disk.total},
        "services": {"configured": list(settings.supervisor_services), "required": sorted(required_services), "missing": missing_services},
        "settings": settings.public_dict(),
    }
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="V57 complete dependency/config/storage preflight")
    ap.add_argument("--json", dest="json_path", default="")
    args = ap.parse_args()
    try:
        settings = Settings.from_env()
        result = run_preflight(settings)
    except BaseException as exc:
        result = {"schema": "59.0-preflight", "at": iso_now(), "ok": False, "errors": [f"preflight bootstrap failed: {type(exc).__name__}: {exc}"]}
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.json_path:
        path = Path(args.json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
