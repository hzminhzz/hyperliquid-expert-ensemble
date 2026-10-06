# hyperliquid-expert-ensemble

## Design authority and current claim boundary

The companion `hzminhzz/hyperliquid-trader-tracker` repository owns the cross-process design and research plan. In the sibling checkout, read `docs/STATUS.md`, `docs/EXECUTION-REVIEW.md` and `plan.md` before implementing the next scoring stage. On the inspected VPS this authority is under `/home/quant/dev/hyperliquid-trader-tracker`.

Execution-review ER0 is implemented on the milestone branch: Python no longer polls `clearinghouseState` or replaces account state independently. It consumes Rust-owned durable `ACCOUNT_SNAPSHOT` and `POSITION_CHANGE` events, preserves full instrument identity, and keeps partial-history position ages unknown. The runtime still computes V1 equal-wallet consensus; execution annotations, coherent V2 evidence cuts, and predictive qualification remain separate later gates.

Financial execution, deployment, and predictive promotion remain outside this repository's current authority. Keep the detailed review and qualification evidence in the companion tracker repository rather than creating a divergent copy here.

Expert consensus, worldview projection, replay, and agent inspection engine for Hyperliquid.

Independent application consuming the KonScanner Rust observation ledger per Contracts C2–C5.
