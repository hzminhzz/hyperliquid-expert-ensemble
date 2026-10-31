# PM-01 — Propr-scoped 90% data readiness (2026-10-31)

## Decision and source authority

The target is **90% SHA-verified native Hyperliquid 1-minute
instrument/UTC-day coverage within the Propr-relevant market universe**,
**not** 100% of the full HyperX wallet universe.

This is a user-approved change to **data acquisition prioritization**,
not an approval to drop unknown hedges, backdate listings, lower trading
capacity standards, or promote any wallet to a live Propr challenge.

Authoritative owner-held private evidence:

- `PROPR_PUBLIC_ASSET_UNIVERSE_20261030.json` — publicly displayed
  Propr universe snapshot of **158 symbols**: 96 crypto, 55 equities,
  2 indices, 5 commodities, 0 FX.
- `PROPR_WALLET_MARKET_COMPATIBILITY_20261030.json` — all 36
  HyperX aliases by 90-day vendor completed-trade count; native crypto
  ticker exact match, and a **tentative** `xyz:`/HIP-3 basename match.
- Frozen source-cache identity contracts and native Hyperliquid 1m
  Parquet/source.json SHA validation. The original SHA checked market
  inventory was 2,433 distinct instrument-days, 1,519 verified.
- Propr order/fee/legal details from
  `PROPR_LIVE_RULES_AND_API_SNAPSHOT_20261030.json` and
  `PROPR_SPECIFIC_WALLET_REASSESSMENT_20261030.md`.

The Propr display list was collected on **2026-10-30**, AFTER PM-01's
2026-10-29 frozen study cutoff. It is **not** a point-in-time listing
history and does not attest that Propr's broker API accepts an exact
Hyperliquid venue/DEX/market ID. These are research inclusion proxies,
never proof of historical tradability or order acceptance.

## All 36 wallet exposures — do not conflate denominators

| Metric | Count | Fraction of full wallet vendor roster |
| --- | ---: | ---: |
| 90-day completed trades, all wallets | 2,508 | 100% |
| Exact native-crypto display matches | 1,832 | 73.0% |
| Extra tentative `xyz:` HIP-3 display matches | 459 | 18.3% |
| **Maximum** native + tentative display overlap | **2,291** | **91.3%** |
| Not matched or unresolved | 217 | 8.7% |

An independent **five-month raw fill count** audit used original HyperX
source records and yielded:

| Metric | Count or share |
| --- | ---: |
| Raw source fills inside frozen study | 73,087 |
| Exact native-crypto display fill count | 48,946 (67.0%) |
| Exact + tentative `xyz:` fill count | 66,534 (91.0% upper bound) |
| Exact + tentative notional share (positive vendor-priced fills) | 91.8% upper bound |
| Wallets with active unlisted/unresolved exposure at source anchor or study fills | **29/36** |

Neither trade-count nor notional overlap equals *executably replicable
portfolio risk*. The notional ratio excludes 12 opaque-source fills
without positive reported prices; no missing prices are imputed.

Only **W048, W026, W047 and VS033** currently have no observed
active **out-of-scope** leg at their vendor-list anchor/study-event
boundaries **and** no reported source position-chain discontinuity.
These four are **data-research priority candidates only**, not
alpha-qualified managers. The same original source may still omit
unlisted account instruments, and their `xyz:` exposures still require
canonical Propr market-ID attestation.

## Measured 90% market-data gate (after 17 Propr-gated CHD backfills)

| Market scope | SHA-verified days | Coverage | More SHA-verified days needed for 90% |
| --- | ---: | ---: | ---: |
| Native crypto matching Propr display | 1,036 / 1,289 | **80.37%** | **125** |
| Native + tentative `xyz:` HIP-3 | 1,522 / 1,990 | **76.48%** | **269** |
| Outside mapped Propr scope | 14 / 443 | 3.16% | **not requested** |
| Unrestricted HyperX native market audit | 1,536 / 2,433 | 63.13% | Not the new target |

**429/897** outstanding instrument-days belong to outside/unverified
Propr scope: 342 outside current display names and 87 under other
unverified DEX namespaces. Data collection for those categories stops
unless there is later direct market-ID evidence showing a Propr mapping.

