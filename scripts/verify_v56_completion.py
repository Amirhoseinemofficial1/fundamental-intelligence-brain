from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

OLD_SHA="cabc46472463869d6fb71b785c5196d9ca327eefb385bcded8b5813d93f64694"
ALLOWED={"IMPLEMENTED_AND_RUNTIME_VERIFIED","IMPLEMENTED_AND_FIXTURE_VERIFIED","LIVE_EXTERNAL_VALIDATION_PENDING","NOT_APPLICABLE_WITH_TECHNICAL_REASON"}
REQUIRED_FIELDS={"requirement_id","requirement","level","current_behavior","required_behavior","source_files","runtime_entrypoint","test_files","test_command","expected_evidence","actual_evidence","status"}

def digest(path:Path)->str:
 h=hashlib.sha256()
 with path.open('rb') as stream:
  for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
 return h.hexdigest()

def main()->int:
 root=Path(__file__).resolve().parents[1];errors=[]
 required=["RELEASE_IDENTITY.json","MANIFEST.json","MANIFEST.sha256","SOURCE_TREE.sha256","README.md","requirements-full.txt","docs/V56_EXECUTION_LEDGER.md","docs/V56_BLOCKING_DEFECT_CLOSURE.md","artifacts/V56_EXECUTION_LEDGER.json","artifacts/V56_TEST_SUMMARY.json"]
 for name in required:
  if not (root/name).is_file():errors.append(f"required_release_file_missing:{name}")
 ledger_path=root/"artifacts/V56_EXECUTION_LEDGER.json"
 records=[]
 if ledger_path.is_file():
  ledger=json.loads(ledger_path.read_text(encoding='utf-8'));records=ledger.get('records') or []
  ids=[r.get('requirement_id') for r in records]
  if len(ids)!=len(set(ids)):errors.append('duplicate_requirement_id')
  for record in records:
   missing=REQUIRED_FIELDS-set(record)
   if missing:errors.append(f"ledger_fields:{record.get('requirement_id')}:{sorted(missing)}");continue
   if record['status'] not in ALLOWED:errors.append(f"invalid_status:{record['requirement_id']}:{record['status']}")
   if record['level']=='A':
    if record['status'] in {'PENDING','IN_PROGRESS','FAILED','LIVE_EXTERNAL_VALIDATION_PENDING'}:errors.append(f"level_a_not_complete:{record['requirement_id']}")
    if not record['actual_evidence'] or not record['runtime_entrypoint'] or not record['test_files']:errors.append(f"level_a_evidence_missing:{record['requirement_id']}")
    for name in record['source_files']+record['test_files']:
     if not (root/name).is_file():errors.append(f"ledger_path_missing:{record['requirement_id']}:{name}")
    runtime=str(record['runtime_entrypoint']).split('#',1)[0]
    if runtime and not (root/runtime).is_file():errors.append(f"runtime_wiring_missing:{record['requirement_id']}:{runtime}")
 identity_path=root/"RELEASE_IDENTITY.json"
 if identity_path.is_file():
  identity=json.loads(identity_path.read_text(encoding='utf-8'))
  if identity.get('release')!='V56_ABSOLUTE_RUN_READY':errors.append('release_identity_version')
  if identity.get('previous_artifact_sha256')!=OLD_SHA:errors.append('previous_artifact_identity')
  if identity.get('final_artifact_sha256') in {None,"",OLD_SHA}:errors.append('new_artifact_identity')
  if int(identity.get('level_a_failed',-1))!=0:errors.append('identity_level_a_failed')
 manifest_path=root/"MANIFEST.json"
 if manifest_path.is_file():
  manifest=json.loads(manifest_path.read_text(encoding='utf-8'));expected={}
  for item in manifest.get('files') or []:expected[item['path']]=item
  for name,item in expected.items():
   path=root/name
   if not path.is_file():errors.append(f"manifest_missing:{name}")
   elif path.stat().st_size!=item['size'] or digest(path)!=item['sha256']:errors.append(f"manifest_mismatch:{name}")
  excluded={'.git','.pytest_cache','__pycache__','.mypy_cache','.ruff_cache'};suffixes={'.pyc','.pyo','.zip','.sqlite','.sqlite3','.db','.log'}
  actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and not any(x in excluded for x in p.relative_to(root).parts) and p.suffix.lower() not in suffixes and p.name not in {'MANIFEST.json','MANIFEST.sha256'}}
  if actual!=set(expected):errors.append(f"manifest_file_set:extra={sorted(actual-set(expected))}:missing={sorted(set(expected)-actual)}")
 summary_path=root/"artifacts/V56_TEST_SUMMARY.json"
 if summary_path.is_file():
  summary=json.loads(summary_path.read_text(encoding='utf-8'))
  for key in ('compileall','full_pytest','manifest_verify','reextracted','clean_install','start_stop_restart','clean_exit'):
   if summary.get(key)!='PASS':errors.append(f"test_evidence_not_pass:{key}")
 failed=sum(r.get('level')=='A' and r.get('status')=='FAILED' for r in records);pending=sum(r.get('level')=='A' and r.get('status')=='PENDING' for r in records);progress=sum(r.get('level')=='A' and r.get('status')=='IN_PROGRESS' for r in records)
 print(f"LEVEL_A_FAILED={failed}");print(f"LEVEL_A_PENDING={pending}");print(f"LEVEL_A_IN_PROGRESS={progress}")
 if errors:
  print("V56_COMPLETION_GATE=FAIL");print(json.dumps(errors,indent=2));return 2
 print("V56_COMPLETION_GATE=PASS");return 0

if __name__=='__main__':raise SystemExit(main())
