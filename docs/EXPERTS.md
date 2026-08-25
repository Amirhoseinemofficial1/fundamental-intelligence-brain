# Trainable Experts

The 20 specialists are `macro`, `central_bank`, `gold`, `oil`, `forex`, `equity`, `crypto`, `token`, `etf`, `regulation`, `geopolitical`, `energy`, `news_novelty`, `onchain`, `derivatives`, `whale`, `liquidity`, `regime`, `pump_risk`, and `dump_risk`.

Every specialist uses `TrainableExpertLifecycle`: build/validate dataset, select specialist features, train candidates, evaluate, calibrate, serialize, register Shadow, infer on the live stream, resolve outcomes, promote/reject, rollback, reload after restart and infer from the Champion. `tests/test_true_expert_matrix.py` executes this lifecycle separately for all 20. `entry` and `exit` are additional trainable policy artifacts, not counted among the 20 specialists.

`build_votes` remains clearly labeled bootstrap fallback. With fallback disabled, artifact-backed specialists and the Meta Champion still produce predictions; this is a mandatory test.
