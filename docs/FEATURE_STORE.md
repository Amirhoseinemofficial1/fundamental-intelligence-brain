# Feature Store

`FeatureStore` builds immutable `features-52.true.1` vectors at a declared prediction cutoff. It queries only observations whose `available_time` is at or before that cutoff and persists the cutoff, provenance, quality and replay flag.

Feature families include macro/fundamental, 17 on-chain fields, 11 derivatives fields, 13 microstructure fields, event surprise/priced-in values, reliability-weighted source conflict, and point-in-time DXY/SPX/NDX/BTC/XAU/WTI cross-asset returns. Unknown/missing values remain explicit zeros; artifact metadata fixes feature ordering. Tests prove a future observation is excluded before availability and changes both the vector and artifact prediction after availability.
