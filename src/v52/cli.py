from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import Settings
from .constants import APP_NAME, APP_VERSION


def main() -> int:
    ap = argparse.ArgumentParser(prog="v52", description=f"{APP_NAME} {APP_VERSION}")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("config")
    sub.add_parser("preflight")
    sup = sub.add_parser("supervisor"); sup.add_argument("--run-seconds", type=float, default=0.0)
    svc = sub.add_parser("service"); svc.add_argument("service"); svc.add_argument("--run-seconds", type=float, default=0.0)
    q = sub.add_parser("quality"); q.add_argument("run_id"); q.add_argument("--mode", choices=("full", "smoke"), default="full")
    b = sub.add_parser("bundle"); b.add_argument("run_id")
    status_choices=("summary", "qualification", "models", "signals", "events", "learning", "drift", "reports","brain-status","data-status","provider-status","asset-status","feature-status","expert-status","model-status","shadow-status","signal-status","active-signals","learning-status")
    brain = sub.add_parser("brain"); brain.add_argument("section", choices=status_choices, default="summary", nargs="?"); brain.add_argument("--limit", type=int, default=50)
    for status_name in ("brain-status","data-status","provider-status","asset-status","feature-status","expert-status","model-status","shadow-status","signal-status","learning-status","drift-status","qualification-status"):
        sub.add_parser(status_name)
    rollback = sub.add_parser("model-rollback"); rollback.add_argument("--version", default=None)
    args = ap.parse_args()
    if args.command == "config":
        settings = Settings.from_env()
        print(json.dumps(settings.public_dict(), indent=2, default=str))
        return 0
    if args.command == "preflight":
        from .diagnostics.preflight import main as fn
        sys.argv = [sys.argv[0]]
        return fn()
    if args.command == "supervisor":
        from .runtime.supervisor import main as fn
        sys.argv = [sys.argv[0], "--run-seconds", str(args.run_seconds)]
        return fn()
    if args.command == "service":
        from .runtime.service import main as fn
        sys.argv = [sys.argv[0], args.service, "--run-seconds", str(args.run_seconds)]
        return fn()
    if args.command == "quality":
        from .diagnostics.quality import main as fn
        sys.argv = [sys.argv[0], args.run_id, "--mode", args.mode]
        return fn()
    if args.command == "bundle":
        from .diagnostics.bundle import main as fn
        sys.argv = [sys.argv[0], args.run_id]
        return fn()
    if args.command == "brain":
        from .diagnostics.intelligence import main as fn
        sys.argv = [sys.argv[0], args.section, "--limit", str(args.limit)]
        return fn()
    if args.command.endswith('-status'):
        from .diagnostics.intelligence import main as fn
        mapping={'drift-status':'drift','qualification-status':'qualification'}
        sys.argv=[sys.argv[0],mapping.get(args.command,args.command)]
        return fn()
    if args.command == "model-rollback":
        import asyncio
        from .intelligence.store import BrainStore
        settings = Settings.from_env()
        result = asyncio.run(BrainStore(settings).rollback_model(args.version))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("rolled_back") else 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
