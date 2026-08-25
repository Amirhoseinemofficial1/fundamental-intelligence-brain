from __future__ import annotations

from pathlib import Path
import pytest

from v52.config import Settings


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("V52_PACKAGE_ROOT", str(root))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TELEGRAM_ENABLED", "0")
    monkeypatch.setenv("TRANSLATION_ENABLED", "0")
    monkeypatch.setenv("NEWS_ENRICHMENT_ENABLED", "0")
    monkeypatch.setenv("STRUCTURED_ENABLED", "0")
    monkeypatch.setenv("DISCOVERY_ENABLED", "0")
    monkeypatch.setenv("MARKET_ENABLED", "1")
    s = Settings.from_env()
    s.ensure_directories()
    return s
