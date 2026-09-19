from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_verifier():
    spec = importlib.util.spec_from_file_location("v57_verifier", ROOT / "scripts" / "verify_v57_completion.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unified_ledger_covers_all_historical_requirements_and_v57_closures():
    ledger = json.loads((ROOT / "artifacts" / "V57_EXECUTION_LEDGER.json").read_text(encoding="utf-8"))
    records = ledger["records"]
    hist = {r["requirement_id"] for r in records if r["requirement_id"].startswith("HIST-")}
    assert ledger["historical_requirement_count"] == 201
    assert hist == {f"HIST-{i:03d}" for i in range(201)}
    assert {f"V57-C{i:02d}" for i in range(1, 13)} <= {r["requirement_id"] for r in records}
    assert not [r for r in records if r["level"] == "A" and r["status"] in {"FAILED", "PENDING", "IN_PROGRESS", "LIVE_EXTERNAL_VALIDATION_PENDING"}]


def test_release_verifier_requires_real_artifact_for_final_mode():
    verifier = _load_verifier()
    errors: list[str] = []
    verifier.verify_identity(errors, None, True)
    assert "final_mode_requires_artifact" in errors


def test_release_verifier_hashes_real_zip_and_rejects_nonzip(tmp_path: Path):
    verifier = _load_verifier()
    fake = tmp_path / "V57_TRUE_AUTONOMOUS_MULTI_ASSET_FUNDAMENTAL_INTELLIGENCE_BRAIN_ABSOLUTE_FINAL.zip"
    fake.write_bytes(b"not-a-zip")
    errors: list[str] = []
    sha = verifier.verify_identity(errors, fake, True)
    assert sha == hashlib.sha256(b"not-a-zip").hexdigest()
    assert "artifact_not_zip" in errors


def test_debian_installer_provisions_prerequisites_and_canonical_paths():
    text = (ROOT / "scripts" / "install_v57_debian.sh").read_text(encoding="utf-8")
    for token in ("python3-venv", "ca-certificates", "build-essential", "procps", "util-linux"):
        assert token in text
    assert "/opt/v57-fundamental" in text
    assert "/var/lib/v57-fundamental" in text
    assert "/etc/v57-fundamental.env" in text
    assert "systemctl enable v57-fundamental.service" in text
    service = (ROOT / "systemd" / "v57-fundamental.service").read_text(encoding="utf-8")
    assert "WorkingDirectory=/opt/v57-fundamental" in service
    assert "EnvironmentFile=/etc/v57-fundamental.env" in service
    assert "ReadWritePaths=/var/lib/v57-fundamental" in service


def test_v57_shell_env_loader_is_used_by_normal_run_path(tmp_path: Path):
    # Use system python rather than requiring the project venv. The run script's
    # check-env mode only needs Python and must exercise the same env loader used
    # by normal startup.
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_V57_ENV_VALUE=known-value\n", encoding="utf-8")
    env = os.environ.copy()
    env["V57_ENV_FILE"] = str(env_file)
    env["V57_PYTHON"] = os.sys.executable
    bash = shutil.which("bash")
    if not bash:
        import pytest
        pytest.skip("bash is validated by the native Debian release suite")
    result = subprocess.run(
        [bash, str(ROOT / "scripts" / "run_v57.sh"), "--check-env", "TEST_V57_ENV_VALUE"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout
    assert result.stdout.strip() == "known-value"
