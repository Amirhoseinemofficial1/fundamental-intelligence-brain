from __future__ import annotations
import argparse, json, statistics
from pathlib import Path
from benchmark_hot_path import run_benchmark

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument('--v51-root',type=Path,required=True); p.add_argument('--v52-root',type=Path,required=True)
    p.add_argument('--items',type=int,default=10000); p.add_argument('--rounds',type=int,default=4); p.add_argument('--max-regression',type=float,default=.15); a=p.parse_args()
    runs={'v51':[],'v52':[]}
    for i in range(a.rounds):
        order=('v51','v52') if i%2==0 else ('v52','v51')
        for version in order:
            root=a.v51_root if version=='v51' else a.v52_root
            runs[version].append(run_benchmark(version,root,a.items,500))
    med={v:statistics.median(x['events_per_second'] for x in xs) for v,xs in runs.items()}
    regression=1-med['v52']/med['v51']; passed=regression<=a.max_regression and all(not x['duplicate_loss'] for xs in runs.values() for x in xs)
    print(json.dumps({'schema':'v52-hot-path-comparison-1','runs':runs,'median_events_per_second':med,'v52_throughput_regression':regression,
      'max_allowed_regression':a.max_regression,'passed':passed},indent=2))
    return 0 if passed else 2
if __name__=='__main__': raise SystemExit(main())
