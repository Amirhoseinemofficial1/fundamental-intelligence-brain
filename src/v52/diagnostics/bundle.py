from __future__ import annotations

import argparse
import hashlib
import json
import os
import tarfile
from pathlib import Path
from typing import Any

from ..config import Settings
from ..loggingx import redact_text
from ..timeutil import iso_now


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(settings: Settings, run_id: str) -> Path:
    run_dir = settings.run_dir / run_id
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run not found: {run_dir}")
    out_dir = settings.data_dir / "diagnostic_bundles"
    out_dir.mkdir(parents=True, exist_ok=True)
    stage = out_dir / f".stage-{run_id}"
    if stage.exists():
        import shutil
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    import shutil
    shutil.copytree(run_dir, stage / "run", dirs_exist_ok=True)
    structured_logs = settings.log_dir / run_id
    if structured_logs.is_dir():
        shutil.copytree(structured_logs, stage / "structured_logs", dirs_exist_ok=True)
    # Copy only redacted public config and durable queue summaries, never the populated .env.
    (stage / "public_settings.json").write_text(json.dumps(settings.public_dict(), indent=2, default=str), encoding="utf-8")
    manifests: list[dict[str, Any]] = []
    for path in sorted(p for p in stage.rglob("*") if p.is_file()):
        # Service logs are already centrally redacted; run a second textual redaction pass defensively.
        redaction = "not_applicable"
        if path.suffix in {".log", ".json", ".jsonl", ".txt"} and path.stat().st_size <= 50_000_000:
            try:
                text = path.read_text(encoding="utf-8")
                redacted = redact_text(text)
                redaction = "checked"
                if redacted != text:
                    path.write_text(redacted, encoding="utf-8")
                    redaction = "redacted"
            except UnicodeDecodeError:
                redaction = "skipped_non_utf8"
        manifests.append({"path": str(path.relative_to(stage)), "bytes": path.stat().st_size, "sha256": sha256(path), "redaction": redaction})
    manifest = {"schema": "52.0-diagnostic-bundle", "at": iso_now(), "run_id": run_id, "files": manifests}
    (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifests.append({"path": "MANIFEST.json", "bytes": (stage / "MANIFEST.json").stat().st_size, "sha256": sha256(stage / "MANIFEST.json")})
    bundle = out_dir / f"V52_DIAGNOSTIC_BUNDLE_{run_id}.tar.gz"
    with tarfile.open(bundle, "w:gz", compresslevel=9) as tf:
        tf.add(stage, arcname=f"V52_DIAGNOSTIC_{run_id}")
    digest = sha256(bundle)
    bundle.with_suffix(bundle.suffix + ".sha256").write_text(f"{digest}  {bundle.name}\n", encoding="utf-8")
    shutil.rmtree(stage)
    return bundle


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    args = ap.parse_args()
    settings = Settings.from_env()
    try:
        path = collect(settings, args.run_id)
        print(path)
        return 0
    except BaseException as exc:
        print(f"diagnostic bundle failed: {type(exc).__name__}: {exc}", file=os.sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
