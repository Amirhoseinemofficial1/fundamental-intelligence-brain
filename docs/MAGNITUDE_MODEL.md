# Magnitude, Risk and Time-to-Peak Models

`DecisionModelEngine` independently loads `magnitude_return`, `magnitude_mfe`, `magnitude_mae`, `time_to_peak` and `risk` Champions. Continuous models train on resolved market responses; risk trains on adverse-excursion labels. Every Champion and Shadow consumes the same runtime feature snapshot and receives an unseen outcome.

When a target has no Champion, runtime labels the source `EMPIRICAL_HISTORY_FALLBACK`; it never disguises a constant as a model. Evidence records target/MFE/MAE/time-to-peak, uncertainty, risk source and Artifact IDs.
