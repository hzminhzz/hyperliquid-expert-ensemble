# PM-01 — Fill-only historical replay (36 Hyperliquid wallets)

**Authoritative research inputs:** private, immutable HyperX fills for the full **6 original + 10 Wave 1 + 20 Wave 2** cohort, and validated **native Hyperliquid** 1-minute OHLCV from the user's ml4t-data lake. The independent Rust observation tracker is **not** a PM-01 data source, gating criterion, or dependency. Missing Rust observations for 14 aliases must not disqualify their fill-based research.

**Authority:** retrospective portfolio research / paper shadow only; NO orders, broker execution, prop-firm policy promotion, Telegram changes, or claims of follower realized profits. The offline replay and audit components never require provider credentials; the separately invoked authenticated recovery utilities load the existing data keys privately and never place orders.

## What the October 30 follow-up fixed

1. **Critical fill ordering bug:** source fills with identical HyperX exchange milliseconds require position-before/after linking. The old adapter correctly reordered within each coin, but a final sort by `trade_id` undid those links. Now instrument-local chains are stably merged by exchange timestamp only, and contradicting TID order is regression tested.
2. **No invented starting book:** reconstruct each instrument from its actual first `start_position` and signed fill delta. Cold-start the portfolio only after every vendor-listed coin has been directly observed, using the resulting full signed exposure as initial inventory. Warm-up fills and earlier discontinuities are censored, never counted as follower PnL. A gap **after** the anchor blocks the whole wallet economic result, including unaffected-looking hedges.
3. **W019/LIT:** use the separately validated three-page immutable new generation instead of stale partial pagination. Accept a cached vendor page series with `has_more=true` **only when its oldest available fill predates the frozen five-month study start**. This is window coverage, not a claim of all-time pagination or account census. Retain the main-source and fresh-generation SHA lineage.
4. **Native 1m contract:** exact Hyperliquid venue and namespace (including `xyz:` in the HIP-3 `source_symbol`), daily data SHA, exact instrument, no same-minute future close, OHLC/volume validation and timezone-aware minute-grid uniqueness. Case-sensitive symbols (e.g. `kPEPE`) are valid identifiers but lack verified local market-day sources. Opaque `#ID` and other unsupported HIP-3 DEX markets remain blocked if economically nonzero; no false `xyz` remapping or Binance substitution.
5. **Follower:** one whole-book scaling factor targeting a selected gross-notional share of hypothetical initial capital (default 10%; extra 1% sensitivity), not inferred source-account equity. Hypothetical next-minute-open execution at 5/15/60-minute delay, taker+half-spread+slippage stress, conservative absolute funding stress, 5%-of-**realized** market-minute volume cap. Matched-instrument/delay **always-long** reference; independent cash-plus-MTM and instrument-ledger reconciliation, open legs marked at a validated terminal market minute. Zero positions must be deleted before funding and marking.
6. **Reproducibility:** every report is private, deterministic and content-addressed. Raw addresses, vendor pages and API keys never enter Git artifacts. Fill-only replay uses no Rust information. Source manifest digests, normalized fill SHA and actual consumed 1m day SHAs are reported.

## Read-only reproducible commands

From the feature branch:

```bash
export PYTHONPATH=src
PY=/home/quant/dev/quant/ml4t-data/.venv/bin/python
ROOT=/home/quant/.local/share/copytrade/reports/five-pod-20261029-research
MARKET=/home/quant/dev/quant/ml4t-data/.data/store
OUT=/home/quant/.local/share/copytrade/reports/pm01-36-shadow

$PY -m ensemble.pm_fill_diagnostics --research-root "$ROOT" --summary
$PY -m ensemble.pm_market_audit --research-root "$ROOT" \
    --market-root "$MARKET" --out "$OUT"
$PY -m ensemble.pm_fill_replay --research-root "$ROOT" \
    --market-root "$MARKET" --out "$OUT" --risk-budget 0.10
$PY -m ensemble.pm_fill_replay --research-root "$ROOT" \
    --market-root "$MARKET" --out "$OUT" --risk-budget 0.01

uv run --group dev python -m pytest -q tests
ruff check src tests
```

