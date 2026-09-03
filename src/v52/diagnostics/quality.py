from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..constants import MARKET_SERVICES, NEWS_RAW_PRODUCER_SERVICES
from ..timeutil import age_seconds, parse_datetime, iso_now


def _load(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("JSON root must be object")
        return obj
    except BaseException as exc:
        raise RuntimeError(f"cannot load required diagnostic artifact {path}: {type(exc).__name__}: {exc}") from exc


def _db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=15.0)
    con.row_factory = sqlite3.Row
    return con


def _counter(final: dict[str, Any], name: str) -> float:
    return float((final.get("counters") or {}).get(name, 0) or 0)


def _gauge(final: dict[str, Any], name: str, default: Any = 0) -> Any:
    return (final.get("gauges") or {}).get(name, default)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    pos = (len(s) - 1) * pct
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] * (hi - pos) + s[hi] * (pos - lo)


def _check(name: str, ok: bool, actual: Any, expected: Any, *, critical: bool = True, note: str = "") -> dict[str, Any]:
    return {"name": name, "status": "PASS" if ok else "FAIL", "critical": critical, "actual": actual, "expected": expected, "note": note}


def _story_metrics(settings: Settings, run_id: str, start_epoch: float, stop_epoch: float) -> dict[str, Any]:
    path = settings.data_dir / "db" / "control.sqlite3"
    if not path.exists():
        return {"unique": 0, "material": 0, "timed_material": 0, "le5": 0, "le15": 0, "le60": 0, "ages": []}
    with _db(path) as con:
        rows = con.execute(
            "SELECT id,materiality,published_at,first_observed_at FROM stories WHERE run_id=? AND created_at>=? AND created_at<=?",
            (run_id, start_epoch, stop_epoch + 2.0),
        ).fetchall()
    material = [r for r in rows if int(r["materiality"] or 0) >= settings.news_materiality_threshold]
    ages: list[float] = []
    for r in material:
        pub, obs = parse_datetime(r["published_at"]), parse_datetime(r["first_observed_at"])
        if pub and obs:
            age = age_seconds(pub, obs)
            if age is not None and age >= 0:
                ages.append(float(age))
    return {
        "unique": len(rows), "material": len(material), "timed_material": len(ages),
        "le5": sum(x <= 5 for x in ages), "le15": sum(x <= 15 for x in ages), "le60": sum(x <= 60 for x in ages),
        "ages": ages,
    }


def _new_raw_count(settings: Settings, run_id: str) -> dict[str, int]:
    path = settings.data_dir / "db" / "news.sqlite3"
    if not path.exists():
        return {"created": 0, "done": 0, "pending": 0, "leased": 0}
    with _db(path) as con:
        rows = con.execute("SELECT state,COUNT(*) c FROM raw_news WHERE run_id=? GROUP BY state", (run_id,)).fetchall()
    out = {"created": 0, "done": 0, "pending": 0, "leased": 0}
    for r in rows:
        out[str(r["state"])] = int(r["c"])
        out["created"] += int(r["c"])
    return out


def _control_counts(settings: Settings, start_epoch: float | None = None) -> dict[str, int]:
    path = settings.data_dir / "db" / "control.sqlite3"
    if not path.exists():
        return {}
    out: dict[str, int] = {}
    with _db(path) as con:
        for table in ("translation_jobs", "alerts", "enrichment_jobs"):
            where = ""
            args: tuple[Any, ...] = ()
            if start_epoch is not None:
                where = " WHERE created_at>=?"
                args = (start_epoch,)
            rows = con.execute(f"SELECT state,COUNT(*) c FROM {table}{where} GROUP BY state", args).fetchall()
            for r in rows:
                out[f"{table}_{r['state']}"] = int(r["c"])
    return out


