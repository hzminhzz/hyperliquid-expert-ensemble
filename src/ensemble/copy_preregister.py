"""Freeze WP3 prospective experiment before the first eligible holdout decision.

A private, immutable protocol and as-known wallet roster are materialized once.
No future outcomes, live orders, Telegram sends or selection optimization.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PRIMARY_START = datetime(2026, 10, 31, tzinfo=UTC)
PRIMARY_END = datetime(2026, 11, 30, tzinfo=UTC)
ROSTER_FRESHNESS = timedelta(hours=2)
PRIMARY_POLICY = "DI2-shadow-v2"
BASELINES = (
    "cash", "long_only", "lagged_15m_trend", "lagged_1h_trend",
    "lagged_1h_reversal",
)


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj)).hexdigest()


def _git_head(repo: Path) -> str:
    dirty = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=repo,
        capture_output=True, text=True, check=True, timeout=20,
    )
    if dirty.stdout:
        raise RuntimeError("cannot seal protocol against a dirty research checkout")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True, timeout=20,
    )
    return head.stdout.strip()


@dataclass(frozen=True, slots=True)
class ForwardProtocol:
    frozen_at: datetime
    holdout_start: datetime
    holdout_end: datetime
    research_code_sha: str
    source_release_ensemble_sha: str
    source_release_tracker_sha: str
    source_ledger_id: str
    ledger_seq_cut: int
    opportunity_cursor_cut: int
    universe_sha256: str
    universe_wallet_count: int
    historical_report_sha256: str

    def __post_init__(self) -> None:
        dates = (self.frozen_at, self.holdout_start, self.holdout_end)
        if any(x.tzinfo is None or x.utcoffset() is None for x in dates):
            raise ValueError("all protocol clocks must be timezone aware")
        if not (self.frozen_at < self.holdout_start
                < self.holdout_end
                and self.holdout_end - self.holdout_start >= timedelta(days=28)):
            raise ValueError("freeze must precede at least 28 full holdout days")
        if self.universe_wallet_count < 10 or self.ledger_seq_cut < 1:
            raise ValueError("insufficient frozen wallet universe or source evidence")
        if self.opportunity_cursor_cut > self.ledger_seq_cut:
            raise ValueError("prospective cursor exceeds source cut")
        # Git deployment identifiers are 40-character SHA-1 commit IDs today;
        # content-addressed roster/report artifacts use 64-character SHA-256.
        for identity in (
            self.research_code_sha, self.source_release_ensemble_sha,
            self.source_release_tracker_sha,
        ):
            if len(identity) not in (40, 64) or not all(
                c in "0123456789abcdef" for c in identity
            ):
                raise ValueError("invalid Git provenance hash")
        for identity in (self.universe_sha256, self.historical_report_sha256):
            if len(identity) != 64 or not all(c in "0123456789abcdef" for c in identity):
                raise ValueError("invalid SHA-256 content hash")

    def payload(self) -> dict[str, Any]:
        return {
            "schema": "copytrade-WP3-frozen-protocol-v1",
            "frozen_at": self.frozen_at.isoformat(),
            "holdout": {
                "start_inclusive": self.holdout_start.isoformat(),
                "end_exclusive": self.holdout_end.isoformat(),
                "assessment_not_before": (
                    self.holdout_end + timedelta(hours=48)
                ).isoformat(),
                "one_locked_assessment": True,
                "min_utc_days": 20,
                "min_instrument_4h_buckets": 100,
                "max_single_instrument_bucket_share": 0.40,
                "max_single_wallet_bucket_share": 0.40,
                "exploratory_after_failed_gate": "REVISE_ON_A_NEW_HOLDOUT",
                "no_peeking_at_forward_outcomes": True,
            },
            "source_cut": {
                "ledger_id": self.source_ledger_id,
                "last_seq": self.ledger_seq_cut,
                "shadow_cursor": self.opportunity_cursor_cut,
                "universe_wallets": self.universe_wallet_count,
                "universe_sha256": self.universe_sha256,
                "historical_attribution_sha256": self.historical_report_sha256,
                "research_code_commit": self.research_code_sha,
                "collector_release_ensemble_commit": self.source_release_ensemble_sha,
                "tracker_release_commit": self.source_release_tracker_sha,
            },
            "population": {
                "design": "ALL_FRESH_VALIDATED_OBSERVER_WALLETS_AS_KNOWN_AT_FREEZE",
                "wallet_selection": "NONE_BY_HISTORICAL_PNL_OR_HOLDOUT_LABELS",
                "new_wallets_after_cut": "EXCLUDED",
                "exits_and_gap_markers": "RETAINED_AS_KNOWN_NEVER_INVENT_TRADER_EXITS",
                "cluster_independence": "UNVERIFIED_GATE_BLOCKS_QUALIFICATION",
                "source_history_completeness": "UNVERIFIED_GATE_BLOCKS_QUALIFICATION",
            },
            "policy": {
                "source_opportunity_revision": PRIMARY_POLICY,
                "primary": "hourly_digest_directional_watch",
                "secondary": "first_actionable_event_entry",
                "decision_status": "NOT_EMITTING_FROZEN_HOURLY_FORECASTS_UNTIL_WP4",
                "eligible_source_actions": ["OPEN", "ADD", "FLIP_NEW_SIDE_ONLY"],
                "no_copy_source_actions": ["REDUCE", "CLOSE", "INVALID", "GAP"],
                "instrument": "HYPERLIQUID_MAINNET_PERP_DEFAULT_ONLY",
                "hourly_decision_time": "EVERY_HH_05_ASIA_BANGKOK",
                "hourly_window": "PRECEDING_60M_KNOWN_AT",
                "hourly_support_min_distinct_wallets": 3,
                "hourly_dominance_ratio_min": 2,
                "hourly_dominance_margin_min": 2,
                "de_risk_veto": "UNWIND_WALLETS_GE_DIRECTIONAL_SUPPORT",
                "first_known_cut": "SOURCE_KNOWN_AT_NO_EX_POST_REVISIONS",
                "delivery_time": "ACTUAL_OBSERVED_TELEGRAM_DELIVERY_TIME_REQUIRED",
                "follower_entry_ready": "MAX(DECISION_KNOWN_AT,DELIVERY_AT)+5S",
                "no_delivery": "CENSORED_NOT_RETROSPECTIVELY_ACTIONABLE",
                "invalidation": "SOURCE_CLOSE_OR_FLIP_OR_OUTAGE_BEFORE_ENTRY_VETO",
                "exit_rule": "FIXED_1H_FROM_ACTUAL_DELAYED_ENTRY",
                "multi_wallet_events": "ONE_INSTRUMENT_PER_FIXED_4H_UTC_BUCKET",
                "portfolio_execution": False,
            },
            "comparison": {
                "primary_horizon": "1h",
                "secondary_horizons": ["15m", "4h"],
                "baseline_families": list(BASELINES),
                "baseline_entry_exit": "IDENTICAL_TO_FOLLOWER_ARM",
                "direction_known_before_decision": True,
                "predecision_quote_max_age_seconds": 90,
                "market_lookback_minutes": [15, 60],
                "momentum_flat_control": "CASH_ZERO_COST",
                "cost_bps_round_trip_base": 10,
                "cost_bps_sensitivity": [5, 20, 30],
                "fee_spread_slippage": "AGGREGATE_MODELED_NOT_OBSERVED",
                "funding_and_impact": "NOT_MEASURED_GATE_BLOCKS_EXECUTABLE_CLAIM",
                "price_source": "MINUTE_MID_DIAGNOSTIC_ONLY",
                "side_specific_quotes": "REQUIRED_BEFORE_EXECUTABLE_RESULT",
                "max_entry_or_exit_mid_lateness_seconds": 90,
                "labels_after_maturity": True,
                "missing_price_or_source": "CENSORED_NOT_ZERO",
                "cash_net_return": 0,
                "no_trade_return": "ZERO_SEPARATE_ABSTENTION_DENOMINATOR",
            },
            "estimands": {
                "primary": "EQUAL_UTC_DAY_MEAN_OF_DEDUPED_HOURLY_FOLLOWER_NET_BPS",
                "incremental": "MATCHED_EQUAL_DAY_NET_VS_EACH_PREDECLARED_MARKET_CONTROL",
                "market_contribution": "SAME_ENTRY_EXIT_LONG_ONLY_MARKET_NET",
                "gross_and_cost": "REPORTED_SEPARATELY_NO_DOUBLE_COUNT",
                "denominators": [
                    "ELIGIBLE_DECISIONS", "NO_TRADE", "CENSORED", "PRICE_MISSING",
                    "MATURED", "PAIRED_PER_BASELINE", "UTC_DAYS",
                    "INSTRUMENT_BUCKETS", "SOURCE_VERIFIED_BUCKETS",
                ],
                "calendar_and_overlap": "ONE_INSTRUMENT_PER_4H_FIXED_BUCKET; DAILY_BLOCKS",
                "weighting": "EQUAL_DAY_NOT_RAW_EVENT_WEIGHTED",
                "uncertainty": {
                    "bootstrap_unit": "UTC_DAY",
                    "draws": 2000,
                    "seed": 20261030,
                    "interval": "95_PERCENTILE",
                    "multiple_comparisons": "HOLM_FAMILYWISE_0.05",
                    "null": "NO_POSITIVE_FOLLOWER_NET_OR_INCREMENTAL_EDGE",
                },
            },
            "success_rule": {
                "mandatory_economic": [
                    "DAY_BOOTSTRAP_LOWER_CI_WALLET_NET_GT_0",
                    "HOLM_ADJUSTED_INCREMENTAL_VS_ALL_PREDECLARED_CONTROLS_GT_0",
                    "MODEL_20BPS_WALLET_NET_MEAN_GT_0",
                ],
                "mandatory_scientific": [
                    "SOURCE_COVERAGE_VERIFIED",
                    "QUOTE_DEPTH_OR_VERIFIED_FOLLOWER_FILL_COSTS",
                    "INDEPENDENT_CLUSTER_EVIDENCE",
                    "SAMPLE_SUPPORT_THRESHOLDS",
                    "ALL_REJECTED_ARMS_RETAINED",
                    "NO_INTERIM_PEAKING",
                ],
                "promotion": "BLOCKED_UNTIL_NEW_HUMAN_APPROVAL",
                "execution": "NEVER_AUTOMATIC",
            },
            "prior_selection": {
                "status": "EXPLORATORY_MANY_WALLETS_POLICIES_AND_HORIZONS_ALREADY_INSPECTED",
                "trial_count": "NOT_RELIABLY_ENUMERATED",
                "historical_alpha": "NOT_QUALIFIED",
                "candidate_family_count": 2,
                "frozen_familywise_hypotheses": len(BASELINES),
            },
        }

    def sealed(self) -> dict[str, Any]:
        payload = self.payload()
        return {
            "protocol_id": "wp3:" + _sha(payload)[:20],
            "content_sha256": _sha(payload),
            "protocol": payload,
        }


def fresh_universe(
    ledger_db: Path, *, cut: datetime,
) -> tuple[str, int, list[str]]:
    """Private roster: point-in-time account snapshots, no labels or rankings."""
    if cut.tzinfo is None:
        raise ValueError("source cut requires timezone")
    with closing(sqlite3.connect(
        f"file:{ledger_db}?mode=ro", uri=True, timeout=20,
    )) as db:
        meta = db.execute(
            "SELECT ledger_id,current_seq FROM ledger_meta LIMIT 1"
        ).fetchone()
        if meta is None or int(meta[1]) < 1:
            raise RuntimeError("source ledger unavailable")
        cutoff = cut.isoformat()
        earliest = (cut - ROSTER_FRESHNESS).isoformat()
        rows = db.execute(
            "SELECT DISTINCT wallet_id FROM outbox_events "
            "WHERE kind='ACCOUNT_SNAPSHOT' AND quality='valid' "
            "AND known_at>=? AND known_at<=? AND seq<=? "
            "ORDER BY wallet_id",
            (earliest, cutoff, int(meta[1])),
        ).fetchall()
    wallets = sorted({str(row[0]).lower() for row in rows})
    if len(wallets) < 10:
        raise RuntimeError("cannot freeze a sufficiently observed wallet universe")
    return str(meta[0]), int(meta[1]), wallets


def freeze(
    *, ensemble_repo: Path, ledger_db: Path, journal_db: Path,
    release_manifest: Path, historical_report: Path, destination: Path,
    holdout_start: datetime = PRIMARY_START, holdout_end: datetime = PRIMARY_END,
    now: datetime | None = None,
) -> dict[str, Any]:
    source_release = json.loads(release_manifest.read_text())
    if source_release.get("schema") != "copytrade-pinned-release-v1":
        raise RuntimeError("unverified production release provenance")
    research_sha = _git_head(ensemble_repo)
    # Resolve identity, then capture the shadow cursor BEFORE the final ledger
    # sequence. Otherwise an actively moving collector may outrun an earlier
    # source meta read, producing an impossible source-cut < shadow-cut.
    with closing(sqlite3.connect(
        f"file:{ledger_db}?mode=ro", uri=True, timeout=20,
    )) as db:
        source = db.execute("SELECT ledger_id FROM ledger_meta LIMIT 1").fetchone()
        if source is None:
            raise RuntimeError("source ledger not initialized")
        ledger_id = str(source[0])
    with closing(sqlite3.connect(
        f"file:{journal_db}?mode=ro", uri=True, timeout=20,
    )) as db:
        row = db.execute(
            "SELECT last_seq FROM opportunity_state WHERE ledger_id=?",
            (ledger_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("prospective journal not initialized")
        shadow_seq = int(row[0])
    frozen_at = now or datetime.now(UTC)
    if frozen_at.tzinfo is None or frozen_at >= holdout_start:
        raise ValueError("future holdout must not have begun")
    actual_ledger_id, seq, wallets = fresh_universe(ledger_db, cut=frozen_at)
    if actual_ledger_id != ledger_id:
        raise RuntimeError("source ledger changed during preregistration")
    if shadow_seq > seq:
        raise RuntimeError("journal cursor outran current source cut; retry after reconciliation")
    roster = {
        "schema": "wp3-private-wallet-universe-v1",
        "source_ledger": ledger_id,
        "cut_seq": seq,
        "frozen_at": frozen_at.isoformat(),
        "wallets": wallets,
        "note": "OBSERVED_ROSTER_NOT_PROOF_OF_SOURCE_HISTORY_COMPLETENESS",
    }
    if not historical_report.exists():
        raise RuntimeError("historical analysis must precede the freeze")
    historical_hash = hashlib.sha256(historical_report.read_bytes()).hexdigest()
    protocol = ForwardProtocol(
        frozen_at=frozen_at, holdout_start=holdout_start,
        holdout_end=holdout_end,
        research_code_sha=research_sha,
        source_release_ensemble_sha=source_release["ensemble_sha"],
        source_release_tracker_sha=source_release["tracker_sha"],
        source_ledger_id=ledger_id,
        ledger_seq_cut=seq,
        opportunity_cursor_cut=shadow_seq,
        universe_sha256=_sha(roster), universe_wallet_count=len(wallets),
        historical_report_sha256=historical_hash,
    ).sealed()
    if destination.exists():
        raise FileExistsError("immutable WP3 preregistration already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".freeze-wp3-", dir=destination.parent))
    stage.chmod(0o700)
    try:
        for name, obj in (
            ("protocol.json", protocol),
            ("wallet-universe.PRIVATE.json", roster),
        ):
            target = stage / name
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(json.dumps(obj, sort_keys=True, indent=2).encode() + b"\n")
        if destination.exists():
            raise FileExistsError("concurrent freeze destination")
        stage.rename(destination)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return {
        "status": "FROZEN_COLLECTION_ONLY_AWAIT_WP4",
        "destination": str(destination),
        "protocol_id": protocol["protocol_id"],
        "content_sha256": protocol["content_sha256"],
        "wallet_count": len(wallets),
        "source_seq": seq,
        "journal_cursor": shadow_seq,
        "holdout_start": holdout_start.isoformat(),
        "holdout_end": holdout_end.isoformat(),
        "predictive_qualified": False,
        "execution_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ensemble-repo", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--historical-report", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--holdout-start", type=str, default=PRIMARY_START.isoformat())
    parser.add_argument("--holdout-end", type=str, default=PRIMARY_END.isoformat())
    args = parser.parse_args()
    result = freeze(
        ensemble_repo=args.ensemble_repo, ledger_db=args.ledger,
        journal_db=args.journal, release_manifest=args.release,
        historical_report=args.historical_report,
        destination=args.destination,
        holdout_start=datetime.fromisoformat(args.holdout_start),
        holdout_end=datetime.fromisoformat(args.holdout_end),
    )
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
