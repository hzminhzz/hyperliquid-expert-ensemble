"""ER5 qualification: independent cadence/universe protocol and common-grid replay."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ensemble.cadence_experiment import (
    CadenceMode,
    Stage2Status,
    TimedPrediction,
    assess_stage2_readiness,
    common_grid_latest,
    default_er5_protocol,
    replay_actions,
)

T0 = datetime(2026, 10, 6, tzinfo=UTC)


def test_er5_requires_a_new_holdout_after_er4():
    with pytest.raises(ValueError, match="strictly after"):
        default_er5_protocol(
            representation_id="R1",
            frozen_at=T0,
            er4_holdout_end=T0 + timedelta(days=30),
            holdout_start=T0 + timedelta(days=30),
            holdout_end=T0 + timedelta(days=60),
        )

    protocol = default_er5_protocol(
        representation_id="R1",
        frozen_at=T0 + timedelta(days=30, hours=1),
        er4_holdout_end=T0 + timedelta(days=30),
        holdout_start=T0 + timedelta(days=31),
        holdout_end=T0 + timedelta(days=61),
    )
    assert tuple(p.mode for p in protocol.policies) == (
        CadenceMode.FIXED_60S,
        CadenceMode.FIXED_300S,
        CadenceMode.HYBRID_INTERRUPT,
        CadenceMode.EXPOSURE_EVENT,
    )


def test_common_grid_uses_only_latest_prediction_available_as_known():
    predictions = (
        TimedPrediction(T0, T0 + timedelta(seconds=10), Decimal("0.2"), "a"),
        TimedPrediction(
            T0 + timedelta(seconds=30),
            T0 + timedelta(minutes=1, seconds=20),
            Decimal("0.8"),
            "b",
        ),
    )
    grid = common_grid_latest(
        predictions,
        start=T0,
        end=T0 + timedelta(minutes=3),
    )
    assert grid[0].value is None
    assert grid[1].value == Decimal("0.2")
    assert grid[1].evidence_id == "a"
    assert grid[2].value == Decimal("0.8")
    assert grid[2].evidence_id == "b"


def test_max_idle_turns_stale_event_prediction_into_missing_not_zero():
    predictions = (
        TimedPrediction(T0, T0, Decimal("0.5"), "a"),
    )
    grid = common_grid_latest(
        predictions,
        start=T0,
        end=T0 + timedelta(minutes=7),
        max_age=timedelta(minutes=5),
    )
    assert grid[5].value == Decimal("0.5")
    assert grid[6].value is None
    assert grid[6].age_seconds == 360


def test_action_replay_charges_only_target_changes_and_preserves_chronology():
    predictions = (
        TimedPrediction(T0, T0, Decimal("0.5"), "a"),
        TimedPrediction(T0 + timedelta(seconds=10), T0 + timedelta(seconds=10), Decimal("0.5"), "b"),
        TimedPrediction(T0 + timedelta(seconds=20), T0 + timedelta(seconds=20), Decimal("-0.5"), "c"),
        TimedPrediction(T0 + timedelta(seconds=30), T0 + timedelta(seconds=30), Decimal(0), "d"),
    )
    actions = replay_actions(predictions, cost_rate=Decimal("0.001"))
    assert [a.evidence_id for a in actions] == ["a", "c", "d"]
    assert [a.turnover for a in actions] == [
        Decimal("0.5"),
        Decimal("1.0"),
        Decimal("0.5"),
    ]
    assert sum((a.cost for a in actions), start=Decimal(0)) == Decimal("0.0020")


def test_er5_without_new_prospective_rows_is_blocked_not_reusing_er4():
    protocol = default_er5_protocol(
        representation_id="R1",
        frozen_at=T0 + timedelta(days=30, hours=1),
        er4_holdout_end=T0 + timedelta(days=30),
        holdout_start=T0 + timedelta(days=31),
        holdout_end=T0 + timedelta(days=61),
    )
    status, reason = assess_stage2_readiness(
        protocol=protocol,
        representation_supported=True,
        new_holdout_rows=0,
        outcome_mature_rows=0,
        minimum_rows=50,
    )
    assert status is Stage2Status.BLOCKED
    assert reason == "NO_NEW_UNTOUCHED_HOLDOUT_ROWS"