def _brain_metrics(settings: Settings, start_epoch: float) -> dict[str, Any]:
    path = settings.data_dir / "brain" / "intelligence.sqlite3"
    if not path.exists():
        return {"exists": False, "schema_version": 0, "events": 0, "predictions": 0, "warmup_delivery_violations": 0, "raw_alert_violations": 0}
    with _db(path) as con:
        schema = con.execute("SELECT COALESCE(MAX(version),0) version FROM schema_migrations").fetchone()
        events = con.execute("SELECT COUNT(*) c FROM events WHERE created_at>=?", (start_epoch,)).fetchone()
        predictions = con.execute("SELECT COUNT(*) c FROM predictions WHERE created_at>=?", (start_epoch,)).fetchone()
        started = con.execute("SELECT value FROM metadata WHERE key='deployment_started_at'").fetchone()
        deployment_started = float(started["value"]) if started else start_epoch
        violations = con.execute(
            """SELECT COUNT(*) c FROM signals WHERE telegram_state='ELIGIBLE' AND opened_at<?""",
            (deployment_started + 172800,),
        ).fetchone()
    control = settings.data_dir / "db" / "control.sqlite3"
    raw_alerts = 0
    delivered_without_id = 0
    if control.exists():
        with _db(control) as con:
            raw_alerts = int(con.execute("SELECT COUNT(*) c FROM alerts WHERE created_at>=? AND kind IN ('story','market','news')", (start_epoch,)).fetchone()["c"])
            delivered_without_id = int(con.execute("SELECT COUNT(*) c FROM alerts WHERE created_at>=? AND state='sent' AND (telegram_message_id IS NULL OR telegram_message_id='')", (start_epoch,)).fetchone()["c"])
    return {
        "exists": True,
        "schema_version": int(schema["version"]),
        "events": int(events["c"]),
        "predictions": int(predictions["c"]),
        "warmup_delivery_violations": int(violations["c"]),
        "raw_alert_violations": raw_alerts,
        "telegram_unconfirmed_sent": delivered_without_id,
    }


def _resource_metrics(path: Path) -> dict[str, Any]:
    psi: list[float] = []
    rss: list[float] = []
    if not path.exists():
        return {"samples": 0, "cpu_psi_avg10_p50": None, "cpu_psi_avg10_p95": None, "child_rss_bytes_p95": None}
    bad = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            p = ((row.get("cpu_psi") or {}).get("some") or {}).get("avg10")
            if p is not None:
                psi.append(float(p))
            total_rss = 0.0
            for child in (row.get("children") or {}).values():
                total_rss += float((child or {}).get("VmRSS_bytes", 0) or 0)
            if total_rss:
                rss.append(total_rss)
        except BaseException:
            bad += 1
    return {
        "samples": max(len(psi), len(rss)), "parse_errors": bad,
        "cpu_psi_avg10_p50": _percentile(psi, 0.50), "cpu_psi_avg10_p95": _percentile(psi, 0.95), "cpu_psi_avg10_max": max(psi) if psi else None,
        "child_rss_bytes_p95": _percentile(rss, 0.95), "child_rss_bytes_max": max(rss) if rss else None,
    }


