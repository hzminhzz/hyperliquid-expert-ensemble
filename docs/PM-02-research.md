# PM-02 — Research-only portfolio-manager governance (partial engineering)

**Status:** NOT ACCEPTED / NOT SCIENTIFICALLY QUALIFIED. Engineering is in progress on stacked draft PR #7. The hourly autonomous direct-implementation goal is documented in `docs/PM-02-goal.md`.

## Source seam

`python -m ensemble.pm_manager --pm01 <sealed pm01_fill_only_replay_v2 JSON> --asof <ISO UTC timestamp> --out <private directory>`

The CLI consumes a 36-wallet PM-01 historical fill-only replay. `--previous <immutable pm02-<sha>.json>` chains a later decision to the previous content-addressed report with monotonic as-of and immutable cohort/state checks. Input PM-01 filenames must match the raw artifact SHA. Outputs are O_EXCL, owner-only, content-addressed and fsynced. A content-addressed local file does **not** independently prove genuine forward receipt time.

No Rust ledger,
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

The allocator now has synthetic-only behavioral coverage for strictly overlapping UTC dates, paired dependence and signed instrument exposures, clone and joint extreme-loss diagnostics, capped greedy marginal utility, risk reserves, and matched equal-risk and shrinkage-inverse-vol comparator curves. No actual wallet has the requisite prospective matched paths, so real dependence and these baselines remain UNKNOWN. The system is **not** a validated portfolio allocator. The following
must be completed before Issue #4 can be accepted:

1. Strict external prospective evidence ingestion and immutable append-only
   decision log with full SHA source lineage, conflicts, and retroactive revisions.
2. Dependence statistics on synchronized exposures and correlated loss tails;
   exact independent sample definitions and multiplicity-family recording.
3. Validate the current conservative greedy marginal portfolio utility and
   matched equal-risk/shrinkage comparisons on a trusted prospective path;
   synthetic tests alone are insufficient. Add full uncertainty calibration,
   multi-parameter pre-registration, and economically defensible R sizing.
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
`pm02-f3df08b95921c3a1ed5eea4a1e71b82efb375d60850873a652b8c52d0f9bb713.json` after introducing source SHA checks and decision-chain metadata:
36 cards; qualified 0; five EMPTY seats, 0 R. Independence and cloning are
**UNKNOWN**, not evidence of 36 independent trading pods.

No prop-firm trading, execution, account promotion or strategy qualification
is authorized by this code.
