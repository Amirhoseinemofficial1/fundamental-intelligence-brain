# Backtest

`BacktestEngine` evaluates a loaded artifact against only feature vectors created before their outcomes were measured. It reports samples, Brier score, expected calibration error, expected value, maximum drawdown and a leakage-safety assertion.

`WalkForwardEngine` creates chronological expanding training windows with purge time and row embargo before each validation block. Backtests are research evidence; they cannot directly promote a model. Promotion requires unseen runtime Shadow outcomes.
