from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

# RELEASE_IDENTITY is excluded from the payload manifest so the identity can contain
# the final manifest hash without creating a circular self-hash. MANIFEST.sha256
# subsequently covers the identity, MANIFEST.json and every payload file.
EXCLUDED_NAMES = {"MANIFEST.json", "MANIFEST.sha256", "RELEASE_IDENTITY.json", ".coverage", "ssh_known_hosts", "server_audit.json", "rar_files.txt"}
EXCLUDED_PARTS = {".git", ".venv", "dist", "deployment", "build", ".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache", "catboost_info", "htmlcov"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".rar", ".zip", ".sqlite", ".sqlite3", ".db", ".log"}


def category(path: Path) -> str:
    head = path.parts[0] if len(path.parts) > 1 else "root"
    if head == "src": return "source"
    if head == "tests": return "test"
    if head == "docs": return "documentation"
    if head == "data": return "reference-data"
    if head in {"bin", "scripts", "systemd"}: return "operations"
    if head == "config": return "configuration"
    return "package-metadata"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _eligible(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    return (
        path.is_file()
        and path.name not in {"ssh_known_hosts","server_audit.json","rar_files.txt"}
        and not any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in relative.parts)
        and path.suffix.lower() not in EXCLUDED_SUFFIXES
    )


def payload_files(root: Path) -> list[Path]:
    files=[]
    for path in root.rglob("*"):
        if not _eligible(root,path) or path.name in EXCLUDED_NAMES: continue
        files.append(path)
    return sorted(files,key=lambda item:item.relative_to(root).as_posix())


def integrity_files(root: Path) -> list[Path]:
    files=[]
    for path in root.rglob("*"):
        if not _eligible(root,path) or path.name == "MANIFEST.sha256": continue
        files.append(path)
    return sorted(files,key=lambda item:item.relative_to(root).as_posix())


def _update_identity(root:Path,source_tree_hash:str,manifest_hash:str,payload_count:int)->None:
    path=root/"RELEASE_IDENTITY.json"
    data=json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data.update({
        "source_tree_hash":source_tree_hash,
        "manifest_hash":manifest_hash,
        "manifest_payload_files":int(payload_count),
        "built_at_utc":datetime.now(timezone.utc).isoformat(),
    })
    path.write_text(json.dumps(data,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")


def build(root: Path) -> dict:
    # Source-tree hash is a deterministic hash of per-file source hashes. It excludes
    # generated release metadata, runtime outputs and documentation-only evidence.
    source_paths=[]
    for path in payload_files(root):
        relative=path.relative_to(root)
        if path.name=="SOURCE_TREE.sha256":continue
        if relative.parts[0] in {"src","tests","config","bin","scripts","systemd","tools"} or (len(relative.parts)==1 and (relative.name.startswith("requirements") or relative.name=="pyproject.toml")):
            source_paths.append(path)
    source_lines=[f"{digest(path)}  {path.relative_to(root).as_posix()}" for path in source_paths]
    source_bytes=("\n".join(source_lines)+"\n").encode()
    (root/"SOURCE_TREE.sha256").write_bytes(source_bytes)
    source_tree_hash=hashlib.sha256(source_bytes).hexdigest()

    entries=[]
    for path in payload_files(root):
        relative=path.relative_to(root)
        entries.append({"path":relative.as_posix(),"size":path.stat().st_size,"sha256":digest(path),"category":category(relative)})
    document={
        "schema":"v57-final-closure-release-manifest-2",
        "self_exclusion":["MANIFEST.json","MANIFEST.sha256","RELEASE_IDENTITY.json"],
        "files":entries,
    }
    manifest_path=root/"MANIFEST.json"
    manifest_path.write_text(json.dumps(document,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    manifest_hash=digest(manifest_path)
    _update_identity(root,source_tree_hash,manifest_hash,len(entries))

    lines=[f"{digest(path)}  {path.relative_to(root).as_posix()}" for path in integrity_files(root)]
    (root/"MANIFEST.sha256").write_text("\n".join(lines)+"\n",encoding="utf-8",newline="\n")
    return {
        "files":len(entries),"bytes":sum(item["size"] for item in entries),"sha256_entries":len(lines),
        "source_tree_files":len(source_paths),"source_tree_hash":source_tree_hash,"manifest_hash":manifest_hash,
    }


def verify(root: Path) -> dict:
    errors=[]
    manifest_path=root/"MANIFEST.json"
    identity_path=root/"RELEASE_IDENTITY.json"
    sidecar_path=root/"MANIFEST.sha256"
    source_tree_path=root/"SOURCE_TREE.sha256"
    if not manifest_path.is_file():return {"ok":False,"files":0,"errors":["missing:MANIFEST.json"]}
    document=json.loads(manifest_path.read_text(encoding="utf-8"))
    expected={item["path"]:item for item in document.get("files",[])}
    actual={path.relative_to(root).as_posix() for path in payload_files(root)}
    for extra in sorted(actual-set(expected)):errors.append(f"unlisted:{extra}")
    for missing in sorted(set(expected)-actual):errors.append(f"manifest-only:{missing}")
    for name,item in expected.items():
        path=root/name
        if not path.is_file():errors.append(f"missing:{name}");continue
        if path.stat().st_size!=int(item["size"]):errors.append(f"size:{name}")
        if digest(path)!=item["sha256"]:errors.append(f"sha256:{name}")

    if not identity_path.is_file():errors.append("missing:RELEASE_IDENTITY.json")
    else:
        identity=json.loads(identity_path.read_text(encoding="utf-8"))
        if identity.get("manifest_hash")!=digest(manifest_path):errors.append("identity_manifest_hash")
        if not source_tree_path.is_file():errors.append("missing:SOURCE_TREE.sha256")
        elif identity.get("source_tree_hash")!=digest(source_tree_path):errors.append("identity_source_tree_hash")
        if int(identity.get("manifest_payload_files",-1))!=len(expected):errors.append("identity_manifest_payload_files")

    if not sidecar_path.is_file():errors.append("missing:MANIFEST.sha256")
    else:
        recorded={}
        for line in sidecar_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():continue
            sha_token,sep,name=line.partition("  ")
            if not sep or len(sha_token)!=64:errors.append(f"manifest_sha_line:{line[:120]}");continue
            recorded[name]=sha_token
        integrity={path.relative_to(root).as_posix():digest(path) for path in integrity_files(root)}
        if set(recorded)!=set(integrity):errors.append(f"manifest_sha_set:extra={sorted(set(recorded)-set(integrity))}:missing={sorted(set(integrity)-set(recorded))}")
        for name,sha in integrity.items():
            if recorded.get(name)!=sha:errors.append(f"manifest_sha_mismatch:{name}")

    return {"ok":not errors,"files":len(expected),"errors":errors,"manifest_hash":digest(manifest_path)}


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[1]);parser.add_argument("--verify",action="store_true")
    args=parser.parse_args();root=args.root.resolve();result=verify(root) if args.verify else build(root);print(json.dumps(result,indent=2));return 0 if result.get("ok",True) else 2


if __name__=="__main__":raise SystemExit(main())
