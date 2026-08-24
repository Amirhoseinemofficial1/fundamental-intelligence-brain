from __future__ import annotations

from pathlib import Path


def test_no_legacy_runtime_files_or_placeholders(settings) -> None:
    root=settings.package_root
    names=[p.name for p in (root/'src').rglob('*.py')]
    forbidden=('v43_','v44_','v45_','v46_','v47_','v48_','v49_','v50_')
    assert not any(n.lower().startswith(forbidden) for n in names)
    for p in (root/'src').rglob('*.py'):
        text=p.read_text(encoding='utf-8')
        import re
        assert re.search(r'\bTODO\b', text) is None
        assert 'FIXME' not in text
        assert 'PLACEHOLDER' not in text.upper()
