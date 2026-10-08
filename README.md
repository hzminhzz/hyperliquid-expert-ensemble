# hyperliquid-expert-ensemble

## Design authority and current claim boundary

The companion `hzminhzz/hyperliquid-trader-tracker` repository owns the cross-process design and research plan. In the sibling checkout, read `docs/STATUS.md`, `docs/EXECUTION-REVIEW.md` and `plan.md` before implementing the next scoring stage. On the inspected VPS this authority is under `/home/quant/dev/hyperliquid-trader-tracker`.

Execution-review ER0 is implemented on the milestone branch: Python no longer polls `clearinghouseState` or replaces account state independently. It consumes Rust-owned durable `ACCOUNT_SNAPSHOT` and `POSITION_CHANGE` events, preserves full instrument identity, and keeps partial-history position ages unknown. The runtime currently computes cluster-bounded descriptive consensus using a provisional historical redundancy manifest, not the fully qualified V2 predictive scoring path. ER4/ER5 remain empirical evidence gates; passing unit tests or Telegram grading does not authorize live copying.

Financial execution, deployment, and predictive promotion remain outside this repository's current authority. Keep the detailed review and qualification evidence in the companion tracker repository rather than creating a divergent copy here.

Expert consensus, worldview projection, replay, and agent inspection engine for Hyperliquid.

Independent application consuming the KonScanner Rust observation ledger per Contracts C2–C5.

## Prospective research capture (observation only)

The advisory runtime now writes a private `prospective-research.db` with append-only, as-known one-minute cluster-consensus cuts, the consumed ledger cursor, provisional universe revision, and HTTP `allMids` reference prices with receipt times. The same store attaches 1m/5m/15m/1h/4h/24h mid-to-mid observations only after each horizon matures; unmatched/stale prices are censored, never backfilled. These are descriptive V1 consensus proxies, not registered ER4 R0/R1/R2 predictions or realized trading PnL. The 10 bps round-trip deduction is an explicit sensitivity assumption, not verified fees, spreads, impact, or funding. Unsupported HIP-3 namespaces do not inherit unrelated spot/default prices. Inspect `research` in the runtime health JSON for counts, lineage and qualifier.

A separate `copytrade-outcomes.service` records newly observed raw trade-transition markouts in `outcomes.db`, with its own activation boundary. Those raw fills are not independent episodes or proven alpha. Telegram advisory delivery is restricted to material Grade-A/B candidates with cooldown, initial warm-up and 429 backoff; no financial orders are placed.
