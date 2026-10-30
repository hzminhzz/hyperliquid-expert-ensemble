# WP5 — Read-only frozen-forward evidence navigation and archival

**Mode:** an operator interface over the private, pinned WP3/WP4 research
evidence. It never creates a missing historical forecast, modifies the
source ledger, sends Telegram, submits orders, or promotes models.

The base read-only health command remains:

```bash
uv run --frozen --no-sync python -m ensemble.copy_shadow health
```

It distinguishes tracker, Python, collector, outcome-evaluator, hourly
digest, forward-evaluator timers, source gaps, runtime release, short-horizon
maturity backlog, and predictive-qualification blockers.

## One-bounded-command evidence access

```bash
# Check head, pagination cursor and research cut references; no returns
uv run --frozen --no-sync python -m ensemble.copy_forward_evidence list --limit 30

# Exact immutable decision+known-at source+delivery+status-only outcome
uv run --frozen --no-sync python -m ensemble.copy_forward_evidence explain \
  --arm hour:<EXACT_PRIVATE_ARM_ID>

# Recompute as-known source and frozen baseline from the immutable source
# rows at the saved source cursor. Mismatch is REPLAY_DRIFT, never repair.
uv run --frozen --no-sync python -m ensemble.copy_forward_evidence replay \
  --period 2026-11-01T12:00Z

# Measure forward store and SQLite WAL footprint; never purge
uv run --frozen --no-sync python -m ensemble.copy_forward_evidence storage

# Private bounded reference-only export; exclusive file creation
uv run --frozen --no-sync python -m ensemble.copy_forward_evidence export \
  --limit 100 --output /home/quant/.local/share/copytrade/reports/forward-refs-001.ndjson.gz

# Verify the byte hash in the adjacent mode-0600 manifest
uv run --frozen --no-sync python -m ensemble.copy_forward_evidence verify \
  --output /home/quant/.local/share/copytrade/reports/forward-refs-001.ndjson.gz
```

The `list` command uses lexicographic, fixed RFC3339 hour-key pagination.
`next_cursor` is null only when no additional cuts exist. Never silently
truncate replay/export windows. `explain` returns original frozen arm,
as-known source snapshot and attached HTTP delivery attempts. Before the
protected assessment, **only horizon/status** is returned for matured
outcomes; full forward return values are redacted. Missing or ambiguous
delivery remains separate from missing price and source history. Every
object retains `expected_return=null`, no size and no financial execution.

The `replay` command re-evaluates only *as-known-at-the-original-cut*
source rows, restricted by the original persisted projection cursor, and
the frozen pre-decision market-control directions. It compares source
evidence and baseline SHA-256 and an exact bounded arm projection.
A replay failure signals mutable evidence, a source gap or a contract
error; it does not silently rewrite the first record. This is diagnostic
reproducibility rather than a re-created prospective prediction.

The compressed export includes only immutable cut and arm references.
Delivery tokens, raw wallet lists, source blobs, private credentials
and forward return values are excluded. Each export has an adjacent
content-hash manifest and mode-0600 private permissions. The source SQLite
files remain authoritative; archives never become a replacement ledger.

## Retention and restore

Retention stays **append-only** across the research interval. Do not
vacuum/delete old evidence or rewrite any historical append-only table to
save space. Measure SQLite and WAL bytes; take bounded verified exports
if storage pressure approaches a separately approved cap. Source
observation data and old opportunity/outcome journals are untouched.
A restore must preserve the original release and protocol identifiers,
WAL-safe SQLite snapshot consistency, all gap records, forward cuts,
associated delivery receipts, and append-only evaluation revisions.
Unrecovered gaps stay unverified.

Qualified predictive alpha, actual user-delivered decisions, broker
execution prices and pod-level risk remain **BLOCKED** until independently
validated. WP6 allocation work is conditional and should not start based
on hypothetical mid-return alpha or partial wallet PnL.
