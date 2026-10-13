# hyperliquid-expert-ensemble

## Design authority and current claim boundary

The companion `hzminhzz/hyperliquid-trader-tracker` repository owns the cross-process design and research plan. In the sibling checkout, read `docs/STATUS.md`, `docs/EXECUTION-REVIEW.md`, `docs/DECISION-INTELLIGENCE.md`, `CONTEXT-MAP.md`, and `plan.md` before implementing the next scoring stage. The October 13 DI0–DI7 design defines first-actionable CopyOpportunity semantics, causal shadow recording, agent inspection, and a gated research/forward-validation loop. The shadow collector, outcomes, board and operational digest are implemented; qualified predictive trade emission and broker execution are not. On the inspected VPS this authority is under `/home/quant/dev/hyperliquid-trader-tracker`.

Execution-review ER0 is implemented on the milestone branch: Python no longer polls `clearinghouseState` or replaces account state independently. It consumes Rust-owned durable `ACCOUNT_SNAPSHOT` and `POSITION_CHANGE` events, preserves full instrument identity, and keeps partial-history position ages unknown. The runtime currently computes cluster-bounded descriptive consensus using a provisional historical redundancy manifest, not the fully qualified V2 predictive scoring path. ER4/ER5 remain empirical evidence gates; passing unit tests or Telegram grading does not authorize live copying.

Financial execution and predictive policy promotion remain outside this repository's current authority. **A descriptive-only operational production deployment is authorized and active**; deployment is not a predictive claim. Keep the detailed review and qualification evidence in the companion tracker repository rather than creating a divergent copy here.

## DI shadow copy-opportunity pipeline (2026-10-13)

**Implementation status:** causal shadow code and retrospective replay **implemented and tested**, with independent **shadow-only source and outcome timers deployed** on the VPS from 2026-10-13 06:55 UTC. The production descriptive Telegram digest was first delivered at 2026-10-13 10:10 UTC, with a twice-daily schedule (09:00 and 21:00 Asia/Bangkok). The current Telegram consensus policy was not changed, predictive promotion was not performed, and financial execution is forbidden. Source capture starts prospectively from ledger cursor 232,911 rather than recasting old trades as fresh signals.

The typed interpreter distinguishes OPEN/ADD/REDUCE/CLOSE/FLIP from follower ENTER/INCREASE/DE_RISK/EXIT/REVERSE. The append-only SQLite journal has an exact Rust ledger cursor, known-time/source coverage checks, deterministic coalescing and explicit no-copy reasons. Follower results are strictly hypothetical REST-mid returns after stated 2-second decision latency and assumed 10-bps round-trip cost, not verified broker executions. Matched same-coin lagged-momentum controls are exploratory and never confer alpha qualification.

Read-only and bounded commands:

~~~bash
uv run --frozen --no-sync python -m ensemble.copy_shadow health
uv run --frozen --no-sync python -m ensemble.copy_shadow scan --retrospective --journal /path/to/private-retro.db --batch 500 --batches 10
uv run --frozen --no-sync python -m ensemble.copy_shadow evaluate --journal /path/to/private-retro.db --prices /path/to/prospective-research.db --outcomes /path/to/private-outcomes.db --since 2026-10-09T00:00:00+00:00 --limit 100
uv run --frozen --no-sync python -m ensemble.copy_shadow study --journal /path/to/private-retro.db --outcomes /path/to/private-outcomes.db --horizon 1h
uv run --frozen --no-sync python -m ensemble.copy_shadow board --hours 4 --limit 5
uv run --frozen --no-sync python -m ensemble.copy_shadow changes --since-seq 232911 --limit 20
uv run --frozen --no-sync python -m ensemble.copy_shadow wallet --wallet 0xwallet --limit 20
uv run --frozen --no-sync python -m ensemble.copy_shadow explain --id copy:<opaque>
uv run --frozen --no-sync python -m ensemble.copy_digest  # dry-run; never sends without --send
uv run --frozen --no-sync python -m ensemble.copy_shadow export --journal /path/to/private-retro.db --output /path/to/part.ndjson.gz --limit 10000
uv run --frozen --no-sync python -m ensemble.copy_shadow verify --output /path/to/part.ndjson.gz
~~~

