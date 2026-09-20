from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V56_SHA = "b6a060328b04b7629de7ff65e6093d71334b143f2e309f4950e560c1daeca3be"
PREVIOUS_RELEASE_SHA = "e762521319fe9374614fc4f7e924eb350570ba44ba1002b401753f66c1749749"
ALLOWED = {
    "IMPLEMENTED_AND_RUNTIME_VERIFIED",
    "IMPLEMENTED_AND_FIXTURE_VERIFIED",
    "LIVE_EXTERNAL_VALIDATION_PENDING",
    "NOT_APPLICABLE_WITH_TECHNICAL_REASON",
}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(command: list[str], *, env: dict[str, str] | None = None, timeout: int = 900) -> tuple[bool, str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    try:
        cp = subprocess.run(command, cwd=ROOT, env=merged, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=timeout, check=False)
        return cp.returncode == 0, cp.stdout[-12000:]
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "")
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        return False, f"TIMEOUT after {timeout}s\n{out[-8000:]}"


def verify_manifest(errors: list[str]) -> None:
    # Use the same deterministic verifier that builds the release manifest so the
    # final gate cannot silently drift from packaging rules.
    ok, out = run([sys.executable, "scripts/build_manifest.py", "--verify"], env={"PYTHONPATH": str(ROOT / "src")}, timeout=180)
    if not ok:
        errors.append("manifest_verification_failed")
        errors.append("manifest_verifier_output:" + out[-4000:].replace("\n", " | "))

def verify_ledger(errors: list[str]) -> tuple[int, int, int, int]:
    path = ROOT / "artifacts" / "V57_EXECUTION_LEDGER.json"
    if not path.is_file():
        errors.append("v57_ledger_missing")
        return 0, 0, 0, 0
    doc = json.loads(path.read_text(encoding="utf-8"))
    records = doc.get("records") or []
    if int(doc.get("historical_requirement_count", -1)) != 201:
        errors.append(f"historical_requirement_count:{doc.get('historical_requirement_count')}")
    hist = [r for r in records if str(r.get("requirement_id", "")).startswith("HIST-")]
    if len(hist) != 201 or {r["requirement_id"] for r in hist} != {f"HIST-{i:03d}" for i in range(201)}:
        errors.append(f"historical_requirement_coverage:{len(hist)}")
    ids = [r.get("requirement_id") for r in records]
    if len(ids) != len(set(ids)):
        errors.append("duplicate_requirement_ids")
    failed = pending = progress = 0
    for r in records:
        status = r.get("status")
        level = r.get("level")
        if status not in ALLOWED:
            errors.append(f"invalid_status:{r.get('requirement_id')}:{status}")
        if level == "A":
            failed += status == "FAILED"
            pending += status == "PENDING"
            progress += status == "IN_PROGRESS"
            if status in {"FAILED", "PENDING", "IN_PROGRESS", "LIVE_EXTERNAL_VALIDATION_PENDING"}:
                errors.append(f"level_a_not_complete:{r.get('requirement_id')}:{status}")
            for field in ("requirement", "source_files", "runtime_entrypoint", "test_files", "test_command", "actual_evidence"):
                if not r.get(field):
                    errors.append(f"level_a_evidence_missing:{r.get('requirement_id')}:{field}")
            # Historical rows deliberately point to full-suite reconciliation; current rows must point to real files.
            if not str(r.get("requirement_id", "")).startswith("HIST-"):
                for name in r.get("source_files", []) + r.get("test_files", []):
                    if not (ROOT / name).exists():
                        errors.append(f"ledger_path_missing:{r.get('requirement_id')}:{name}")
                entry = str(r.get("runtime_entrypoint", "")).split("#", 1)[0]
                if entry and not (ROOT / entry).exists():
                    errors.append(f"runtime_entrypoint_missing:{r.get('requirement_id')}:{entry}")
    return failed, pending, progress, len(records)


