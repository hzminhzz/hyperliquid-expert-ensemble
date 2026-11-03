# Trader Intelligence Telegram research-only digest release

This is a **digest-only**, reversible opt-in deployment. It does not activate A/B, any capital allocation, or broker orders. The running real-time `copytrade-ensemble.service`, `copytrade-tracker.service`, collectors and their timers are deliberately left untouched.

## Runtime evidence

- The existing `copytrade-flow-digest.timer` fires the hourly descriptive message.
- The new opt-in flag `--ti-research-overlay` calls `trader_grade_producer.build_research_grades` with the **existing** read-only as-known `copy-opportunities-live.db` journal and both exact hourly board snapshots. It imports **pinned** `copytrade-pm` source and reuses `TraderIntelligence.evaluate` and `assessment_document_for_board`.
- This display roster is assembled at the actual digest cut and **is not scientifically predeclared**. Every candidate has `DYNAMIC_ROSTER_NOT_INDEPENDENTLY_REGISTERED`, missing book/equity stays unknown, independence remains unverified, and **only C/X at 0R** can be rendered. No follower PnL or calibrated A/B is synthesized.
- The new private in-process JSON artifact must pass the **existing exact SHA, clock, model/policy pin, C/X and source-board validator**. A failure preserves the original descriptive message and logs `FALLBACK_...` in `grade_status`.
- The digest's **original period-level SQLite deduplication, delivery-ambiguity behavior, forward capture and text** remain unchanged; no new Telegram send path or direct order interface is introduced. If no fresh grade-eligible watch exists, display a truthful C/X research-only note.

## Pin, stage and smoke

From a clean, verified ensemble worktree on the optional Telegram PR branch (not `main`), with the tested clean PM Trader Intelligence PR branch:

```bash
uv run --group dev python -m pytest -q tests
uv run --group dev ruff check src tests deploy
uv run --group dev python -m deploy.trader_intelligence_digest_release stage \
  --ensemble-repo /home/quant/.devspace/worktrees/hyperliquid-expert-ensemble-1b51c0ba \
  --pm-repo /home/quant/.devspace/worktrees/copytrade-pm-6307ed53
```

`stage` archives both exact clean Git SHAs and installs the ensemble environment **offline** into `~/.local/share/copytrade/releases/ti-digest-ens-<sha>-pm-<sha>/`. It never installs or changes a systemd unit. Verify real-journal, no-network dry-run using the staged source:

```bash
uv run --group dev python -m deploy.trader_intelligence_digest_release smoke \
  --release /home/quant/.local/share/copytrade/releases/ti-digest-ens-<sha>-pm-<sha>
```

## Apply and rollback

With explicit operator approval, and only if the dry-run, active live source, current tracker and ensemble pass:

```bash
uv run --group dev python -m deploy.trader_intelligence_digest_release activate --apply \
  --release /home/quant/.local/share/copytrade/releases/ti-digest-ens-<sha>-pm-<sha>
```

The program installs one managed systemd drop-in for **`copytrade-flow-digest.service`**, changing its executable and working directory to the new pinned release and setting a private `PYTHONPATH` to the sealed PM source. It briefly stops/starts only `copytrade-flow-digest.timer` to reload the override. It does **not** restart either continuous runtime. Its `activation.json` records hashes, live dry-run and verified unchanged main service PIDs. Existing private environment files and the old release stay preserved.

Revert *only this overlay*:

```bash
uv run --group dev python -m deploy.trader_intelligence_digest_release rollback --apply \
  --release /home/quant/.local/share/copytrade/releases/ti-digest-ens-<sha>-pm-<sha>
```

This removes the managed drop-in or restores its prior copy and reloads the digest timer. It never rewrites journal evidence, previous Telegram messages, the existing main ensemble release, or the tracker.

## Acceptance levels

1. **ENGINEERED:** tests, lint, build and pinned source SHA match.
2. **VERIFIED DRY-RUN:** actual as-known journal and message rendering with injected non-network sender; the grade status is `RESEARCH_CX_OVERLAY` or `RESEARCH_CX_NO_CANDIDATE`.
3. **DEPLOYED CONFIGURATION:** systemd digest `ExecStart` resolves to staged immutable source, timer active, real-time ensemble/tracker unchanged.
4. **DELIVERED:** a genuine Telegram `sendMessage` acknowledgement for the new period confirms a new overlay message, not merely an active timer. Delivery state must be checked from the actual live digest logs and SQLite journal.
5. **SCIENTIFICALLY QUALIFIED:** **NO**; the unqualified P1 uncertainty/source-coverage evidence and future-outcome blockers are unchanged.

**Caution:** Do not bypass period deduplication to force a duplicate Telegram message. No A/B or financial risk activation follows from a successful research display.
