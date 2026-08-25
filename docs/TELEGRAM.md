# Telegram Intelligence Contract

Telegram is intelligence-only and uses the durable alert queue. Supported classes are:

1. final signal
2. active update
3. exit
4. invalidation
5. pump warning
6. dump warning
7. important fundamental intelligence
8. validated learning update
9. daily learning report
10. critical system warning

Final signals include market, direction/action, entry type, quality/confidence, regime, drivers, controlling Champion, signal ID and time. Delivery retries are durable and secrets are redacted. `tests/test_v52_telegram_intelligence_formats.py` validates all ten render paths.