The new `ensemble.pm_propr_scope` tool is read-only. It checks all
36 aliases and original input checksums, emits a complete denominator
rather than relying on a missing-only queue, and returns **both**
strict and candidate 90%-readiness numbers plus an exact, priority-
ordered, **Propr-only** acquisition queue. It separates the snapshot
from historical PIT information. The market-audit schema was
extended additively with `instrument_day_inventory` to make the
denominators independently auditable.

`ensemble.pm_chd_target_1m` accepts optional
`--propr-scope PRIVATE_SEALED_REPORT` and
`--propr-policy strict|candidate` to fail closed before a provider
request for any day outside the selected scope, any stale market audit
SHA, or a scope whose 90% target is already met. Default policy with
the scope is `strict`. Provider requests remain bounded to five
explicit source-day targets per invocation; existing canonical data
is never overwritten.

**Seventeen** Propr-strict, previously missing native instrument-days were
successfully recovered in five bounded CHD batches, each with 1,440
UTC minutes and source.json/Parquet SHA verification. The first five
were `POL/2026-10-27`, `VIRTUAL/2026-10-12`, `MET/2026-10-26`,
`TIA/2026-10-28`, `DASH/2026-10-29`. The remaining twelve were:
`POL/2026-09-28`, `POL/2026-09-29`, `POL/2026-10-02`,
`SYRUP/2026-10-03`, `SYRUP/2026-10-04`, `SYRUP/2026-10-08`,
`SYRUP/2026-10-10`, `GRAM/2026-10-11`, `GRAM/2026-10-12`,
`GRAM/2026-10-13`, `GRAM/2026-10-14`, `GRAM/2026-10-22`.
None attests executable book depth.

## Narrow wallet-first target — achieved

Rather than demanding the entire 36-wallet cohort pass 90% day
coverage, the immediate Propr research target selects wallets with
**no observed nonzero out-of-scope book leg at the source anchor or
study events, and no cached source position-chain error**. All
four pass the *strict* native-crypto 90% market-day gate:

| Wallet | Propr-native 1m instrument-days | Day coverage | Tentative xyz included |
| --- | ---: | ---: | ---: |
| W048 | 22/22 | **100%** | 22/22 |
| W026 | 22/24 | **91.7%** | 22/24 |
| W047 | 26/26 | **100%** | 34/34 |
| VS033 | 45/49 | **91.8%** | 52/56 |

**No further data downloads are required to satisfy this particular
90% market-day gate for these four.** It remains unverified whether
Propr accepts their exact instrument IDs, whether all source-account
positions have been observed, and whether their follower executions
pass realistic capacity and costs.

A fresh **actual 36-wallet, 1%-gross replay** after these 17 CHD backfills
still returns only 3 historical-only VS077 scenarios and **105 blocked**
(10%-gross: **108/108 blocked**). Even the four shortlisted wallets
cannot currently pass full-horizon follower replay:

| Wallet | Representative 5m first blocker | Economic implication |
| --- | --- | --- |
| W048 | BNB needs 131.9% of ex-post 1m volume vs 5% cap | Order capacity infeasible at this size |
| W026 | GRAM needs 3,354% of ex-post 1m volume vs 5% cap | Material capacity failure |
| W047 | PUMP needs 5.56% of ex-post 1m volume vs 5% cap | Slightly over cap; later trades also unverified |
| VS033 | An unavailable exact SYRUP minute/day | Partial 90%-day coverage cannot complete full-horizon book |

These are retrospective volume proxies, not order-book depth or a
Propr execution guarantee. A lower *research-only* notional budget
might relieve some volume failures but does not prove meaningful PnL,
first-known signal timing or realistic brokerage constraints.

These wallets remain **research candidates, not approved PMs**.
Continuing downloads across unrelated wallets or unmapped HIP-3
instruments has no automatic priority.

## Reproduction

