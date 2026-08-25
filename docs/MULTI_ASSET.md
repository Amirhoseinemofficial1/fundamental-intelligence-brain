# Multi-Asset System

| Family | Seed examples | Dynamic discovery | Fundamental features | Reference response | Specialist |
|---|---|---|---|---|---|
| Gold/metals | XAUUSD, XAGUSD | symbol/name | yields, inflation, ETF flow, risk | provider chain / `reference_prices` | `gold` |
| Oil/energy | WTI, BRENT | symbol/name | inventory, production, refinery use | provider chain / `reference_prices` | `oil`, `energy` |
| Forex | seven majors, DXY | symbol/name | rates, differentials, macro | provider chain / `reference_prices` | `forex` |
| Equity/index | SPX, NDX, AAPL, NVDA, TSLA, MSFT | ticker/exchange/issuer | earnings, guidance, margins, microstructure | provider chain / `reference_prices` | `equity` |
| Crypto | BTC, ETH | ticker/metadata | on-chain, derivatives, microstructure | exchange semantic stores + provider chain | `crypto` |
| Token | SOL plus contract/chain discovery | ticker/contract/chain/project | supply, flows, DEX, bridge, derivatives | exchange/reference provider chain | `token` |

An asset family is covered end-to-end only when mapping, features, artifact inference, reference response and outcome learning all resolve. Local tests use deterministic non-crypto and crypto fixtures; endpoint/credential validation remains a Level-B deployment gate.
