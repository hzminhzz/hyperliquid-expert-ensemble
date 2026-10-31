# PM migration checkpoint (2026-10-31)

## Scope
- Source repo: hzminhzz/hyperliquid-expert-ensemble, PM-01 PR #6 and PM-02 PR #7.
- Target: separate copytrade-pm repository for PM-01/PM-02/PM-03, preserving the original ensemble runtime and deployment.
- This branch is an **integration and migration staging branch**, not the extracted repository and not authorized for live trading.

## Verified engineering baseline
- PM-01 base SHA: 3654dba637244ffb645ddb2b21ad7c687306345d
- PM-02 commit: 0f0fdc9446469d60aa2d9bde9450caf78ad224cf
- Merge made with no upstream branch edits; combined suite 231 passed, 2 skipped.
- All six PM-02 source/test files pass Ruff lint and format.
- PM-01 preserves the v2 `pm01_fill_only_replay_v2` artifact consumed by PM-02.
- Earlier evidence remains stored in private SHA-addressed artifacts. Do **not** copy their bytes to GitHub.
- Existing ensemble advisory systemd deployment is outside this branch and must not be altered.

## Migration invariant
1. Capture clean source and source-commit SHAs before creating a destination.
2. Do not migrate secrets, private wallet alias/address maps, raw vendor fills, market Parquet, deployment credentials, or untracked `.workspace` content.
3. Preserve PM-specific history and evidence IDs. Cross-link old issues #3/#4/#5 to successor tracking, retaining originals as provenance.
4. Move PM research code/tests to a separate `copytrade_pm` Python package and CLI. Legacy consensus and Telegram runtime remain in the ensemble repository.
5. Keep PM-01 replay v2 accepted by PM-02, with additive schema extension only after versioned consumer compatibility tests.
6. Isolate all data access behind read-only adapters. Source event, first-known receipt, decision, and modeled fill clocks remain distinct.
7. No PM-01/02 simulation, historical source fill, or PM-03 scenario may claim qualified follower alpha without prospective evidence.
8. PM-03 is shadow-only; synthetic account outcomes cannot imply a real pass probability.
9. Never route PM-01/02/03 directly to the existing ensemble runtime or a broker/execution gateway.
10. Before cutover, run legacy ensemble regression tests and the new PM suite independently; compare deterministic artifact hashes, risk decisions, and evidence lineage. Verify the systemd service remains on its original pinned release.

## Scientific blockers
PM-01's Propr-scoped four-wallet 90% market-day readiness is **not** portfolio/execution qualification. Missing executable minutes/participation capacity, incomplete source economics, first-known observations and Propr legal/market identity remain unresolved. PM-02 therefore must retain empty seats and zero actual risk proposals.

## Next irreversible boundaries
- Create private `hzminhzz/copytrade-pm` only after choosing a history-preserving extraction method that excludes unrelated ensemble runtime files/history; no blind mirrored repository push.
- Put a contract-based integration test ahead of changing the PM-01/PM-02 artifact schema.
- Implement PM-03 only in the isolated PM project, with deterministic read-only synthetic ledger, account floor models, attribution, and no live order path.
