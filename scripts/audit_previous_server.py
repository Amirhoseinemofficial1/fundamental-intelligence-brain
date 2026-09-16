import json,tarfile,collections
from pathlib import Path
p=Path('/root/v57-prepatch-diagnostics.tar.gz')
counts=collections.Counter();findings=collections.Counter();files=0
with tarfile.open(p,'r:gz') as tar:
    for m in tar:
        if not m.isfile() or not m.name.endswith(('.json','.jsonl')):continue
        files+=1
        f=tar.extractfile(m)
        for line in f:
            try:obj=json.loads(line)
            except Exception:continue
            name=Path(m.name).name
            counts[name]+=1
            text=json.dumps(obj).lower()
            for marker in ('out of memory','memoryerror','database is locked','traceback','checksum mismatch','failed','429','403','401','no such table','train_specialists','train_decisions'):
                if marker in text:findings[name+':'+marker]+=1
print(json.dumps({'files_reviewed':files,'records':dict(counts),'error_stage_counts':dict(findings)},indent=2))
