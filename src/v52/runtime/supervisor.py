from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from ..config import Settings
from ..constants import SOURCE_PRODUCER_SERVICES, PROCESSOR_SERVICES, DOWNSTREAM_SERVICES
from ..loggingx import configure_logging
from ..storage.control import ControlStore
from ..storage.news import NewsStore
from ..timeutil import iso_now
from .health import cpu_psi, memory_info, proc_start_ticks, proc_stats

logger = logging.getLogger(__name__)


def _status_pid_matches(
    obj: dict[str, Any],
    pid: int,
    *,
    supervisor_pid: int | None = None,
    allow_launcher_wrapper: bool = False,
) -> bool:
    """Match a heartbeat to a tracked process, including a verified Windows venv wrapper."""
    if int(obj.get("pid") or 0) == int(pid):
        return True
    extra = obj.get("extra") or {}
    return bool(
        allow_launcher_wrapper
        and supervisor_pid
        and int(extra.get("parent_pid") or 0) == int(supervisor_pid)
    )


def heartbeat_issue(status_path: Path, *, service: str, pid: int, run_id: str, started_epoch: float, now_epoch: float,
                    stale_seconds: float, supervisor_pid: int | None = None,
                    allow_launcher_wrapper: bool = False) -> str | None:
    # Give a freshly spawned service one complete stale window to publish its first heartbeat.
    if now_epoch - started_epoch <= stale_seconds:
        return None
    try:
        st = status_path.stat()
    except FileNotFoundError:
        return "missing_status"
    except OSError as exc:
        return f"status_stat_error:{type(exc).__name__}"
    if now_epoch - st.st_mtime > stale_seconds:
        return f"stale_status:{now_epoch - st.st_mtime:.1f}s"
    try:
        obj = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"invalid_status:{type(exc).__name__}"
    if obj.get("service") != service:
        return "service_identity_mismatch"
    if not _status_pid_matches(
        obj, pid, supervisor_pid=supervisor_pid, allow_launcher_wrapper=allow_launcher_wrapper
    ):
        return "pid_identity_mismatch"
    gauges = obj.get("gauges") or {}
    if str(gauges.get("run_id") or "") != run_id:
        return "run_identity_mismatch"
    return None


