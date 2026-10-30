# PM-01 — Whole-wallet research/shadow replay boundary

Issue: [#3](https://github.com/hzminhzz/hyperliquid-expert-ensemble/issues/3).
Implementation: `ensemble.pm_sources`, `pm_portfolio`, `pm_shadow`,
`pm_source_audit`, and `pm_research`. Research-only CLI modules; **not**
imported by the persistent Telegram runtime. No broker, exchange signer,
live-order or production-policy side effect.

## What is implemented

1. Freeze **36 independently resolved on-chain wallet identities** from private
   authoritative maps: original 6, Wave 1 10, Wave 2 20. Exact membership,
   identity collisions, vendor-roster contracts, independently audited
   Wave 1/Wave 2 page state, W019's independently verified fresh LIT
   source generation, and source manifest
   SHA-256 are checked. Raw addresses stay in the private VPS source and are
   replaced by SHA-256 identity fingerprints in the private report.
2. Independently verify cached HyperX pages against the append-only journal,
   matching private sidecars, SHA-256 of original decompressed bytes, source
   wallet, native coin ID, source TID and source event timestamps.
   **HyperX retrieval is not historic first-known visibility.** Do not
   backdate any vendor fill's knowledge clock.
3. Query the *authoritative Rust observation outbox read-only*, in a pinned
   SQLite transaction. Preserve event, receipt, knowledge time, global
   sequence and ledger ID. Consume only events known at or before a frozen
   research cut. Atomic snapshots replace all scoped legs (including empty);
   signed deltas check observed before/after state; CLOSE deletes the leg;
   FLIP transfers one signed quantity change. Unknown, stale, contradictory,
   unseeded or gap-affected states remain blocked.
4. Independently size the **whole observed portfolio** in a follower research
   account using a single source-equity normalization, one gross leverage
   ceiling, 5% market-volume participation, 5m/60m delay and taker,
   spread/slippage and conservative absolute-funding *assumptions*.
   The contemporaneous always-long market-only control receives matched
   instrument selection, absolute source notionals, rebalance timing,
   execution costs and conservative funding. A single unsupported hedge
   leg blocks the **entire** scenario. No substituted Binance market data.
5. Mark cash and all open legs using SHA-validated native 1m sources. Maintain
   instrument cash flows and independent cash-plus-mark-to-market accounting.
   The 1m base-volume × open notional participation proxy is explicitly
   disclosed whenever a file lacks observed quote volume; it is **not**
   an executable order-book depth measurement.
6. Persist a content-addressed **private** JSON report, source event-row SHA,
   source manifests, consumed 1m source-day SHA, per-wallet reasons, model
   assumptions, paired-control verdicts and exemplar cards.

## Frozen study and classified evidence

- Vendor cutoff: **2026-10-29 17:22:16.142 UTC**. Source snapshots and
  real known-at observations originate only from the Rust outbox. Historical
  HyperX records do not contain credible as-known historical timestamps.
- Bounded replay: up to 1,000,000 relevant source rows and 12 predeclared
  daily decision cuts strictly before the frozen study cutoff; latest
  hypothetical fills must not exceed cutoff. Runs must not silently skip
  unsupported positions or downweight missing hedge legs.
- The 36-wallet roster is **winner-selected ex post** from previously
  inspected vendor and tracker data. Any Rust-known replay over the same
  period is retrospective as-known, **NOT untouched prospective OOS
  follower performance**. No live broker fills were captured.
- Real-world source accounting cannot be reconstructed from available
  HyperX fills alone: missing initial state, settlement/funding, cash,
  collateral, possible separate vault/DEX scopes and account corrections.
  An observed Rust snapshot is only the producer's declared account scope,
  not proof of all-time complete exchange account state.
- At the originally frozen report: 14/36 identities had **no Rust account
  events** in the source window; W019's initial LIT cache was incomplete,
  but a later verified 3-page generation supplied 1,165 additional distinct
  vendor fills, with 4,000 old fills exactly unchanged. The effective Wave 2
  vendor-reported roster is now covered for five months (20/20), **not** a
  complete five-month account ledger. Missing Rust/market evidence remains
  explicitly BLOCKED/UNKNOWN, never omitted or treated as zero.
- Source audits must be rerun if local source files change. Previously
  reported 58 `xyz:SP500`/`xyz:XYZ100` Parquet/source.json mismatches
  were **0/0 on the later Oct 30 read-only recheck**. That is a current
  checksum statement only, not evidence of a single stable snapshot of
  the entire live lake.
- Zero verified economic pods, zero qualified prospective follower PnL
  and zero authorized live strategies. **No promotion** permitted.

## Reproduce on a VPS with authorized private data

Run from the existing PM worktree. No API requests, order submission,
Telegram delivery or service modifications occur. Outputs stay private at
mode 0700 directory / 0600 file.

```bash
export PYTHONPATH=src
PY=/home/quant/dev/quant/ml4t-data/.venv/bin/python
ROOT=/home/quant/.local/share/copytrade/reports/five-pod-20261029-research
MARKET=/home/quant/dev/quant/ml4t-data/.data/store
OUT=/home/quant/.local/share/copytrade/reports/pm01-36-shadow

$PY -m ensemble.pm_source_audit \
  --research-root "$ROOT" --market-root "$MARKET" --out "$OUT"

$PY -m ensemble.pm_research \
  --research-root "$ROOT" \
  --ledger /home/quant/.local/share/copytrade/observation.db \
  --market-root "$MARKET" --out "$OUT" \
  --cutoff 2026-10-29T17:22:16.142+00:00 --samples 12 \
  --max-events 1000000

PYTHONPATH=src python -m pytest -q tests/test_pm_portfolio.py tests/test_pm_shadow.py
ruff check src/ensemble/pm*.py tests/test_pm*.py
```

Each rerun on identical immutable content should produce the identical
research SHA and file path. Source mutation creates a new evidence revision;
a conflicting existing SHA path is never overwritten. No private raw data,
wallet address list, raw HyperX page or credential belongs in this Git repo.

## Remaining qualification gates (do not auto-close #3)

- Confirm the **entire** cross-DEX, cross-account wallet exposure plus
  equity, funding, collateral and source history with independent official
  exchange/account reconciliation, preserving first-known receipt clocks.
- Acquire missing actual-first-known observations for the 14 unseen
  aliases, explicitly recover or gate upstream feed gaps, and verify
  all relevant market/instrument-day 1m
  sources and actual depth/quotes without time travel.
- Generate a **pre-registered future holdout** with frozen pod universe,
  allocation, baseline, latency and cost assumptions; measure independent
  forward follower evidence, drawdowns, source vs follower attribution and
  prop-rule suitability separately. The proof threshold cannot be
  replaced by pass counts from synthetic tests.

**Engineering verification of a deterministic fail-closed pipeline is
not a qualified whole-wallet follower or investment recommendation.**