Audit first: `pm_market_audit` emits each required missing/unsupported native instrument-day (wallet count and exact UTC date) in a **prioritized private remediation queue**. A valid Parquet-day SHA does not itself establish a tradeable one-minute bar, best bid/ask, or execution capacity. No `ml4t-data` source partitions are overwritten or silently filled. CryptoHFTData source data acquisition must use authorized, budgeted vendor calls and a separate integrity-verified immutable generation.

## Verified October 30 source and result classification

- **36/36** identities included; initial independent raw-source checksum audit: **941** original HyperX pages, **104,051** raw fills. W019 new LIT addendum independently verified **1,165 additional distinct rows**, with **4,000** overlapping rows identical. Raw source fills remain vendor-historical knowledge, not timestamped real follower observations.
- After the same-millisecond ordering repair, **27 of 36 wallets have no detected per-instrument source chain breaks**; nine have at least one discontinuity requiring censoring or explicit rejection.
- Native 1m inventory audit: **2,401** distinct instrument/day requirements sampled from the 36 source books. **1,458** verified day-byte-SHA; **943** missing/unsupported days (855 missing local market day, 88 unsupported HIP-3 DEX). The queue has 943 entries and **zero currently materializable local tick-day candidates**; missing provider history cannot be fabricated.
- **$100,000 hypothetical initial capital, 10% gross-notional budget:** 36 wallets × 3 delay cases = 108/108 fail-closed; no economic PnL estimate. Missing native markets and bar-volume capacity are key blockers.
- **1% gross-notional sensitivity:** 3/108 scenarios become executable in this simplified 1m model, all for **VS077** over **~123 hours** (5.1 days), not a representative five-month study. 5m/15m/60m historical-oracle PnL: **+$221.40 / +$219.75 / +$232.43**, respectively, on a simulated $100k account. The **same-market always-long control has identical PnL** in each case: **zero incremental alpha**. Non-qualifying, gross selection/hindsight biased and only a base-volume/open trade proxy, not real market-depth execution.

## 2026-10-31 independently authenticated HyperX resnapshot and CHD backfill

**HyperX is the ONLY wallet-fill recovery source in this implementation.** The
existing Data API key is loaded privately from the tracker; NO Hyperliquid
official-fill API, trading token, or live broker was called. The original
2026-10-29 frozen source cache remains immutable. New pages are separately
stored under owner-only
`/home/quant/.local/share/copytrade/reports/pm01-hyperx-gaps/`.

- Bounded HyperX resnapshot: **15 affected wallet/instrument pairs**, **18**
  authenticated data API calls. Every response passed source wallet,
  instrument, page, SHA-256, full vendor pagination and overlapping-TID
  record equality checks. No conflicting original TID content was seen.
- Newly recovered, distinct **pre-cutoff** fills: `W019/HYPE +2,121`,
  `VS083/HYPE +108`, **total +2,229**. All 2,229 are **older than the
  2026-05-29 study start** (2025 history); they repair earlier source-chain
  evidence, not new 2026 follower decisions.
- Source chain inconsistencies: **27 → 15** across the same nine wallets.
  `W019` 6→2, `VS083` 9→1, others unchanged. **27/36** wallets have
  contiguous observable fill-to-fill position chains. No synthetic
  correction or implicit zero inventory is inserted.
- All remaining 15 mismatches occur across *different exchange timestamps*
  and persisted after authenticated full HyperX re-query. They may indicate
  historical provider omissions, position-adjustment events, or other
  source limitations. They are not evidence that market trades alone can
  reconstruct all exposure. **Two critical post-anchor breaks remain**:
  `W035/HBAR` +886 units on Oct 15–23 and `W041/AVAX` +20 units on
  Oct 21, 2026. Both remain FAIL-CLOSED.
- `pm_hyperx_overlay_audit` cryptographically verifies all new page
  generations and independently computes per-wallet gaps before and after.
  `pm_fill_replay --hyperx-generations ...` can use only these exact
  verified extra rows. The original source generator is never changed.
