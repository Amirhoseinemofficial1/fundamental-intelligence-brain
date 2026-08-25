from __future__ import annotations

import importlib
import pkgutil
import v52


def test_every_v52_module_imports() -> None:
    failed=[]
    for m in pkgutil.walk_packages(v52.__path__, v52.__name__+'.'):
        try: importlib.import_module(m.name)
        except Exception as exc: failed.append((m.name,type(exc).__name__,str(exc)))
    assert failed == []
