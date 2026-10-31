"""PM-03 handoff validates entire cohort without granting research capital."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest

from ensemble.pm_handoff import validate_handoff


def _cohort_fixture():
    cohort = (
        [("original", f"O{i:02d}") for i in range(6)]
        + [("wave1", f"A{i:02d}") for i in range(10)]
        + [("wave2", f"B{i:02d}") for i in range(20)]
    )
    replay = {
        "schema": "pm01_fill_only_replay_v2",
        "wallet_count": 36,
        "source_contract_shas": {"roster": "a" * 64},
        "study_cutoff": datetime(2026, 10, 29, tzinfo=UTC).isoformat(),
        "never_claim_prospective_alpha": True,
        "prospective_qualified_wallet_count": 0,
        "rows": [
            {"wallet": alias, "cohort": group, "latency_minutes": latency}
            for group, alias in cohort
            for latency in (5, 15, 60)
        ],
    }
    tape = {
        "schema": "pm01_observed_book_tape_v1",
        "wallet_count": 36,
        "source_manifests_sha256": {"roster": "a" * 64},
        "cutoff": replay["study_cutoff"],
        "pm03_actionable_targets_available": False,
        "wallets": [
            {
                "wallet": alias,
                "cohort": group,
                "causal_follower_actions_allowed": False,
                "source_first_known_at": None,
            }
            for group, alias in cohort
        ],
    }
    pm02 = {
        "schema": "pm02_manager_governance_v1",
        "asof": datetime(2026, 10, 30, tzinfo=UTC).isoformat(),
        "cards": [{"wallet": alias} for _, alias in cohort],
        "qualified_count": 0,
        "live_trading_authorized": False,
        "five_seats": [
            {"wallet": None, "risk_budget_r": 0.0, "authority": "RESEARCH_PROPOSAL_ONLY"}
            for _ in range(5)
        ],
    }
    return replay, tape, pm02


def test_real_state_allows_pm03_synthetic_research_but_never_live_risk():
    report = validate_handoff(*_cohort_fixture())
    assert report["pm03_synthetic_engineering_can_start"]
    assert report["pm03_historical_shadow_diagnostics_can_start"]
    assert report["pm03_account_risk_backtest_with_real_actionable_pm_targets"] is False
    assert report["pm03_live_account_authorized"] is False
    assert report["pm02_approved_risk_budget_r"] == 0
    assert (
        "NO_VERIFIED_PROSPECTIVE_FIRST_KNOWN_WALLET_ACTIONS"
        in report["pm03_data_readiness_blockers"]
    )


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (
            lambda a, b, c: a.update(schema="pm01_fill_only_replay_v3"),
            "PM01_BREAKING_CONSUMER_SCHEMA",
        ),
        (
            lambda a, b, c: b.update(pm03_actionable_targets_available=True),
            "UNRECONCILED_TAPE_ACTION_AUTHORITY",
        ),
        (
            lambda a, b, c: c["five_seats"][0].update(risk_budget_r=0.1, wallet="O00"),
            "UNQUALIFIED_PM02_REAL_ALLOCATIONS",
        ),
        (
            lambda a, b, c: b["wallets"][0].update(source_first_known_at="2026-10-29T00:00:00Z"),
            "FILL_TAPE_FORGED_PROSPECTIVE_CLOCK",
        ),
        (
            lambda a, b, c: a["rows"][0].update(wallet="FOREGED"),
            "CROSS_STAGE_WALLET_IDENTITIES_DIVERGED",
        ),
        (
            lambda a, b, c: b["wallets"][0].update(wallet="FORGED"),
            "CROSS_STAGE_WALLET_IDENTITIES_DIVERGED",
        ),
        (
            lambda a, b, c: b.update(source_manifests_sha256={"roster": "b" * 64}),
            "SOURCE_MANIFEST_GENERATION_DIVERGED",
        ),
        (
            lambda a, b, c: c.update(live_trading_authorized=True),
            "PM02_CANNOT_AUTHORIZE_LIVE_TRADING",
        ),
        (
            lambda a, b, c: a.update(prospective_qualified_wallet_count=1),
            "NO_INDEPENDENT_PROSPECTIVE_VALIDATION",
        ),
    ],
)
def test_handoff_fail_closed_against_mismatched_stage(change, error):
    replay, tape, pm02 = copy.deepcopy(_cohort_fixture())
    change(replay, tape, pm02)
    with pytest.raises(ValueError, match=error):
        validate_handoff(replay, tape, pm02)
