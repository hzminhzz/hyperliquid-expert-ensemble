"""V29 qualification: deterministic TradeSignal state machine (Q19-Q20/Q24-Q25)."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ensemble.signal_policy import (
    ConfidenceComponents,
    PredictiveEvidence,
    SignalEvent,
    SignalPolicy,
    SignalState,
    apply_signal_policy,
)

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def evidence(strength: str, *, features: tuple[str, ...] = ("b4_relative",)):
    return PredictiveEvidence(
        evidence_id=f"pred:{strength}",
        coin="BTC",
        horizon="1h",
        signal_strength=Decimal(strength),
        confidence=ConfidenceComponents(
            reliability_mass=Decimal("0.9"),
            independent_breadth=Decimal(3),
            sample_support=100,
            calibration_support=None,
        ),
        promoted_features=features,
        baseline_comparators=(("B0", Decimal("0.1")), ("B2", Decimal("0.2"))),
        supporting_clusters=("cluster:A",),
        opposing_clusters=("cluster:B",),
        missing_information=(),
        invalidation_conditions=("coverage<0.5",),
        evidence_refs=("ens:1",),
        feature_revision="features:v29",
        model_revision="model:v29",
        universe_revision="universe:1",
        as_of=T0,
        knowledge_time=T0,
        crowding_risk=Decimal("0.4"),
    )


def enabled_policy() -> SignalPolicy:
    return SignalPolicy(
        policy_revision="policy:v29",
        model_revision="model:v29",
        promoted_features=("b4_relative",),
        entry_threshold=Decimal("0.5"),
        exit_threshold=Decimal("0.25"),
        change_threshold=Decimal("0.1"),
        enabled=True,
    )


def test_q19_current_unpromoted_policy_is_disabled_not_fabricated():
    policy = replace(enabled_policy(), promoted_features=(), enabled=False)
    signal = apply_signal_policy(evidence("0.9"), policy)

    assert signal.state is SignalState.FLAT
    assert signal.event is SignalEvent.NONE
    assert signal.blocker_code == "NO_PROMOTED_PREDICTIVE_EVIDENCE"
    assert signal.signal_strength == Decimal("0.9")


def test_q19_enter_and_persistence_emit_deterministic_events():
    policy = enabled_policy()
    first = apply_signal_policy(evidence("0.6"), policy)
    second = apply_signal_policy(evidence("0.6"), policy, prior_signal=first)

    assert first.state is SignalState.LONG
    assert first.event is SignalEvent.ENTER
    assert second.state is SignalState.LONG
    assert second.event is SignalEvent.NONE


def test_q19_increase_and_reduce_keep_direction():
    policy = enabled_policy()
    entered = apply_signal_policy(evidence("0.6"), policy)
    increased = apply_signal_policy(evidence("0.8"), policy, prior_signal=entered)
    reduced = apply_signal_policy(evidence("0.6"), policy, prior_signal=increased)

    assert increased.state is SignalState.LONG
    assert increased.event is SignalEvent.INCREASE
    assert reduced.state is SignalState.LONG
    assert reduced.event is SignalEvent.REDUCE


def test_q19_exit_and_reverse_require_declared_threshold_crossings():
    policy = enabled_policy()
    entered = apply_signal_policy(evidence("0.7"), policy)

    exit_signal = apply_signal_policy(evidence("0.2"), policy, prior_signal=entered)
    assert exit_signal.state is SignalState.FLAT
    assert exit_signal.event is SignalEvent.EXIT

    reverse_signal = apply_signal_policy(evidence("-0.7"), policy, prior_signal=entered)
    assert reverse_signal.state is SignalState.SHORT
    assert reverse_signal.event is SignalEvent.REVERSE


def test_q20_expected_return_requires_calibration_and_crowding_stays_separate():
    policy = enabled_policy()
    uncalibrated = replace(evidence("0.7"), expected_return=Decimal("0.01"))
    with pytest.raises(ValueError, match="calibration"):
        apply_signal_policy(uncalibrated, policy)

    calibrated = replace(
        uncalibrated,
        calibration_revision="cal:v1",
    )
    signal = apply_signal_policy(calibrated, policy)
    assert signal.expected_return == Decimal("0.01")
    assert signal.crowding_risk == Decimal("0.4")
    assert signal.confidence.reliability_mass == Decimal("0.9")


def test_q24_missing_promoted_feature_blocks_signal_without_guessing():
    signal = apply_signal_policy(
        evidence("0.9", features=("other",)),
        enabled_policy(),
    )

    assert signal.state is SignalState.FLAT
    assert signal.event is SignalEvent.NONE
    assert signal.blocker_code == "PREDICTIVE_EVIDENCE_INCOMPLETE"
