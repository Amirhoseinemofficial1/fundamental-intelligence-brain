# Signals, Entry and Exit

The validated lifecycle is:

```text
CREATED -> QUALIFIED -> SENT -> ENTRY_PENDING -> ACTIVE
        -> UPDATED -> EXIT_PENDING -> EXITED -> POST_EXIT
        -> EVALUATED -> LEARNED
```

`INVALIDATED` is terminal from pre-exit states. Illegal skips raise an error and each accepted transition is persisted in `signal_lifecycle`, so a restarted process continues from database state.

The Meta artifact controls probability. Uncertainty, agreement, feature quality, qualification and risk create a real `NO_TRADE` outcome. Champion `entry` and `exit` artifacts drive entry style and exit probability when available; an explicitly named bootstrap fallback is used before training. Outcomes generate MFE, MAE, timing, reversal, post-exit and failure evidence.
