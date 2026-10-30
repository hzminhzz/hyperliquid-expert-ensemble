# WP4 — Frozen hourly forward research, shadow-only

**Frozen authority:** private WP3 protocol
`wp3:bbe029b370dea8d032f7`, dated 30 October 2026.
WP4 cannot rewrite the WP3 decision rules, wallet roster, horizon, comparator
parameters, or historical attribution. Its research code and release identities
are recorded separately from the original research-code commit.

**Production mode:** observer + descriptive Telegram digest + append-only
research hypotheses only. No order, broker, account sizing, expected-return
calibration, qualified wallet-skill score, or automatic promotion is authorized.

## Independent evidence layers

1. **Source:** Rust observation ledger; each record has source ledger and
   knowledge timestamps. Persisted stream outages and malformed snapshots
   remain hard blockers; future recovery cannot manufacture uninterrupted
   earlier coverage.
2. **Decision:** the hourly digest (at hh:05, Asia/Bangkok; the same UTC hour)
   seals exactly one `forward_cuts` record before Telegram send during
   2026-10-31 00:00 UTC through 2026-11-30 00:00 UTC. Manual late runs outside
   minutes 05–08 cannot backfill the period. A first cut is never overwritten
   on retry. All wallet IDs come from the frozen private roster, not latest
   winner rankings.
3. **Hypothesis:** `forward_arms` holds every eligible instrument with a
   frozen research-only direction or typed ABSTAIN. Eligible latest as-known
   wallet/instrument actions are OPEN, ADD, or FLIP new-side; source reductions,
   CLOSE and loss of coverage are not opposite-direction entries. Three
   distinct observed wallet addresses with 2:1 and 2-address majority are
   the preregistered unqualified thresholds, not independent pods. A
   dominating de-risk/exit group vetoes. Failed coverage causes ABSTAIN.
   Unobserved assets are not negative return observations.
4. **Baseline:** cash, long-only, lagged 15-minute trend, lagged one-hour
   trend, and lagged one-hour reversal are frozen using only reference mids
   that existed before the **same decision cut**. Missing/stale lookback
   remains null. A later evaluator cannot revise original baseline direction.
   Momentum-flat direction is valid cash and incurs no round trip.
5. **Delivery:** `forward_delivery` records an append-only Telegram HTTP
   send attempt and acknowledgement. It is **not proof a user's device
   received/read the message**. The message includes all frozen research
   candidate instruments or the forward arm is not acknowledged as displayed.
   Ambiguous earlier attempts censor the entire hourly candidate despite a
   later confirmed API acknowledgement.
6. **Outcome:** a separate timer writes `forward_evaluations` in the
   independent `copy-forward-hourly.db`. The candidate can only start at
   `max(decision_at, HTTP_ack_at)+5s`; minute-sampled entry mid must arrive
   within 90 seconds. **The one-hour exit is measured from that actual
   delayed sampled entry**, not from raw source action or assumed two-second
   reaction. Secondary 15m and 4h are separate immutable horizon revisions.
   Missing delivery, late acknowledgement, stale/outage source, unavailable
   entry or exit, and unqualified samples are CENSORED, never forced to zero.
   Net = signed gross mid return minus 10-bps modeled roundtrip cost once.
   Costs 5/10/20/30bps and matched controls share the exact entry and exit.
   Funding, spread, impact, actual bid/ask, liquidity and prop-broker fills
   **are not observed**; these remain hypothetical diagnostic returns.
7. **Assessment:** one-shot, protocol-locked evaluation not earlier than
   2026-12-02 00:00 UTC. Automated systemd one-shot runs at
   **2026-12-02 00:10 UTC** and writes an exclusive mode-0600 aggregate
   report under
   `~/.local/share/copytrade/reports/wp3-20261030/assessment/final-report.json`.
   It checks denominators, independent UTC days and non-overlapping
   instrument buckets, costs, cash and market controls, and exploratory Holm
   day-sign-flip comparison. Source-stream completeness, verified end-user
   delivery, independent clusters, actual execution prices, and prop-platform
   constraints are still *explicit unqualified gates*, so no automatic
   positive-alpha/trading verdict can be issued.

## Operations / verification

```bash
# Read only, no post-freeze signal reconstruction
uv run --frozen --no-sync python -m ensemble.copy_forward verify
uv run --frozen --no-sync python -m ensemble.copy_forward_assess status
uv run --frozen --no-sync python -m ensemble.copy_shadow health
uv run --frozen --no-sync python -m ensemble.copy_forward_evidence list --limit 30
uv run --frozen --no-sync python -m ensemble.copy_forward_evidence storage

systemctl --user is-active copytrade-tracker.service copytrade-ensemble.service \
  copytrade-flow-digest.timer copytrade-forward-evaluate.timer \
  copytrade-forward-assess.timer
systemctl --user list-timers 'copytrade*'
systemctl --user show copytrade-flow-digest.service -p ExecStart
```

The forward journal is a different SQLite file, not an extension/rewrite of
`copy-opportunities-live.db` or `copy-outcomes-live.db`. It contains no
public wallet list, but it must stay private with mode-0600 permissions.
Raw user wallet universes and private protocol artifacts must not be checked
into the public feature branch. Production systemd units must run from one
**pinned** release independent of the research checkout. No standalone
trading gateway is present or permitted.

The existing descriptive digest remains functional if research capture
fails; it must say research measurement is unavailable. A missed forward
cut cannot be marked as a successful period by a later backfill. If the
holdout start is missed for a material portion of the required sample, retain
the missing period, and register a **new** future interval; never silently
amend the WP3 seal.

**Incident / rollback:** disable only `copytrade-forward-evaluate.timer`
and `copytrade-forward-assess.timer` if the new forward research path
fails; preserve raw ledger, forecast DB and Telegram delivery receipts.
For a software rollback use the pinned-release installer backup and
write a new restart/coverage epoch. A research-only failure may not promote
execution or erase previously recorded observations.
