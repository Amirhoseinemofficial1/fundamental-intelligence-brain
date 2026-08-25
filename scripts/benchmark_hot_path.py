from __future__ import annotations

import argparse
import importlib
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path


def run_benchmark(module: str, package_root: Path, item_count: int, batch_size: int) -> dict:
    source_root = str((package_root / "src").resolve())
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    os.environ[f"{module.upper()}_PACKAGE_ROOT"] = str(package_root.resolve())
    os.environ["TELEGRAM_ENABLED"] = "0"
    os.environ["TRANSLATION_ENABLED"] = "0"
    os.environ["NEWS_ENRICHMENT_ENABLED"] = "0"
    with tempfile.TemporaryDirectory(prefix=f"{module}-hot-path-") as td:
        os.environ["DATA_DIR"] = td
        Settings = importlib.import_module(f"{module}.config").Settings
        RawNewsItem = importlib.import_module(f"{module}.models").RawNewsItem
        NewsStore = importlib.import_module(f"{module}.storage.news").NewsStore
        settings = Settings.from_env(); settings.ensure_directories(); store = NewsStore(settings)
        items = [RawNewsItem(raw_key=f"benchmark:{i}", source_key="benchmark", source_name="Benchmark", source_url="https://example.com/feed",
            item_url=f"https://example.com/{i}", title=f"Federal Reserve benchmark event {i}", summary="Deterministic hot-path fixture",
            category="ECONOMY", region="US", language="en", published_at="2026-08-25T00:00:00Z", observed_at="2026-08-25T00:00:01Z",
            priority=10, payload={"fixture": True, "sequence": i}) for i in range(item_count)]
        latencies=[]; inserted=0; started=time.perf_counter()
        for offset in range(0, len(items), batch_size):
            before=time.perf_counter(); inserted += store._insert_raw_many_sync(items[offset:offset+batch_size]); latencies.append(time.perf_counter()-before)
        elapsed=time.perf_counter()-started; ordered=sorted(latencies); p95=ordered[min(len(ordered)-1,int(len(ordered)*.95))]
        return {"schema":"v52-hot-path-benchmark-1","module":module,"items":item_count,"inserted":inserted,"batch_size":batch_size,
            "elapsed_seconds":elapsed,"events_per_second":inserted/max(elapsed,1e-9),"batch_latency_p50_ms":statistics.median(latencies)*1000,
            "batch_latency_p95_ms":p95*1000,"duplicate_loss":inserted!=item_count,"python":os.sys.version.split()[0]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Reproducible raw-news persistence benchmark for V51/V52")
    parser.add_argument("--module", choices=("v51", "v52"), required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--items", type=int, default=20000)
    parser.add_argument("--batch", type=int, default=500)
    args = parser.parse_args()
    result = run_benchmark(args.module, args.package_root, args.items, args.batch)
    print(json.dumps(result, indent=2))
    return 0 if not result["duplicate_loss"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
