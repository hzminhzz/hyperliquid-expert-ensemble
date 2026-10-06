"""V26 qualification: frozen paired ablation program (Q13-Q16)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ensemble.ablation import (
    AblationArchive,
    AblationFamily,
    AblationObservation,
    AblationRegistration,
    CandidateStatus,
    default_v26_registrations,
    evaluate_ablation,
    validate_required_families,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOLDOUT = T0 + timedelta(days=30)


def registration(
    *,
    candidate: str = "candidate",
    min_samples: int = 3,
    min_delta: str = "0.001",
    max_search_count: int = 3,
) -> AblationRegistration:
    return AblationRegistration(
        registration_id=f"reg:{candidate}",
        family=AblationFamily.LADDER,
        baseline_feature="baseline",
        candidate_feature=candidate,
        holdout_start=HOLDOUT,
        holdout_end=HOLDOUT + timedelta(days=30),
        primary_metric="mean_directional_return",
        min_samples=min_samples,
        min_delta=Decimal(min_delta),
        max_search_count=max_search_count,
        latency_ms=200,
        cost_bps=Decimal(5),
        frozen_at=HOLDOUT - timedelta(seconds=1),
    )


def observation(i: int, ret: str, baseline: str | None, candidate: str | None):
    return AblationObservation(
        source_id=f"s:{i}",
        known_at=HOLDOUT + timedelta(days=i),
        forward_net_return=Decimal(ret),
        features=(
            ("baseline", None if baseline is None else Decimal(baseline)),
            ("candidate", None if candidate is None else Decimal(candidate)),
        ),
    )


def test_q13_paired_complete_cases_use_identical_samples():
    result = evaluate_ablation(
        registration(),
        [
            observation(0, "0.01", "1", "1"),
            observation(1, "0.02", "1", None),
            observation(2, "-0.01", "-1", "-1"),
            observation(3, "0.03", "1", "1"),
        ],
        search_count=1,
    )

    assert result.paired_samples == 3
    assert result.evaluated_source_ids == ("s:0", "s:2", "s:3")


def test_q13_candidate_can_promote_or_reject_only_by_frozen_rule():
    observations = [
        observation(0, "0.02", "-1", "1"),
        observation(1, "0.03", "-1", "1"),
        observation(2, "0.01", "-1", "1"),
    ]
    promoted = evaluate_ablation(registration(min_delta="0.01"), observations, search_count=1)
    assert promoted.status is CandidateStatus.PROMOTE
    assert promoted.reason == "PREDECLARED_DELTA_CLEARED"

    rejected = evaluate_ablation(
        registration(candidate="candidate", min_delta="0.10"),
        observations,
        search_count=1,
    )
    assert rejected.status is CandidateStatus.REJECT
    assert rejected.reason == "NO_INCREMENTAL_HOLDOUT_VALUE"


def test_q14_insufficient_holdout_is_retained_as_inconclusive():
    result = evaluate_ablation(
        registration(min_samples=10),
        [observation(0, "0.01", "1", "1")],
        search_count=1,
    )

    assert result.status is CandidateStatus.INCONCLUSIVE
    assert result.reason == "INSUFFICIENT_HOLDOUT_SUPPORT"
    assert result.paired_samples == 1


def test_q15_search_budget_violation_rejects_candidate_and_records_count():
    result = evaluate_ablation(
        registration(max_search_count=2),
        [
            observation(0, "0.01", "1", "1"),
            observation(1, "0.01", "1", "1"),
            observation(2, "0.01", "1", "1"),
        ],
        search_count=3,
    )

    assert result.status is CandidateStatus.REJECT
    assert result.reason == "SEARCH_BUDGET_EXCEEDED"
    assert result.search_count == 3


def test_q16_mandatory_ablation_families_are_registered_before_holdout():
    registrations = default_v26_registrations(
        holdout_start=HOLDOUT,
        holdout_end=HOLDOUT + timedelta(days=30),
        frozen_at=HOLDOUT - timedelta(days=1),
        min_samples=50,
        min_delta=Decimal("0.001"),
        max_search_count=7,
        latency_ms=200,
        cost_bps=Decimal(5),
    )
    validate_required_families(registrations)

    families = {item.family for item in registrations}
    assert AblationFamily.RAW_VS_CLIPPED in families
    assert AblationFamily.STATE_VS_FLOW in families
    assert AblationFamily.STATE_PLUS_FLOW in families
    assert AblationFamily.EQUAL_VS_CLUSTER in families
    assert AblationFamily.ABSOLUTE_VS_RELATIVE in families
    assert AblationFamily.LATENCY_COST in families
    assert AblationFamily.EXPERT_DROPOUT in families


def test_negative_and_inconclusive_results_survive_restart(tmp_path: Path):
    result = evaluate_ablation(
        registration(min_samples=10),
        [observation(0, "0.01", "1", "1")],
        search_count=2,
    )
    path = tmp_path / "ablation.db"

    first = AblationArchive(path)
    assert first.append(result) is True
    assert first.append(result) is False
    first.close()

    reopened = AblationArchive(path)
    try:
        assert reopened.count() == 1
        conflicting = type(result)(
            registration_id=result.registration_id,
            registration_hash="different",
            status=result.status,
            paired_samples=result.paired_samples,
            baseline_metric=result.baseline_metric,
            candidate_metric=result.candidate_metric,
            metric_delta=result.metric_delta,
            search_count=result.search_count,
            reason=result.reason,
            evaluated_source_ids=result.evaluated_source_ids,
        )
        with pytest.raises(ValueError, match="conflict"):
            reopened.append(conflicting)
    finally:
        reopened.close()