def verify_identity(errors: list[str], artifact: Path | None, require_artifact: bool) -> str | None:
    path = ROOT / "RELEASE_IDENTITY.json"
    if not path.is_file():
        errors.append("release_identity_missing")
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("release") != "V59_IMPACT_ROUTED_DATA_BRAIN":
        errors.append(f"release_identity:{data.get('release')}")
    if data.get("release_version") != "59.0.0":
        errors.append(f"release_version:{data.get('release_version')}")
    if data.get("previous_artifact_sha256") != PREVIOUS_RELEASE_SHA:
        errors.append("previous_artifact_sha_mismatch")
    if int(data.get("schema_version", -1)) != 10:
        errors.append(f"schema_version:{data.get('schema_version')}")
    manifest_path = ROOT / "MANIFEST.json"
    source_tree_path = ROOT / "SOURCE_TREE.sha256"
    if not manifest_path.is_file() or data.get("manifest_hash") != digest(manifest_path):
        errors.append("release_identity_manifest_hash_mismatch")
    if not source_tree_path.is_file() or data.get("source_tree_hash") != digest(source_tree_path):
        errors.append("release_identity_source_tree_hash_mismatch")
    if require_artifact and artifact is None:
        errors.append("final_mode_requires_artifact")
        return None
    if artifact is None:
        return None
    if not artifact.is_file():
        errors.append(f"artifact_missing:{artifact}")
        return None
    sha = digest(artifact)
    if sha == V56_SHA:
        errors.append("artifact_equals_v56_baseline")
    expected_name = data.get("final_artifact_filename")
    if expected_name and artifact.name != expected_name:
        errors.append(f"artifact_filename:{artifact.name}!={expected_name}")
    if artifact.suffix.lower() == ".rar":
        if not artifact.read_bytes()[:8].startswith(b"Rar!\x1a\x07"):
            errors.append("artifact_not_rar")
    elif not zipfile.is_zipfile(artifact):
        errors.append("artifact_not_zip")
    else:
        with zipfile.ZipFile(artifact) as zf:
            bad = zf.testzip()
            if bad:
                errors.append(f"artifact_zip_crc:{bad}")
            names = set(zf.namelist())
            # Support package either rooted or flat, but the release identity must be present exactly once.
            identity_names = [n for n in names if n.endswith("/RELEASE_IDENTITY.json") or n == "RELEASE_IDENTITY.json"]
            if len(identity_names) != 1:
                errors.append(f"artifact_release_identity_count:{len(identity_names)}")
    sidecar = artifact.with_name(artifact.name + ".sha256")
    if sidecar.is_file():
        token = sidecar.read_text(encoding="utf-8").strip().split()[0]
        if token != sha:
            errors.append("artifact_sidecar_sha_mismatch")
    return sha


def execute_full(errors: list[str], outputs: dict[str, str]) -> None:
    # Offline release verification must validate the package itself without requiring
    # live Telegram credentials or Internet. Credentialed/native-live validation is
    # kept as an explicit external gate.
    env = {
        "PYTHONPATH": str(ROOT / "src"),
        "TELEGRAM_ENABLED": "0",
        "PREFLIGHT_STRICT_NETWORK": "0",
    }
    checks = [
        ("compileall", [sys.executable, "-m", "compileall", "-q", "src", "tests"], 180),
        ("pytest", [sys.executable, "-m", "pytest", "-q", "tests"], 1200),
        ("v57_closure", [sys.executable, "-m", "pytest", "-q", "tests/test_v57_final_closure.py", "tests/test_v57_release_verifier.py", "tests/test_v57_graceful_shutdown.py", "tests/test_shutdown_release_contracts.py"], 300),
        ("preflight", [sys.executable, "-m", "v52.diagnostics.preflight"], 180),
        ("orphan_audit", [sys.executable, "scripts/orphan_audit.py"], 180),
    ]
    for name, cmd, timeout in checks:
        ok, out = run(cmd, env=env, timeout=timeout)
        outputs[name] = out
        if not ok:
            errors.append(f"execution_failed:{name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Independent V57 release completion gate")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--full", action="store_true", help="Execute compileall, entire pytest suite, targeted closure, preflight and orphan audit")
    parser.add_argument("--final", action="store_true", help="Require both --full and a real final ZIP artifact")
    args = parser.parse_args()
    errors: list[str] = []
    if args.final and not args.full:
        errors.append("final_mode_requires_full_execution")
    required = [
        "RELEASE_IDENTITY.json", "MANIFEST.json", "MANIFEST.sha256", "SOURCE_TREE.sha256", "README.md",
        "requirements-full.txt", "docs/REQUIREMENT_MATRIX.md", "docs/V57_EXECUTION_LEDGER.md",
        "docs/V57_FINAL_REQUIREMENT_MATRIX.md", "artifacts/V57_EXECUTION_LEDGER.json",
        "scripts/build_v57_ledger.py", "scripts/verify_v57_completion.py", "tests/test_v57_final_closure.py",
        "tests/test_v57_release_verifier.py", "tests/test_v57_graceful_shutdown.py", "scripts/install_v57_debian.sh", "systemd/v57-fundamental.service",
    ]
    for name in required:
        if not (ROOT / name).is_file():
            errors.append(f"required_file_missing:{name}")
    failed, pending, progress, count = verify_ledger(errors)
    verify_manifest(errors)
    artifact_sha = verify_identity(errors, args.artifact.resolve() if args.artifact else None, args.final)
    outputs: dict[str, str] = {}
    if args.full:
        execute_full(errors, outputs)
    print(f"UNIFIED_REQUIREMENTS={count}")
    print(f"LEVEL_A_FAILED={failed}")
    print(f"LEVEL_A_PENDING={pending}")
    print(f"LEVEL_A_IN_PROGRESS={progress}")
    if artifact_sha:
        print(f"FINAL_ARTIFACT_SHA256={artifact_sha}")
    for name, output in outputs.items():
        tail = " | ".join(line.strip() for line in output.splitlines()[-3:] if line.strip())
        print(f"EXEC_{name.upper()}={'PASS' if not any(e == f'execution_failed:{name}' for e in errors) else 'FAIL'} {tail}")
    if errors:
        print("V57_COMPLETION_GATE=FAIL")
        print(json.dumps(errors, indent=2))
        return 2
    print("V57_COMPLETION_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
