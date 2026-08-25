from __future__ import annotations

import json
from pathlib import Path

from v52.config import Settings
from v52.news.registry import SourceRegistry


def test_env_schema_unique_and_identical(settings: Settings) -> None:
    root = settings.package_root
    keys=[]
    for line in (root/'.env.example').read_text().splitlines():
        s=line.strip()
        if s and not s.startswith('#') and '=' in line:
            keys.append(line.split('=',1)[0].strip())
    assert len(keys) == len(set(keys))
    schema=json.loads((root/'config/settings.schema.json').read_text())
    skeys=[x['name'] for x in schema['settings']]
    assert keys == skeys
    assert not any(k.startswith(('V43_','V44_','V45_','V46_','V47_','V48_','V49_','V50_')) for k in keys)


def test_source_registry_has_real_coverage(settings: Settings) -> None:
    r=SourceRegistry(settings)
    summary=r.summary()
    assert summary['direct'] >= 100
    assert summary['priority'] >= 300
    assert summary['discovery_sites'] >= 1500
    assert summary['structured_providers'] >= 80
    hot={x.key for x in r.hot_sources()}
    coverage={x.key for x in r.coverage_sources()}
    assert hot
    assert hot.isdisjoint(coverage)
