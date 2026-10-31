# PM-01 — Direct executor forensic pass (2026-10-31)

Status: **ENGINEERING VERIFIED / SCIENTIFICALLY BLOCKED**.
This is research-only. It does **not** authorize a PM, broker orders, prop-firm
policy promotion, a Telegram production change, or a claim of transferable alpha.

All operations were performed on the existing
`feat/pm01-36-wallet-replay` checkout; the original HyperX cache,
`ml4t-data` source checkout, untracked work and separate PM-02 branch were
left intact.

## Changes and complete 36-wallet verification

1. **Native Hyperliquid CHD 1-minute history.** Ten additional, previously
   missing, exact venue/DEX/day partitions were materialized using the
   existing canonical `ml4t-data` Python writer and owner CHD SDK key:
   `SKY/2026-10-15`, `LDO/2026-10-15`, `AVNT/2026-10-21`,
   `xyz:IREN/2026-10-24`, `xyz:HIMS/2026-10-08`,
   `CAKE/2026-09-28`, `ENS/2026-10-06`, `ZETA/2026-10-15`,
   `xyz:AAOI/2026-10-07`, `xyz:IREN/2026-10-08`.
   Each exact venue-symbol/day partition has a verified
   `data.parquet` + `source.json` SHA. No substitutions or old-file
   overwrites. The audited distinct instrument-day inventory is now
   **1,519 / 2,433 SHA verified**; **914** still unavailable
   (including prior verified CHD no-source days). Coverage means
   source integrity, not minute-by-minute liquidity.
2. **Partial-day contract explicit.** The exact
   `xyz:HIMS/2026-10-08` source has **1,424 recorded minute bars**,
   missing the first 16 minute entries. It is SHA-valid but **NOT**
   a full 1,440-minute grid. An actual `VerifiedHyperliquidMinuteSource`
   access to `00:05 UTC` fails `MISSING_EXACT_1M_BAR`; `17:29 UTC`
   succeeds with that same verified source SHA. The CHD acquisition
   reporting code now records full-grid status, missing-minute count and
   zero-volume minutes separately from the byte-SHA result. Neither
   complete grid nor recorded trade volume is proof of executable L2 depth.
3. **5% minute-volume cap diagnostics.** Previously the shadow replay
   returned only `MINUTE_PARTICIPATION_EXCEEDED`. It now emits the
   failing follower/control book, actual attempted notional, recorded
   native-minute notional or proxy, allowed max, required ex-post
   participation fraction and source day SHA. No cap, sizing or cost
   parameter was relaxed. Example 1%-gross first blocks:
   `VS042/AERO` requires **278.8%** of the recorded minute volume;
   `IX-003/io:ANTH` requires **5.875%**, versus the fixed 5% limit.
   The former would be unviable at this execution minute even with
   a considerably smaller order; the latter is close but remains
   unqualified, not rounded up into an allowed fill.
4. **Read-only REST prospective receipts, independent of WS tier.**
   `ensemble.pm_hyperx_rest_forward` is a bounded, explicit
   **at most five vendor coin/page-1 requests**, each separated by at
   least 12 seconds; it uses the existing pinned HyperX Data API source
   adapter, never a trading token. It verifies a frozen wallet/coin
   roster before requests, records raw vendor bytes privately under
   immutable SHA-named pages, and only stores receipts of fills
   observed within the one-hour recency window. A REST response receipt
   time is not retrospectively inferred from the fill event time.
   It marks historical backfills separately and denies account/forward
   qualification without continuous position/collateral state and
   real follower fills. One live batch of
   `W035/HBAR`, `W041/AVAX`, `CA-005/AVNT`,
   `VS066/xyz:AMD`, `W042/LDO` completed five authenticated
   requests and captured **zero** recent fill receipts. The
   vendor `/fills/{wallet}` endpoint without a `coin` query
   returned HTTP 422 in a separate one-request proof; it is **not**
   a cheap whole-wallet subscription replacement.
5. **Unchanged whole-book scientific gates.** The replay input remains
   36/36 verified aliases, original immutable source fills plus the
   2,229 SHA-verified older HyperX fill additions, with **15**
   unresolved position-chain mismatches and no point-in-time
   fully reconciled account-equity/funding/collateral census.
   All 36 historical book anchors are **vendor-listed** exposure,
   not verified whole exchange accounts. No new valid first-known
   prospective follower episodes exist.

## Actual 36-wallet replay

| Case | Historical hindsight scenarios | Blocked | Qualified prospective |
| --- | ---: | ---: | ---: |
| 1%-gross hypothetical $100k | 3 (VS077, ~123h) | 105 | **0** |
| 10%-gross hypothetical $100k | 0 | 108 | **0** |

At 1% gross, the 105 blocked scenarios split into **60 missing-market**
and **45 ex-post minute-participation** blockers. All completed
VS077 5m/15m/60m PnL figures remain
`+$221.40 / +$219.75 / +$232.43`, **equal to their own
matched long-only market control**. Incremental alpha evidence = zero.

Actual PM-02 CLI successfully accepted the new `pm01_fill_only_replay_v2`
artifact, with **five EMPTY seats and 0 R** in its latest read-only output.
The PM-01 → PM-02 → PM-03 provenance gate returns
`RESEARCH_ENGINEERING_CONTRACT_VALID_PROSPECTIVE_BLOCKED`.
Synthetic PM-03 engineering is unblocked; actual Prop-firm account
validation is not.

## Private evidence hashes

All stored at
`/home/quant/.local/share/copytrade/reports/pm01-36-shadow/`
unless indicated otherwise; do **not** commit raw wallet addresses,
page bytes, or API credentials.

- Expanded market SHA/day audit:
  `130b3e4faed084084c9561df2ef356caf30b40976a76c71efb79deef6a99b803`
- 1%-gross 36-wallet replay with exact capacity evidence:
  `aebbce5adcc6295e133eeb8e0492bb1733b1645bf5c9d9669dd8c967aa888ac2`
- 10%-gross 36-wallet replay:
  `c032f1227f2c1d745fab8452d99890da2f21286474b44fc19d7db561f7a2b3b9`
- Bounded HyperX REST receipt batch (0 new recent receipts):
  `d9689bd132ecf8c0484f98df9d035fbca4c267141b469ab58c6dde8f19ca3215`
- Actual PM-02 consumer output:
  `dc1785056c67f5d78eb497c797541b40729644d9848fb1c1cfd38df395091073`
- PM-01/02/03 fail-closed handoff:
  `ee2710b4714fc432ef6291791076020b0a4a41821478d920a1004ec8a412bb38`

## Next scientific decisions

- Acquire authoritative source position adjustments / exact complete
  HyperX history for 15 unresolved breaks, or retain those scopes
  `UNKNOWN` forever. Do not create synthetic trade IDs.
- Resolve missing **914** native market-day scopes where CHD actually has
  trades; keep confirmed no-trade or missing exact 1m as blocked.
- Respect the real 5%-volume participation cap and include price/quote/L2
  research before any execution feasibility judgment.
- Start durable independently timestamped follower reception for the
  36-wallet universe with a **provider-authorized subscription budget**
  and complete initial account snapshot; Free HyperX WS capacity rejected
  the 36-address request. No quota bypass or surrogate historical
  `first_known_at`. A one-hour recency-bounded REST survey is a
  diagnostic, not continuous 36-wallet coverage.
- Obtain genuine matched follower fill/net-PnL results over untouched
  forward episodes before scientific approval. Passing engineering tests
  or repeated historical SHA checks is insufficient.

Keep Issue #3 open and PR #6 draft while science gates remain blocked.
