from __future__ import annotations

import json
import re
from pathlib import Path


def audit(root: Path) -> dict:
    src = root / "src" / "v52"
    service_text = (src / "runtime" / "service.py").read_text(encoding="utf-8")
    constants: dict[str, object] = {}
    exec((src / "constants.py").read_text(encoding="utf-8"), constants)
    services = list(constants["DEFAULT_SERVICES"])
    missing_services = [name for name in services if name not in service_text and not name.startswith("market_")]
    all_text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in src.rglob("*.py"))
    store_text = (src / "intelligence" / "store.py").read_text(encoding="utf-8")
    tables = sorted(set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-zA-Z0-9_]+)", store_text)))
    table_occurrences = {table: len(re.findall(rf"\b{re.escape(table)}\b", all_text)) for table in tables}
    orphan_tables = [table for table, count in table_occurrences.items() if count < 2]
    required_runtime = {
        "active_signal_monitor": "ActiveSignalMonitorService",
        "temporal_shadow_runtime": "temporal_meta:%",
        "regime_governance": "RegimeGovernance",
        "dex_market": "DexContractMarketProvider",
        "telegram_producers": "TelegramIntelligenceProducers",
    }
    missing_runtime = [name for name, token in required_runtime.items() if token not in all_text]
    return {"ok": not (missing_services or orphan_tables or missing_runtime), "services": len(services),
        "missing_services": missing_services, "tables": len(tables), "orphan_tables": orphan_tables,
        "missing_runtime_components": missing_runtime, "table_occurrences": table_occurrences}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    result = audit(root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
