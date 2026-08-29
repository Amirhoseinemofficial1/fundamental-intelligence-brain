from __future__ import annotations

import json
import os
import time

from v52.runtime.supervisor import _status_pid_matches, heartbeat_issue


def _status(path, service: str, pid: int, run_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "service": service,
        "pid": pid,
        "gauges": {"run_id": run_id},
    }), encoding="utf-8")


def test_fresh_heartbeat_is_accepted(tmp_path) -> None:
    p = tmp_path / "status.json"
    now = time.time()
    _status(p, "news_hot", os.getpid(), "run-x")
    assert heartbeat_issue(p, service="news_hot", pid=os.getpid(), run_id="run-x",
                           started_epoch=now - 60, now_epoch=now, stale_seconds=30) is None


def test_wrong_run_identity_is_rejected(tmp_path) -> None:
    p = tmp_path / "status.json"
    now = time.time()
    _status(p, "news_hot", os.getpid(), "old-run")
    assert heartbeat_issue(p, service="news_hot", pid=os.getpid(), run_id="run-x",
                           started_epoch=now - 60, now_epoch=now, stale_seconds=30) == "run_identity_mismatch"


def test_stale_heartbeat_is_rejected(tmp_path) -> None:
    p = tmp_path / "status.json"
    now = time.time()
    _status(p, "news_hot", os.getpid(), "run-x")
    old = now - 120
    os.utime(p, (old, old))
    issue = heartbeat_issue(p, service="news_hot", pid=os.getpid(), run_id="run-x",
                            started_epoch=now - 180, now_epoch=now, stale_seconds=30)
    assert issue and issue.startswith("stale_status:")


def test_windows_venv_launcher_identity_requires_exact_supervisor_parent() -> None:
    status = {"pid": 222, "extra": {"parent_pid": 333}}
    assert _status_pid_matches(status, 111, supervisor_pid=333, allow_launcher_wrapper=True)
    assert not _status_pid_matches(status, 111, supervisor_pid=444, allow_launcher_wrapper=True)
    assert not _status_pid_matches(status, 111, supervisor_pid=333, allow_launcher_wrapper=False)