Using `scan` without `--retrospective` on a **new** journal starts at the current ledger cursor and does not relabel prior unseen history as actionable. Both modes are shadow-only; no orders or Telegram sends. Retrospective backfills are not unbiased live trials. Reuse one journal per declared mode; do not mix two different knowledge-clock semantics. Command `ensemble copy ...` is an equivalent CLI route.

Source semantics, qualification gates and resource/retention policy are defined in the sibling tracker's `docs/DECISION-INTELLIGENCE.md`. Sparse cut mode and **replacement of the existing production ensemble/Telegram policy** are deliberately **not enabled**. Shadow source/outcome timers are enabled and memory-capped under `deploy/systemd/copytrade-opportunity-*.{service,timer}`; confirm their independent user-systemd status before trusting recent records. Prospective independent evaluation must begin from a genuinely new capture boundary after operational integrity is qualified. An optional Parquet export requires Polars; the bounded gzip NDJSON adapter works without it. The old one-minute SQLite recorder remains separate and unchanged.

## Production operations (descriptive, October 13, 2026)

The original production services stay active: `copytrade-tracker.service`, `copytrade-ensemble.service`, and `copytrade-outcomes.service`. New dedicated user-systemd timers: `copytrade-opportunity-shadow.timer` (approximately every 20 seconds), `copytrade-opportunity-outcomes.timer` (approximately every 10 minutes), and `copytrade-flow-digest.timer` (09:00/21:00 Asia/Bangkok). The last sends Telegram **observational** summaries only after a valid fresh ledger cursor; it deduplicates per 12-hour delivery period in `copy-digest-delivery.db`. A network acknowledgement lost after delivery can cause a marked retry; the digest period is included in the message.

The systemd source/outcome research worker limits are 256/384 MiB; digest MemoryMax is 128 MiB. Persistent systemd user resource budgets for the existing services: tracker MemoryHigh/Max 128/384 MiB; ensemble 2.5/3.5 GiB; historical raw outcomes 600/1000 MiB. The all-service Oct 11 host OOM and 1278-minute historical scoring gap are **not resolved by these limits**; source information from that interval must never be treated as fresh. Other VPS jobs still consume substantial RAM/swap; inspect `copy health` and `systemctl --user show ... -p MemoryCurrent -p MemoryMax`.

```bash
systemctl --user status copytrade-opportunity-shadow.timer copytrade-opportunity-outcomes.timer copytrade-flow-digest.timer
uv run --frozen --no-sync python -m ensemble.copy_shadow health
uv run --frozen --no-sync python -m ensemble.copy_shadow board --hours 4 --limit 5
uv run --frozen --no-sync python -m ensemble.copy_shadow study --horizon 1h
journalctl --user -u copytrade-flow-digest.service --since today
```

The retrospective and prospective SQLite opportunity journals have pinned, conflicting mode identities and cannot be mixed accidentally. The operational system has neither authority to send trade entries from the new board nor authority to execute orders. A research/forward-validation terminal predicate cannot become PASS through deployment, unit tests, or an attractive one-day return.

Expert consensus, worldview projection, replay, and agent inspection engine for Hyperliquid.

Independent application consuming the KonScanner Rust observation ledger per Contracts C2–C5.

## Prospective research capture (observation only)

The advisory runtime now writes a private `prospective-research.db` with append-only, as-known one-minute cluster-consensus cuts, the consumed ledger cursor, provisional universe revision, and HTTP `allMids` reference prices with receipt times. The same store attaches 1m/5m/15m/1h/4h/24h mid-to-mid observations only after each horizon matures; unmatched/stale prices are censored, never backfilled. These are descriptive V1 consensus proxies, not registered ER4 R0/R1/R2 predictions or realized trading PnL. The 10 bps round-trip deduction is an explicit sensitivity assumption, not verified fees, spreads, impact, or funding. Unsupported HIP-3 namespaces do not inherit unrelated spot/default prices. Inspect `research` in the runtime health JSON for counts, lineage and qualifier.

A separate `copytrade-outcomes.service` records newly observed raw trade-transition markouts in `outcomes.db`, with its own activation boundary. Those raw fills are not independent episodes or proven alpha. In particular, the raw transition recorder signs flow by `delta` and a REDUCE-LONG must not be interpreted as a new SHORT follower entry; the proposed CopyOpportunity contract in the sibling tracker documentation corrects this conceptual distinction. Telegram advisory delivery is restricted to material Grade-A/B candidates with cooldown, initial warm-up and 429 backoff; no financial orders are placed.