```bash
cd /home/quant/.devspace/worktrees/hyperliquid-expert-ensemble-e95bd915
export PYTHONPATH=src
PY=/home/quant/dev/quant/ml4t-data/.venv/bin/python
RESEARCH=/home/quant/.local/share/copytrade/reports/five-pod-20261029-research
MARKET=/home/quant/dev/quant/ml4t-data/.data/store
OUT=/home/quant/.local/share/copytrade/reports/pm01-36-shadow

$PY -m ensemble.pm_market_audit \
  --research-root "$RESEARCH" --market-root "$MARKET" --out "$OUT"

# Pass the exact output market-audit artifact from the previous command:
$PY -m ensemble.pm_propr_scope \
  --research-root "$RESEARCH" \
  --market-audit "$OUT/pm01-36-wallets-fcb98de61ca43cdeeed0.json" \
  --out "$OUT"

# Every subsequent bounded fetch must use the *matching* sealed scope
# and market-audit SHA. Regenerate both after a successful batch.
$PY -m ensemble.pm_chd_target_1m \
  --ml4t-root /home/quant/dev/quant/ml4t-data \
  --audit "$OUT/pm01-36-wallets-fcb98de61ca43cdeeed0.json" \
  --propr-scope "$OUT/pm01-36-wallets-643139a4dff46e26ae9c.json" \
  --propr-policy strict --mode trades --max-days 1 \
  --target POL@2026-09-30 --out "$OUT"
```

The last example is **optional and is not needed for the four-wallet
90% target already met**. It makes one new data-provider request when
invoked; it is not part of replay or an order/background service. Update the two SHA-linked input paths
from the latest locally audited output before later batches.

Current SHA-256 source evidence (owner-private; not committed):

- Market `instrument_day_inventory` after backfill:
  `fcb98de61ca43cdeeed0b27826593a288c3f7f3ea43299907f6a3cb1222ff758`.
- Propr-scoped 36-wallet 90%-readiness report:
  `643139a4dff46e26ae9cac4354350032aaa6e54c48f3028930cdf2c423da8529`.
- 1%-gross replay after scoped backfill:
  `33f95784345e79ca5b1fd9e870d0882ea0e4c37e634ff316da49ab76a961e1c5`.
- 10%-gross replay after scoped backfill:
  `7a28ddf136426bd9d64a3d82f9f58135894515b7037eafaf54c4c7b3273a7114`.
- Five Propr-gated CHD batch evidence files (all private):
  `a67ac6e90e58a91df5f7e3312f594873b187bc3673bfeb91f380e36b10160b23`,
  `605c2b5f681e391ae69a3b1fcf5d10ae0311e553682efae9fa46a0db23659da3`,
  `0c6da2a6bf6f72d03b5022cebc19d17929c4b7e976d7c570cda020bf515b70d2`,
  `a43424bdd4dda313e5344f857ed745b7e6689858c9002508f6f067bff5736979`,
  `93eee885070955ba80c016583b8882329e6b6c2228a6a3edcad0aae6eb3d1546`.

## PM-01 and PM-03 scientific acceptance remain separate

Even after 90% scoped day coverage, the following are **independent**
qualification requirements:

1. Historical vendor fill timestamps are not prospective local
   observation clocks; source first-known, follower decision and
   hypothetical fill timing must remain separate.
2. The 15 unresolved source position-chain mismatches and incomplete
   wallet-level funding, collateral and account-state census remain
   UNKNOWN. They cannot be cleared by filtering to Propr symbols.
3. Unavailable non-Propr legs can be actual economic hedges. Dropping
   them creates a *different* manager strategy. Such a Propr-only
   implementation requires explicit partial-replication risk attribution
   and must not be labeled faithful whole-wallet follower performance.
4. Passing a day SHA does not establish 1m exact-bar completeness,
   executable 5%-minute participation or account-specific Propr fees
   and profit/drawdown rules.
5. Tentative `xyz:` basename matches must be resolved to actual Propr
   order IDs, venue scope and historical listing periods before a
   Propr-executable follower universe is declared.
6. Independent matched prospective follower PnL and uncertainty
   analysis are required for any wallet alpha claim or active budget.

**Decision:** Adopt 90% Propr-relevant market-data coverage as an
acquisition milestone. Keep PM-01 engineering and scientific
qualification separate and keep Issue #3 OPEN until its accepted
point-in-time follower research criteria are actually met.
