from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

import pytest


def test_common_rejects_colon_env_without_leaking_value(tmp_path: Path) -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash-specific deployment test")
    root = tmp_path / "pkg"
    (root / "bin").mkdir(parents=True)
    common = Path(__file__).resolve().parents[1] / "bin" / "_common.sh"
    (root / "bin" / "_common.sh").write_text(common.read_text(encoding="utf-8"), encoding="utf-8")
    secret = "123456:VERY_SECRET_TOKEN_VALUE"
    (root / ".env").write_text(f"TELEGRAM_BOT_TOKEN: {secret}\n", encoding="utf-8")
    proc = subprocess.run(["bash", "-c", f"source '{root / 'bin' / '_common.sh'}'"], text=True, capture_output=True)
    assert proc.returncode != 0
    assert "never KEY: value" in proc.stderr
    assert "TELEGRAM_BOT_TOKEN" in proc.stderr
    assert secret not in proc.stderr
