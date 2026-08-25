from __future__ import annotations

from v52.diagnostics.preflight import run_preflight


def test_preflight_config_and_reference_contract(settings) -> None:
    result=run_preflight(settings)
    # Dependency completeness may depend on the test interpreter; release validation installs requirements first.
    assert result['config']['duplicates']==[]
    assert result['config']['count']==result['config']['schema_count']
    assert result['storage']['disk_write'] is True
    assert result['references']['registry_summary']['direct'] >= 100
