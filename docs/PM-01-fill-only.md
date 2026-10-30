# PM-01 direction correction — fill-only historical replay

**Authoritative research inputs:** immutable HyperX source fills for **all 36** wallets (6 original / 10 Wave 1 / 20 Wave 2) and venue-specific native Hyperliquid 1-minute OHLCV in `ml4t-data`. The independent Rust observation tracker is **not** a gating input, inclusion criterion, or source of wallet returns for this study. The 14 aliases that lack Rust observations remain fully included in the HyperX cohort.

Run:
```bash
PYTHONPATH=src /home/quant/dev/quant/ml4t-data/.venv/bin/python -m ensemble.pm_fill_replay \
  --research-root /home/quant/.local/share/copytrade/reports/five-pod-20261029-research \
  --market-root /home/quant/dev/quant/ml4t-data/.data/store \
  --out /home/quant/.local/share/copytrade/reports/pm01-36-shadow
```

The source-only replay is intentionally **retrospective**: a HyperX fill's execution timestamp tells us what the wallet did, but not when a follower could discover it. Delay scenarios of 5, 15 and 60 minutes are **hypothetical oracle delays**, never genuine prospective evidence. The current 90-day vendor instrument roster may omit markets traded earlier; it is not a complete historical exchange-account census.

- Native source quality contract: validate raw gzip bytes with SHA-256 page metadata, verify wallet/coin/TID, enforce source position-before/after continuity, keep HIP-3 namespaces distinct, and reject opaque `#ID` markets until resolved. Never replace a missing Hyperliquid minute with Binance or invented prices.
- Positions: preserve unknown starting exposure; begin a replay only after a *fully observed flat reset across the vendor-listed instrument scope*, never assume a zero starting portfolio. A broken source position chain blocks the wallet simulation. Missing markets or insufficient 1m liquidity block the whole-book run, including hedges.
- Capital: normalize the whole observed basket to a declared 10% gross-notional budget on a hypothetical $100k follower; do not pretend to know unavailable source account equity. Model taker fees, half-spread, slippage, funding stress, 1m volume participation and a same-market long-only control.
- Evidence: all 36 aliases are represented in the output with three scenarios each and explicit status/reason. `pnl: null` means **unreplayable**, not a loss or zero return; source-account economic reconciliation and executable liquidity remain unverified.
- Research contract: a fully replayed vendor-listed subset could demonstrate retrospective exposure economics, **not** profitable prospective copying. No live execution, production policy promotion, or Telegram changes.

An earlier Rust-dependent `pm_research.py` is retained only for separate observation-research audit. Its 14-wallet absence verdict must **not** be used to block fill-based PM-01.
