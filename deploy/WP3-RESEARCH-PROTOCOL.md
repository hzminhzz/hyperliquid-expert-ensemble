# WP3 — Preregistered economics and frozen forward hypothesis

**Status:** historical read-only diagnostics implemented; prospective **protocol freeze
only**. This is not a backtest pass, a copy-trade signal, executable PnL, or
approval for financial execution. WP4 must implement the forward forecast/price
contract *before* forward performance can count.

## Scope and ownership

- Rust source ledger remains authoritative. Python is an immutable
  research/analysis reader. The live journal, markouts, Telegram, and private
  wallet data are not rewritten for this study.
- Primary candidate is an **hourly** descriptive watch, not an immediate
  copy order. It uses only eligible newly known OPEN, ADD and verified FLIP
  new-side changes from the preceding 60 minutes; distinct wallet support >=3,
  directional dominance >=2:1 and net margin >=2; reduction/exit pressure
  at least as large as directional support vetoes the watch.
- One fixed 1-hour follower holding period **after the actual available entry
  price**, using the real Telegram delivery timestamp plus a declared
  5-second reaction allowance. Absent actual delivery or usable quote is
  censored, not a fabricated prospective action. This must be built in WP4.
- An event-driven first actionable variant is secondary. 15m and 4h horizons
  are registered secondary endpoints. Do not choose the winning horizon after
  looking at the new holdout.

## Frozen period and population

- Freeze must finish before **2026-10-31 00:00 UTC**.
- Assessment interval: **2026-10-31 00:00 UTC inclusive** through
  **2026-11-30 00:00 UTC exclusive**. One final assessment, earliest after
  2026-12-02 00:00 UTC; no repeated peeking.
- Private roster: *every* wallet with a recent valid Rust-account snapshot
  as known at freeze. No top-performing wallets picked using the same
  historical labels. Newcomers after freeze excluded from confirmatory claims.
  One source snapshot alone does not prove complete fills history or all
  instrument scope. Frozen private wallet IDs must not go to GitHub or a
  public research report.
- Existing exploratory historical results are inspected and compared to
  stronger controls *before* freeze. Historical inclusion, 1m mid prices,
  source-coverage issues and prior multiple hypothesis search are explicitly
  unresolved. No "profitability" conclusion comes from old labels.

## Historical accounting and controls

Run `ensemble.copy_economics` against the three actual SQLite stores in
**read-only mode** and with an explicit knowledge cutoff. It validates:
event <= first-known <= earliest actionable <= sampled entry <= sampled exit <=
outcome-written; exact signed gross mid return; one modeled round-trip cost;
positive finite prices; and quote-vintage admissibility.

The historical comparison uses an independent 4-hour UTC instrument bucket
with the earliest observed opportunity in each bucket, then **equal UTC-day
mean**. This reduces redundant wallet shock sampling without proving
independent pods. A 2,000-draw *UTC-day* bootstrap reports an interval around
that *same equal-day estimand*, not around a different episode-weighted mean.

All controls share the exact same hypothetical entry/exit mids and measured
mid-sample delays; their directions use only pre-known prices:

1. Cash/no-trade (zero return, zero cost).
2. Always-long same-coin benchmark.
3. Same-coin 15-minute prior momentum.
4. Same-coin 60-minute prior momentum.
5. Same-coin 60-minute reversal.

Unobserved or stale momentum feature is **missing**, never replaced by a
future sample or a convenient zero. Flat momentum signals explicitly select
cash and pay no transaction cost. Every active position gets the same
10-bps modeled round trip, with 5/20/30-bps sensitivity. Original source
markouts already include 10bps, so no additional 10bps deduction is allowed
from their `hypothetical_net_return`. Funding, impact, market depth, venue
differences, and actual prop-broker fills are NOT measured.

Report *separate* gross, wallet net, comparator net, incremental matched net,
sample counts, effective dates, cost sensitivity, missingness, measured
collector and sampled-price latency, instrument/wallet concentration and
source-coverage strata. All historical results are exploratory even if
one interval excludes zero.

## Confirmatory rules

The frozen private `protocol.json` is machine-readable, signed by a
content hash and paired with a mode-0600 exact wallet roster. It requires:

- >=20 valid UTC days, >=100 distinct four-hour instrument shock buckets,
  no single instrument or wallet >40% of the retained buckets.
- An *observable* hourly decision and actual delivery, with point-in-time
  cluster/source and price evidence. Current observer feed history remains
  unverified, and market-reference mids are not executable quotes.
- Positive cash-adjusted hypothetical net (lower daily-bootstrap interval
  >0), superior to each preregistered market control with Holm familywise
  alpha=.05, and positive net mean at 20bps modeled cost.
- More importantly, **source completeness, independent-pod support and
  executable-cost/fill evidence** are mandatory before ever interpreting
  that result as deployable. No possible statistical success bypasses them.
- Before any statistical promotion: independently verify source gaps,
  purging/overlap, calendar weighting, conservative cost sensitivity,
  multiple testing, no post-freeze wallet reselection, and no future inputs.
  Do not turn data quality blockers into a negative trade or into a loss.

The current library freezes the protocol; it does **not** automatically
produce mature forward hourly forecasts, track prop trading constraints, or
enforce all promotion gates. WP4 creates the separate immutable
`DecisionCut -> Forecast/Abstention -> Outcome -> Qualification` contract,
and may not rewrite this preregistration to fit observed results. Any
protocol amendment gets a new independent, later holdout.

## Private operating procedure

After committing clean code and passing tests, create read-only historical
attribution using an explicit cutoff no later than freeze. Use an immutable
private directory under `~/.local/share/copytrade/reports/` and the
`--output` argument. Then freeze once:

```bash
uv run --frozen --no-sync python -m ensemble.copy_economics \
  --journal /home/quant/.local/share/copytrade/copy-opportunities-live.db \
  --outcomes /home/quant/.local/share/copytrade/copy-outcomes-live.db \
  --prices /home/quant/.local/share/copytrade/market-reference.db \
  --cutoff 2026-10-30T11:33:58+00:00 \
  --output /home/quant/.local/share/copytrade/reports/wp3-20261030/historical-attribution.json

uv run --frozen --no-sync python -m ensemble.copy_preregister \
  --ensemble-repo /home/quant/dev/hyperliquid-expert-ensemble \
  --ledger /home/quant/.local/share/copytrade/observation.db \
  --journal /home/quant/.local/share/copytrade/copy-opportunities-live.db \
  --release /home/quant/.local/share/copytrade/releases/ens-bdfe4003dcb2_trk-8646ea573f68/manifest.json \
  --historical-report /home/quant/.local/share/copytrade/reports/wp3-20261030/historical-attribution.json \
  --destination /home/quant/.local/share/copytrade/reports/wp3-20261030/preregistration
```

After freezing, preserve `protocol.json` and `wallet-universe.PRIVATE.json`
as immutable private evidence. The hashes make unintended modifications
detectable but do not cryptographically notarize a freeze to a third party.
New code deployment to the observer is **not** needed for this offline study.
Never merge any of these private artifacts into the public repository.
