# PM-02 — Research-only portfolio-manager governance (partial engineering)

**Status:** NOT ACCEPTED / NOT SCIENTIFICALLY QUALIFIED. This is an initial implementation, not Issue #4 completion.

## Source seam

`python -m ensemble.pm_manager --pm01 <sealed pm01_fill_only_replay_v2 JSON> --asof <ISO UTC timestamp> --out <private directory>`

The CLI consumes a 36-wallet PM-01 historical fill-only replay. No Rust ledger,
exchange API, trading key, broker service or production Telegram process is used.
Output: private SHA-256-addressed JSON file with 36 aliases, retrospective
hypothetical scenarios, verified prospective gate results, lifecycle proposals
and five empty-or-proposed research seats. No wallet addresses are serialized.

## Frozen preliminary policies

- 60 independently observed episodes and >=40 effective episodes across >=30 days;
  evidence known within 24 hours; conservative 3.5-sigma uncertainty bound.
- Strictly positive lower bound of *incremental* follower PnL in R after matched
  market-only controls; serial dependence reduces effective sample count.
- Historical HyperX fill times never qualify as first-known receipt clocks.
- Correlation >=0.85 or co-loss overlap >=0.65 flags possible shared manager risk.
- Up to 5 hypothetical seats; per-seat <=0.20 R. Unused risk is reserved.
- Research-only states: CANDIDATE, SHADOW, INCUBATION, ACTIVE, REDUCED, SUSPENDED.
  No transition carries live authorization. PM-03 owns account-risk authority.
- Source PnL is not independently reconciled; all source-PnL fields UNKNOWN.

## Important unfinished requirements

The proof-of-concept is **not** a validated portfolio allocator. The following
must be completed before Issue #4 can be accepted:

1. Strict external prospective evidence ingestion and immutable append-only
   decision log with full SHA source lineage, conflicts, and retroactive revisions.
2. Dependence statistics on synchronized exposures and correlated loss tails;
   exact independent sample definitions and multiplicity-family recording.
3. True marginal portfolio utility and matched equal-risk/shrinkage comparisons
   on identical as-of paths. Current ranking sorts conservative individual
   lower bounds and is **not** a verified marginal portfolio optimizer.
4. Complete lifecycle transitions including CORE/RETIRED, escalation,
   thresholds fixed in a decision-policy manifest before prospective collection.
5. Account-level stress and correlated-tail sizing with explicit emergency cuts.
   Current proposal is only a static capped research risk envelope.
6. Source descriptive PnL reconciliation, independently known turnover,
   execution latency sensitivity, source/follower divergence, coverage,
   confidence intervals and regime-drift analysis per wallet.
7. Full adversarial suite and format verification, including multi-asset
   market outages, cloned tails, selection-history counterexamples and
   retrospective source-revision contamination.

Synthetic tests do not prove actual wallet profitability.

## October 31 evidence result

Running on PM-01 10%-gross overlay replay artifact
`pm01-36-wallets-2827a5b74b52c7e24b30.json` produced
`pm02-ea1a4e4968aaee1853c7c832af2befa657196481603a68e1f4544bd761115bf9.json`:
36 cards; qualified 0; five EMPTY seats, 0 R. Independence and cloning are
**UNKNOWN**, not evidence of 36 independent trading pods.

No prop-firm trading, execution, account promotion or strategy qualification
is authorized by this code.
