# Model Factory

The core tier implements Bayesian probability, logistic regression, elastic-net linear classification and SGD in pure Python. The ML tier adds scikit-learn Random Forest, Extra Trees and Histogram Gradient Boosting when installed. Boosting and deep dependency tiers are declared separately so their absence does not break core training.

Training is chronological with a 70/30 out-of-sample holdout. Candidate selection uses Brier score, followed by Platt calibration. Serialized JSON or checksummed joblib artifacts contain model family/version, dataset/feature versions, feature order, train/validation windows, hyperparameters, metrics, calibration, seed and environment. Loading verifies hashes before inference.

The optional tier files are `requirements-core.txt`, `requirements-ml.txt`, `requirements-boosting.txt`, and `requirements-deep.txt`. Core-tier fallback is explicitly tested with scikit-learn unavailable.
