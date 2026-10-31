"""Read-only PM-01 -> PM-02 -> PM-03 research handoff contract.

A valid engineering handshake permits building synthetic account simulators.
It does not certify any wallet, live risk budget, or executable prop account.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path

from .pm_research import _write_private_evidence

SCHEMA = "pm01_pm02_pm03_research_handoff_v1"


def _utc(timestamp: str) -> datetime:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("UNVERIFIED_HANDOFF_TIMESTAMP")
    return parsed.astimezone(UTC)


def _read_sealed(path: Path, prefix: str, max_bytes: int = 70_000_000) -> dict:
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise ValueError("OVERSIZED_EVIDENCE_ARTIFACT")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("MALFORMED_HANDOFF_EVIDENCE")
    if prefix == "pm02-":
        # PM-02 writes the SHA of its canonical report *excluding* the
        # embedded sha256 field, unlike the PM-01 raw-byte evidence writer.
        expected = f"pm02-{value.get('sha256')}.json"
    else:
        expected = f"{prefix}{sha256(raw).hexdigest()[:20]}.json"
    if path.name != expected:
        raise ValueError("HANDOFF_SOURCE_HASH_FILENAME_MISMATCH")
    if path.stat().st_mode & 0o077:
        raise ValueError("NONPRIVATE_HANDOFF_EVIDENCE")
    return value


def validate_handoff(replay: dict, tape: dict, pm02: dict) -> dict:
    """Produce a versioned dependency verdict, never action instructions."""
    if replay.get("schema") != "pm01_fill_only_replay_v2":
        raise ValueError("PM01_BREAKING_CONSUMER_SCHEMA")
    if tape.get("schema") != "pm01_observed_book_tape_v1":
        raise ValueError("PM01_MISSING_SOURCE_POSITION_TAPE")
    if pm02.get("schema") != "pm02_manager_governance_v1":
        raise ValueError("PM02_MISSING_RISK_CONTRACT")
    if (
        replay.get("wallet_count") != 36
        or tape.get("wallet_count") != 36
        or len(replay.get("rows", [])) != 108
        or len(tape.get("wallets", [])) != 36
        or len(pm02.get("cards", [])) != 36
    ):
        raise ValueError("INCOMPLETE_PM01_PM02_COHORT")
    if tape.get("source_manifests_sha256") != replay.get("source_contract_shas"):
        raise ValueError("SOURCE_MANIFEST_GENERATION_DIVERGED")
    if tape.get("cutoff") != replay.get("study_cutoff"):
        raise ValueError("PM01_FROZEN_STUDY_CUTOFF_DRIFT")
    if _utc(pm02["asof"]) < _utc(replay["study_cutoff"]):
        raise ValueError("PM02_DECISION_BEFORE_PM01_CUTOFF")
    if pm02.get("live_trading_authorized") is not False:
        raise ValueError("PM02_CANNOT_AUTHORIZE_LIVE_TRADING")
    if replay.get("never_claim_prospective_alpha") is not True:
        raise ValueError("SOURCE_PROSPECTIVE_ORACLE_CLAIM")
    if tape.get("pm03_actionable_targets_available") is not False:
        raise ValueError("UNRECONCILED_TAPE_ACTION_AUTHORITY")
    if any(
        x.get("causal_follower_actions_allowed") is not False
        or x.get("source_first_known_at") is not None
        for x in tape["wallets"]
    ):
        raise ValueError("FILL_TAPE_FORGED_PROSPECTIVE_CLOCK")
    if replay.get("prospective_qualified_wallet_count") != 0:
        raise ValueError("NO_INDEPENDENT_PROSPECTIVE_VALIDATION")
    replay_aliases = {r.get("wallet") for r in replay["rows"]}
    tape_aliases = {w.get("wallet") for w in tape["wallets"]}
    pm02_aliases = {c.get("wallet") for c in pm02["cards"]}
    if (
        len(replay_aliases) != 36
        or len(tape_aliases) != 36
        or len(pm02_aliases) != 36
        or replay_aliases != tape_aliases
        or tape_aliases != pm02_aliases
    ):
        raise ValueError("CROSS_STAGE_WALLET_IDENTITIES_DIVERGED")
    from collections import Counter

    if Counter(w.get("cohort") for w in tape["wallets"]) != Counter(
        {"original": 6, "wave1": 10, "wave2": 20}
    ):
        raise ValueError("MISSING_36_WALLET_SUBCOHORT")
    for card in pm02["cards"]:
        if card.get("wallet") not in replay_aliases:
            raise ValueError("PM02_UNKNOWN_WALLET")
    # Report the actual PM-02 risk budget; never make it into trade authority.
    seats = pm02.get("five_seats")
    if not isinstance(seats, list) or len(seats) != 5:
        raise ValueError("PM02_FIVE_SEAT_POLICY_MISSING")
    actual_budget = sum(float(x.get("risk_budget_r", 0)) for x in seats)
    if actual_budget < 0:
        raise ValueError("NEGATIVE_PM_BUDGET")
    if any(x.get("authority") != "RESEARCH_PROPOSAL_ONLY" for x in seats):
        raise ValueError("PM02_PROPOSAL_AUTHORITY_CHANGED")
    if actual_budget != 0 or pm02.get("qualified_count") != 0:
        # This frozen historical PM-01 cannot authorize a positive risk budget
        # no matter what a separate manager optimizer claims.
        raise ValueError("UNQUALIFIED_PM02_REAL_ALLOCATIONS")
    if any(x.get("wallet") is not None for x in seats):
        raise ValueError("PM02_SHOULD_RETAIN_EMPTY_SEATS")
    return {
        "schema": SCHEMA,
        "status": "RESEARCH_ENGINEERING_CONTRACT_VALID_PROSPECTIVE_BLOCKED",
        "wallet_count": 36,
        "pm01_observed_tape_ready": True,
        "pm02_governance_artifact_ready": True,
        "pm03_synthetic_engineering_can_start": True,
        "pm03_historical_shadow_diagnostics_can_start": True,
        "pm03_account_risk_backtest_with_real_actionable_pm_targets": False,
        "pm03_live_account_authorized": False,
        "source_follower_first_known_receipt_clocks": 0,
        "pm02_qualified_managers": 0,
        "pm02_approved_risk_budget_r": 0,
        "pm02_research_risk_proposal_r": actual_budget,
        "pm03_data_readiness_blockers": [
            "NO_VERIFIED_PROSPECTIVE_FIRST_KNOWN_WALLET_ACTIONS",
            "INCOMPLETE_ACCOUNT_EQUITY_FUNDING_COLLATERAL",
            "UNRESOLVED_WALLET_POSITION_CHAINS",
            "UNVERIFIED_LIQUIDITY_EXECUTION_AND_MISSING_MARKET_DAYS",
            "PM02_HAS_ZERO_QUALIFIED_MANAGER_RISK_PROPOSALS",
            "PROPR_CONTRACT_AND_EXECUTION_RESTRICTIONS_NOT_APPROVED",
        ],
        "user_permission_scope": "READ_ONLY_RESEARCH_NO_LIVE_ORDERS",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pm01", type=Path, required=True)
    parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--pm02", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    replay = _read_sealed(args.pm01, "pm01-36-wallets-")
    tape = _read_sealed(args.tape, "pm01-36-wallets-")
    pm02 = _read_sealed(args.pm02, "pm02-")
    # PM-02 source digests are canonical-JSON rather than raw file SHA.
    pm01_digest = sha256(
        json.dumps(replay, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    if pm02.get("pm01_source_sha256") != pm01_digest:
        raise ValueError("PM02_PM01_SOURCE_ARTIFACT_CONFLICT")
    digest2 = sha256(
        json.dumps(
            {k: v for k, v in pm02.items() if k != "sha256"},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    if pm02.get("sha256") != digest2:
        raise ValueError("PM02_ALTERED_GOVERNANCE_REPORT")
    verdict = validate_handoff(replay, tape, pm02)
    verdict["pm01_raw_sha256"] = sha256(args.pm01.read_bytes()).hexdigest()
    verdict["tape_raw_sha256"] = sha256(args.tape.read_bytes()).hexdigest()
    verdict["pm02_raw_sha256"] = sha256(args.pm02.read_bytes()).hexdigest()
    out, digest = _write_private_evidence(args.out, verdict)
    print(
        json.dumps(
            {
                "status": verdict["status"],
                "account_backtest_ready": verdict[
                    "pm03_account_risk_backtest_with_real_actionable_pm_targets"
                ],
                "sha256": digest,
                "file": str(out),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
