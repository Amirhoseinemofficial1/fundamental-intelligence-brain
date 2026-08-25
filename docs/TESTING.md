# Testing and Release Gates

Local Level-A gates:

```bash
python -m pytest -q
python -m compileall -q src tests
bash -n bin/*.sh
python -m v52.cli preflight
```

Critical tests prove that changing Gold or Meta Champion artifacts changes runtime predictions; structured observations change point-in-time vectors and predictions; all 20 experts train/serialize/Shadow/promote/reload/infer; heuristics can be disabled; unseen Shadow outcomes govern promotion; non-crypto prices resolve learning; downtime/replay do not advance warm-up; signal states survive restart; and the full observation-to-daily-review path completes.

The final ZIP is extracted into a new directory and all applicable gates are rerun there. `MANIFEST.json` and `MANIFEST.sha256` are then independently verified.