- CHD 0.8.0 **market trade** SDK, via the existing ml4t-data authoritative
  writer, published three **new** native Hyperliquid OHLCV days:
  `ALGO/2026-10-29`, `POL/2026-10-28`, `VIRTUAL/2026-10-24`,
  each **1,440 1m bars** and SHA-256 verified. These were NOT
  wallet-specific fills. CHD finalized candle manifest returned no covered
  interval for these days; the validated trade-to-OHLCV path succeeded.
  Original canonical market partitions and all source code under
  `ml4t-data` were preserved.
- The distinct required instrument-day market checksum audit improved from
  **1,458/2,401 accepted** to **1,461/2,401 accepted**; still **940**
  missing/unsupported instrument-days (including 88 unsupported HIP-3
  DEX days), and true executable L2 depth remains unverified.
- Retrospective 36-wallet, 1%-gross follower replay with verified HyperX
  supplement: **3/108 hypothetical scenarios** (only VS077) and **105/108
  blocked**, unchanged from the prior outcome. No new qualified pods.

Reproduction for source improvements (no provider calls):

```bash
export PYTHONPATH=src
PY=/home/quant/dev/quant/ml4t-data/.venv/bin/python
ROOT=/home/quant/.local/share/copytrade/reports/five-pod-20261029-research
BASE=/home/quant/.local/share/copytrade/reports/pm01-hyperx-gaps
OUT=/home/quant/.local/share/copytrade/reports/pm01-36-shadow

$PY -m ensemble.pm_hyperx_overlay_audit --research-root "$ROOT" \
    --generations "$BASE/g20261030t1710a" "$BASE/g20261030t1745b" \
      "$BASE/g20261030t1755c" "$BASE/g20261030t1815d" \
    --out "$OUT"

$PY -m ensemble.pm_fill_replay --research-root "$ROOT" \
    --market-root /home/quant/dev/quant/ml4t-data/.data/store \
    --out "$OUT" --risk-budget 0.01 \
    --hyperx-generations "$BASE/g20261030t1710a" \
      "$BASE/g20261030t1745b" "$BASE/g20261030t1755c" \
      "$BASE/g20261030t1815d"
```

The authenticated fetch command is separately bounded by
`--max-calls`, `--max-pages`, and a 12s request interval;
the CHD target-day writer has `--max-days` and a SHA-audited target allowlist.
These commands make external requests **only when explicitly invoked**.
Re-running the two audit/replay commands above makes no provider requests.

**Still BLOCKED for economic qualification:** 15 source-position breaks,
940 native market instrument-days, incomplete vendor market-roster census,
source account equity/funding/collateral accounting, and genuine prospective
signal receipt clocks.

## Non-negotiable caveats and unfinished PM-01 gates

1. **Vendor-listed-book is not whole Hyperliquid exchange account:** the vendor's current 90-day instrument roster may omit prior-only instruments, vault/DEX account scopes, funding/collateral/cash changes and liquidation economics. Initial signed exposure derives from reported per-instrument source fill `start_position`; there is no verified source account equity or account PnL reconciliation. All allocations are whole-**observed**-book only.
2. **Historical fill timestamp is not first-known follower receipt:** Oracle delay experiments are retrospective, even at 60 minutes. This cannot qualify any wallet as an out-of-sample predictive pod. Later genuine forward validation needs a pre-registered wallet universe and independently received signal clocks, but **must not depend on a separate Rust research tool**.
3. **Missing market 1m:** every active and residual follower leg requires native venue-specific price/volume at the modeled time. The exact missing-day queue is recorded, not silently dropped. The model's 5% capacity check uses contemporaneous *realized* minute volume unavailable before placing an order; this is ex post stress testing, **not proof of executable liquidity**. Missing quotes/L2 and broker-specific prop rules remain unsatisfied.
4. **No pod promotion / no PM-01 closure:** even apparently positive VS077 shadow returns did **not** outperform its own matched always-long baseline. Zero means **zero measured incremental edge**, not evidence that future alpha is precisely zero. A blocked `pnl: null` result is not a zero return or a losing trade.

The older `pm_research.py` is an independent Rust-derived observation audit only; its missing-14-Rust-coverage result must never be presented as a fill-only PM-01 acceptance blocker.