def evaluate(settings: Settings, run_id: str, *, mode: str = "full") -> dict[str, Any]:
    run_dir = settings.run_dir / run_id
    run = _load(run_dir / "run.json")
    sup = _load(run_dir / "supervisor_final.json")
    start_epoch = float(run.get("start_epoch") or 0)
    stop_epoch = parse_datetime(sup.get("stopped_at"))
    stop_epoch_f = stop_epoch.timestamp() if stop_epoch else time.time()
    duration = max(1.0, stop_epoch_f - start_epoch)
    finals: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for svc in run.get("services") or []:
        p = run_dir / "services" / svc / "final.json"
        if p.exists():
            finals[svc] = _load(p)
        else:
            missing.append(svc)

    checks: list[dict[str, Any]] = []
    checks.append(_check("supervisor_operational", bool(sup.get("operational_success")), sup.get("operational_success"), True))
    checks.append(_check("all_service_finals_present", not missing, missing, []))
    fatal = {s: int(f.get("exit_code", 1) or 0) for s, f in finals.items() if int(f.get("exit_code", 1) or 0) != 0 or _counter(f, "service_fatal") > 0}
    checks.append(_check("no_service_fatal", not fatal, fatal, {}))
    restarts = {k: int(v) for k, v in (sup.get("restart_counts") or {}).items() if int(v or 0) > 0}
    checks.append(_check("zero_unexpected_restarts", not restarts, restarts, {}))
    forced_kills = list(sup.get("forced_kills") or [])
    checks.append(_check("zero_forced_shutdown_kills", not forced_kills, forced_kills, []))
    stale_terminations = list(sup.get("stale_terminations") or [])
    checks.append(_check("zero_stale_heartbeat_terminations", not stale_terminations, stale_terminations, []))

    # Exact NewsStore durability accounting: every collector-reported inserted item must exist durably in raw_news.
    inserted = sum(int(_counter(finals.get(s, {}), "news_raw_inserted")) for s in NEWS_RAW_PRODUCER_SERVICES if s in finals)
    raw = _new_raw_count(settings, run_id)
    checks.append(_check("raw_news_insert_accounting", inserted == raw["created"], {"collector_inserted": inserted, **raw}, "collector_inserted == durable_raw_rows"))

    stories = _story_metrics(settings, run_id, start_epoch, stop_epoch_f)
    duration_factor = duration / 1200.0
    target_unique = max(1, math.ceil(settings.diagnostic_min_unique_20m * duration_factor))
    target_material = max(1, math.ceil(settings.diagnostic_min_material_unique_20m * duration_factor))
    if mode == "full":
        checks.append(_check("diagnostic_duration", duration >= 1100.0, round(duration, 1), ">=1100 seconds"))
        checks.append(_check("news_unique_volume", stories["unique"] >= target_unique, stories["unique"], f">={target_unique}"))
        checks.append(_check("material_news_volume", stories["material"] >= target_material, stories["material"], f">={target_material}"))
        timed = stories["timed_material"]
        ratio15 = stories["le15"] / timed if timed else 0.0
        ratio60 = stories["le60"] / timed if timed else 0.0
        checks.append(_check("material_news_under_15s_ratio", ratio15 >= settings.diagnostic_min_under_15s_ratio, round(ratio15, 4), f">={settings.diagnostic_min_under_15s_ratio}"))
        checks.append(_check("material_news_under_60s_ratio", ratio60 >= settings.diagnostic_min_under_60s_ratio, round(ratio60, 4), f">={settings.diagnostic_min_under_60s_ratio}"))
    else:
        checks.append(_check("smoke_generated_news_activity", raw["created"] >= 0, raw["created"], ">=0", critical=False))

    # Queue growth gates are based on durable state, not fake in-memory qsize proxies.
    baseline = (run.get("baseline") or {}).get("control_queues") or {}
    endq = _control_counts(settings)
    trans_start = int(baseline.get("translation_jobs_pending", 0)) + int(baseline.get("translation_jobs_leased", 0))
    trans_end = int(endq.get("translation_jobs_pending", 0)) + int(endq.get("translation_jobs_leased", 0))
    growth = trans_end - trans_start
    if settings.translation_enabled:
        checks.append(_check("translation_backlog_growth", growth <= settings.diagnostic_max_translation_backlog_growth, growth, f"<={settings.diagnostic_max_translation_backlog_growth}"))
    newq = _control_counts(settings, start_epoch)
    checks.append(_check("no_new_dead_translation_jobs", int(newq.get("translation_jobs_dead", 0)) == 0, int(newq.get("translation_jobs_dead", 0)), 0))
    checks.append(_check("no_new_dead_enrichment_jobs", int(newq.get("enrichment_jobs_dead", 0)) == 0, int(newq.get("enrichment_jobs_dead", 0)), 0))
    if settings.telegram_enabled:
        checks.append(_check("no_new_dead_alerts", int(newq.get("alerts_dead", 0)) == 0, int(newq.get("alerts_dead", 0)), 0))

    brain = _brain_metrics(settings, start_epoch)
    checks.append(_check("brain_schema_ready", brain["exists"] and brain["schema_version"] == 1, {"exists": brain["exists"], "schema_version": brain["schema_version"]}, {"exists": True, "schema_version": 1}))
    checks.append(_check("warmup_signal_blackout", brain["warmup_delivery_violations"] == 0, brain["warmup_delivery_violations"], 0))
    checks.append(_check("telegram_intelligence_only", brain["raw_alert_violations"] == 0, brain["raw_alert_violations"], 0))
    if settings.telegram_enabled:
        checks.append(_check("telegram_delivery_confirmed", brain["telegram_unconfirmed_sent"] == 0, brain["telegram_unconfirmed_sent"], 0))
    if stories["unique"] > 0:
        checks.append(_check("durable_stories_enter_brain", brain["events"] > 0, brain["events"], ">0"))

    # Market exact persistence invariants are emitted by each isolated market service on graceful shutdown.
    market_detail: dict[str, Any] = {}
    if settings.market_enabled:
        for svc in (s for s in run.get("services") or [] if s in MARKET_SERVICES):
            f = finals.get(svc, {})
            c, g = f.get("counters") or {}, f.get("gauges") or {}
            detail = {
                "raw_enqueued": int(c.get("market_raw_enqueued", 0) or 0),
                "raw_written": int(c.get("market_raw_written", 0) or 0),
                "raw_synced": int(g.get("market_raw_synced", 0) or 0),
                "semantic_enqueued": int(c.get("market_semantic_enqueued", 0) or 0),
                "semantic_persisted": int(c.get("market_semantic_persisted", 0) or 0),
                "raw_q": int(g.get("market_raw_qsize", 0) or 0),
                "semantic_q": int(g.get("market_semantic_qsize", 0) or 0),
                "recovery_pending": int(g.get("market_recovery_pending", 0) or 0),
                "raw_invariant": int(g.get("market_raw_invariant_ok", 0) or 0),
                "semantic_invariant": int(g.get("market_semantic_invariant_ok", 0) or 0),
            }
            market_detail[svc] = detail
            raw_ok = detail["raw_enqueued"] == detail["raw_written"] == detail["raw_synced"] and detail["raw_q"] == 0 and detail["raw_invariant"] == 1
            sem_ok = detail["semantic_enqueued"] == detail["semantic_persisted"] and detail["semantic_q"] == 0 and detail["semantic_invariant"] == 1
            checks.append(_check(f"{svc}_raw_durability", raw_ok, detail, "enqueued==written==synced; q=0; invariant=1"))
            checks.append(_check(f"{svc}_semantic_durability", sem_ok, detail, "enqueued==persisted; q=0; invariant=1"))
            checks.append(_check(f"{svc}_recovery_complete", detail["recovery_pending"] == 0, detail["recovery_pending"], 0))

    resources = _resource_metrics(run_dir / "resources.jsonl")
    p95 = resources.get("cpu_psi_avg10_p95")
    if mode == "full":
        checks.append(_check("cpu_psi_avg10_p95", p95 is not None and float(p95) <= settings.diagnostic_max_cpu_psi_avg10, p95, f"<={settings.diagnostic_max_cpu_psi_avg10}"))
        checks.append(_check("resource_samples_parse_clean", int(resources.get("parse_errors", 0)) == 0, resources.get("parse_errors", 0), 0))

    critical_failed = [x for x in checks if x["critical"] and x["status"] != "PASS"]
    result = {
        "schema": "52.0-quality-gate", "at": iso_now(), "run_id": run_id, "mode": mode,
        "status": "PASS" if not critical_failed else "FAIL", "duration_seconds": duration,
        "checks": checks, "critical_failures": [x["name"] for x in critical_failed],
        "metrics": {
            "stories": {**{k: v for k, v in stories.items() if k != "ages"},
                         "age_seconds_p50": _percentile(stories["ages"], 0.5), "age_seconds_p95": _percentile(stories["ages"], 0.95)},
            "raw_news": raw, "control_start": baseline, "control_end": endq, "control_new": newq,
            "translation_backlog_growth": growth, "market": market_detail, "brain": brain, "resources": resources,
        },
    }
    out = run_dir / "quality_gate.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(out)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="V52 unified process/data/freshness/resource quality gate")
    ap.add_argument("run_id")
    ap.add_argument("--mode", choices=("full", "smoke"), default="full")
    args = ap.parse_args()
    settings = Settings.from_env()
    try:
        result = evaluate(settings, args.run_id, mode=args.mode)
    except BaseException as exc:
        result = {"schema": "52.0-quality-gate", "at": iso_now(), "run_id": args.run_id, "mode": args.mode, "status": "FAIL", "critical_failures": ["diagnostic_execution"], "error": f"{type(exc).__name__}: {exc}"}
        run_dir = settings.run_dir / args.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "quality_gate.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("status") == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