class Supervisor:
    def __init__(self, settings: Settings, run_seconds: float = 0.0):
        self.settings = settings
        self.run_seconds = run_seconds
        self.stop_event = asyncio.Event()
        self.children: dict[str, asyncio.subprocess.Process] = {}
        self.logs: dict[str, Any] = {}
        self.restart_times: dict[str, deque[float]] = defaultdict(deque)
        self.restart_counts: dict[str, int] = defaultdict(int)
        self.exit_codes: dict[str, int | None] = {}
        self.started: set[str] = set()
        self.failed_reason = ""
        self.run_id = os.getenv("V52_RUN_ID") or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
        self.run_dir = settings.run_dir / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.resource_task: asyncio.Task[Any] | None = None
        self.monitor_tasks: dict[str, asyncio.Task[Any]] = {}
        self.supervisor_start_ticks = proc_start_ticks(os.getpid()) or 0
        self.forced_kills: list[str] = []
        self.shutdown_phases: list[dict[str, Any]] = []
        self.service_started_epoch: dict[str, float] = {}
        self.stale_signaled: set[str] = set()
        self.stale_terminations: list[dict[str, Any]] = []

    async def _baseline(self) -> dict[str, Any]:
        news = NewsStore(self.settings)
        control = ControlStore(self.settings)
        return {
            "raw_pending": await news.pending_count(),
            "control_queues": await control.queue_counts(),
        }

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["V52_RUN_ID"] = self.run_id
        env["V52_SUPERVISOR_PID"] = str(os.getpid())
        env["V52_SUPERVISOR_START_TICKS"] = str(self.supervisor_start_ticks)
        env["V52_PACKAGE_ROOT"] = str(self.settings.package_root)
        # Child stdout is already captured by the supervisor.  Their structured
        # JSON logs are the canonical files, so avoid writing every record twice.
        env["V52_CONSOLE_LOGGING"] = "0"
        src = str(self.settings.package_root / "src")
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return env

    async def start_service(self, service: str) -> None:
        log_dir = self.run_dir / "services" / service
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "service.log"
        fh = open(log_path, "ab", buffering=0)
        self.logs[service] = fh
        cmd = [sys.executable, "-m", "v52.runtime.service", service]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=fh, stderr=asyncio.subprocess.STDOUT, env=self._env(), cwd=str(self.settings.package_root))
        self.children[service] = proc
        self.service_started_epoch[service] = time.time()
        self.stale_signaled.discard(service)
        self.started.add(service)
        self.exit_codes[service] = None
        logger.info("service started service=%s pid=%s", service, proc.pid)
        self.monitor_tasks[service] = asyncio.create_task(self.monitor_service(service, proc), name=f"monitor:{service}")

    async def monitor_service(self, service: str, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        self.exit_codes[service] = code
        if self.stop_event.is_set():
            return
        logger.error("service exited unexpectedly service=%s code=%s", service, code)
        now = time.monotonic()
        q = self.restart_times[service]
        q.append(now)
        while q and now - q[0] > self.settings.supervisor_restart_window_seconds:
            q.popleft()
        if len(q) > self.settings.supervisor_restart_limit:
            self.failed_reason = f"restart budget exhausted service={service} exits={len(q)}"
            logger.critical(self.failed_reason)
            self.stop_event.set()
            return
        self.restart_counts[service] += 1
        await asyncio.sleep(min(30.0, 1.0 * (2 ** min(5, len(q) - 1))))
        if not self.stop_event.is_set():
            old = self.logs.pop(service, None)
            if old:
                old.close()
            await self.start_service(service)

    async def wait_for_startup_ready(self) -> None:
        """Fail closed unless every configured child publishes a heartbeat for this run/pid.

        The diagnostic timer must measure steady runtime, not interpreter/import startup.  This also
        prevents short smoke runs from signaling a freshly spawned child before its signal handler
        and final-artifact path are installed.
        """
        deadline = time.monotonic() + max(10.0, self.settings.service_stale_seconds)
        pending = set(self.settings.supervisor_services)
        while pending and time.monotonic() < deadline and not self.stop_event.is_set():
            ready: set[str] = set()
            for name in pending:
                proc = self.children.get(name)
                if proc is None:
                    continue
                if proc.returncode is not None:
                    self.failed_reason = f"service exited before readiness service={name} code={proc.returncode}"
                    self.stop_event.set()
                    return
                path = self.settings.state_dir / name / "status.json"
                try:
                    obj = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                gauges = obj.get("gauges") or {}
                if (
                    obj.get("service") == name
                    and _status_pid_matches(
                        obj,
                        int(proc.pid),
                        supervisor_pid=os.getpid(),
                        allow_launcher_wrapper=os.name == "nt",
                    )
                    and str(gauges.get("run_id") or "") == self.run_id
                ):
                    ready.add(name)
            pending.difference_update(ready)
            if pending:
                await asyncio.sleep(0.10)
        if pending:
            self.failed_reason = f"startup readiness timeout services={sorted(pending)}"
            logger.critical(self.failed_reason)
            self.stop_event.set()
            return
        logger.info("all services ready count=%s", len(self.settings.supervisor_services))

    async def resource_loop(self) -> None:
        path = self.run_dir / "resources.jsonl"
        sample_seconds = max(5.0, float(os.getenv("RESOURCE_SAMPLE_SECONDS", "30")))
        next_sample = 0.0
        with path.open("a", encoding="utf-8", buffering=1) as fh:
            while not self.stop_event.is_set():
                now_epoch = time.time()
                for name, proc in list(self.children.items()):
                    if proc.returncode is not None or name in self.stale_signaled:
                        continue
                    issue = heartbeat_issue(
                        self.settings.state_dir / name / "status.json", service=name, pid=proc.pid, run_id=self.run_id,
                        started_epoch=self.service_started_epoch.get(name, now_epoch), now_epoch=now_epoch,
                        stale_seconds=self.settings.service_stale_seconds,
                        supervisor_pid=os.getpid(), allow_launcher_wrapper=os.name == "nt",
                    )
                    if issue:
                        self.stale_signaled.add(name)
                        event = {"service": name, "pid": proc.pid, "issue": issue, "at": iso_now()}
                        self.stale_terminations.append(event)
                        logger.error("service heartbeat unhealthy; terminating for supervised recovery service=%s pid=%s issue=%s", name, proc.pid, issue)
                        try:
                            proc.send_signal(signal.SIGTERM)
                        except ProcessLookupError:
                            logger.info("stale service exited before watchdog signal service=%s", name)
                if now_epoch >= next_sample:
                    children = {name: proc_stats(p.pid) for name, p in self.children.items() if p.returncode is None}
                    payload = {
                        "ts": iso_now(),
                        "supervisor": proc_stats(),
                        "children": children,
                        "cpu_psi": cpu_psi(),
                        "memory": memory_info(),
                        "stale_signaled": sorted(self.stale_signaled),
                    }
                    fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                    next_sample = now_epoch + sample_seconds
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue

    def _alive_services(self, names: tuple[str, ...] | list[str]) -> list[str]:
        return [name for name in names if (proc := self.children.get(name)) is not None and proc.returncode is None]

    def _signal_services(self, names: tuple[str, ...] | list[str], sig: signal.Signals = signal.SIGTERM) -> list[str]:
        signaled: list[str] = []
        for service in names:
            proc = self.children.get(service)
            if proc is None or proc.returncode is not None:
                continue
            try:
                proc.send_signal(sig)
                signaled.append(service)
            except ProcessLookupError:
                logger.info("service already exited before signal service=%s", service)
        return signaled

    async def _wait_until(self, names: tuple[str, ...] | list[str], deadline: float) -> list[str]:
        while time.monotonic() < deadline:
            alive = self._alive_services(names)
            if not alive:
                return []
            await asyncio.sleep(min(0.20, max(0.01, deadline - time.monotonic())))
        return self._alive_services(names)

    async def _drain_pause(self, seconds: float, overall_deadline: float, label: str) -> None:
        remaining = max(0.0, overall_deadline - time.monotonic())
        delay = min(max(0.0, seconds), remaining)
        if delay <= 0:
            return
        logger.info("shutdown drain phase=%s seconds=%.2f", label, delay)
        self.shutdown_phases.append({"phase": label, "drain_seconds": delay, "at": iso_now()})
        try:
            await asyncio.wait_for(asyncio.sleep(delay), timeout=delay + 0.5)
        except asyncio.TimeoutError:
            logger.warning("shutdown drain timer overran phase=%s", label)

    async def shutdown(self) -> None:
        self.stop_event.set()
        overall_deadline = time.monotonic() + self.settings.shutdown_grace_seconds
        logger.info("supervisor dependency-aware shutdown children=%s", list(self.children))

        # Phase 1: stop external producers first. Durable downstream services remain alive.
        producers = tuple(x for x in SOURCE_PRODUCER_SERVICES if x in self.children)
        signaled = self._signal_services(producers)
        self.shutdown_phases.append({"phase": "producers_stop", "services": signaled, "at": iso_now()})
        producer_wait = min(overall_deadline, time.monotonic() + max(3.0, self.settings.shutdown_grace_seconds * 0.45))
        remaining_producers = await self._wait_until(producers, producer_wait)
        if remaining_producers:
            logger.warning("producer services still stopping services=%s", remaining_producers)

        # Let news_processor consume durable raw rows while translation/enrichment/alerts stay online.
        await self._drain_pause(self.settings.shutdown_drain_seconds, overall_deadline, "raw_to_story_drain")

        processors = tuple(x for x in PROCESSOR_SERVICES if x in self.children)
        signaled = self._signal_services(processors)
        self.shutdown_phases.append({"phase": "processors_stop", "services": signaled, "at": iso_now()})
        processor_wait = min(overall_deadline, time.monotonic() + max(2.0, self.settings.shutdown_grace_seconds * 0.15))
        await self._wait_until(processors, processor_wait)

        # Allow jobs already committed by the processor to complete before consumers stop.
        await self._drain_pause(self.settings.shutdown_drain_seconds, overall_deadline, "downstream_drain")

        downstream = tuple(x for x in DOWNSTREAM_SERVICES if x in self.children)
        classified = set(producers) | set(processors) | set(downstream)
        other = tuple(x for x in self.children if x not in classified)
        signaled = self._signal_services((*downstream, *other))
        self.shutdown_phases.append({"phase": "downstream_stop", "services": signaled, "at": iso_now()})

        all_names = tuple(self.children)
        remaining = await self._wait_until(all_names, overall_deadline)
        if remaining:
            logger.error("shutdown grace exhausted; forcing kill services=%s", remaining)
            self.forced_kills.extend(remaining)
            for service in remaining:
                proc = self.children[service]
                try:
                    proc.kill()
                except ProcessLookupError:
                    logger.info("service exited before forced kill service=%s", service)
            await asyncio.gather(*(self.children[x].wait() for x in remaining), return_exceptions=True)

        for service, proc in self.children.items():
            self.exit_codes[service] = proc.returncode
        for task in self.monitor_tasks.values():
            if not task.done():
                task.cancel()
        monitor_results = await asyncio.gather(*self.monitor_tasks.values(), return_exceptions=True)
        for result in monitor_results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                logger.error("monitor task failed during shutdown error=%s", result)
        if self.resource_task:
            self.resource_task.cancel()
            resource_result = await asyncio.gather(self.resource_task, return_exceptions=True)
            for result in resource_result:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    logger.error("resource monitor failed during shutdown error=%s", result)
        for service, fh in self.logs.items():
            try:
                fh.close()
            except Exception as exc:
                logger.warning("failed closing service log service=%s error=%s", service, exc)

    async def run(self) -> int:
        baseline = await self._baseline()
        meta = {
            "schema": "52.0-run",
            "run_id": self.run_id,
            "started_at": iso_now(),
            "start_epoch": time.time(),
            "services": list(self.settings.supervisor_services),
            "baseline": baseline,
            "settings": self.settings.public_dict(),
        }
        (self.run_dir / "run.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
        for service in self.settings.supervisor_services:
            await self.start_service(service)
        await self.wait_for_startup_ready()
        self.resource_task = asyncio.create_task(self.resource_loop(), name="resource-monitor")
        timer_task: asyncio.Task[Any] | None = None
        if self.run_seconds > 0 and not self.stop_event.is_set():
            async def timer() -> None:
                await asyncio.sleep(self.run_seconds)
                self.stop_event.set()
            timer_task = asyncio.create_task(timer(), name="supervisor-timer")
        stop_wait = asyncio.create_task(self.stop_event.wait(), name="supervisor-stop-wait")
        try:
            assert self.resource_task is not None
            done, _ = await asyncio.wait({stop_wait, self.resource_task}, return_when=asyncio.FIRST_COMPLETED)
            if self.resource_task in done and not self.stop_event.is_set():
                try:
                    exc = self.resource_task.exception()
                except asyncio.CancelledError:
                    exc = RuntimeError("resource monitor cancelled unexpectedly")
                self.failed_reason = f"resource monitor exited unexpectedly: {type(exc).__name__}: {exc}" if exc else "resource monitor exited unexpectedly"
                logger.critical(self.failed_reason)
                self.stop_event.set()
            await self.stop_event.wait()
        finally:
            stop_wait.cancel()
            await asyncio.gather(stop_wait, return_exceptions=True)
            if timer_task:
                timer_task.cancel()
                await asyncio.gather(timer_task, return_exceptions=True)
            await self.shutdown()
        operational = not self.failed_reason and self.started == set(self.settings.supervisor_services)
        # During intentional shutdown SIGTERM exits may be 0 or negative depending on where a service is waiting.
        # Each service writes final.json; absence/fatal is checked by diagnostics.
        report = {
            "schema": "52.0-supervisor-final",
            "run_id": self.run_id,
            "stopped_at": iso_now(),
            "operational_success": operational,
            "failed_reason": self.failed_reason,
            "started": sorted(self.started),
            "restart_counts": dict(self.restart_counts),
            "exit_codes": self.exit_codes,
            "forced_kills": self.forced_kills,
            "stale_terminations": self.stale_terminations,
            "shutdown_phases": self.shutdown_phases,
        }
        (self.run_dir / "supervisor_final.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"V52_RUN_ID={self.run_id}", flush=True)
        return 0 if operational else 1


async def _amain(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    settings.ensure_directories()
    sup = Supervisor(settings, args.run_seconds)
    configure_logging("supervisor", settings.log_dir / sup.run_id, settings.log_level)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, sup.stop_event.set)
        except NotImplementedError:
            logger.warning("event-loop signal handlers unsupported on this platform signal=%s", sig.name)
    return await sup.run()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-seconds", type=float, default=0.0)
    args = ap.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
