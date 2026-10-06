"""V23 qualification: trader-relative conviction (Q09)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ensemble.conviction import (
    BiasHistoryPoint,
    ConvictionStatus,
    DriftStatus,
    fit_relative_conviction_artifact,
    score_relative_conviction,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def point(i: int, bias: str, *, known_at: datetime | None = None) -> BiasHistoryPoint:
    return BiasHistoryPoint(
        expert_id="0xexpert",
        coin="BTC",
        raw_bias=Decimal(bias),
        known_at=known_at or (T0 - timedelta(days=30 - i)),
        evidence_id=f"e:{i}",
    )


def test_q09_low_support_is_unknown_not_neutral():
    artifact = fit_relative_conviction_artifact(
        expert_id="0xexpert",
        coin="BTC",
        history=[point(0, "0.2"), point(1, "0.4")],
        training_cutoff=T0,
        revision="conv:v1",
        min_support=5,
    )
    score = score_relative_conviction(artifact, raw_bias=Decimal("0.8"))

    assert score.status is ConvictionStatus.INSUFFICIENT_SUPPORT
    assert score.magnitude_percentile is None
    assert score.signed_percentile is None


def test_q09_empirical_percentile_is_trader_relative_and_signed():
    history = [point(i, str(i / 10)) for i in range(1, 11)]
    artifact = fit_relative_conviction_artifact(
        expert_id="0xexpert",
        coin="BTC",
        history=history,
        training_cutoff=T0,
        revision="conv:v1",
        min_support=5,
    )

    positive = score_relative_conviction(artifact, raw_bias=Decimal("0.75"))
    negative = score_relative_conviction(artifact, raw_bias=Decimal("-0.75"))

    assert positive.status is ConvictionStatus.VALID
    assert positive.magnitude_percentile == Decimal("0.7")
    assert positive.signed_percentile == Decimal("0.7")
    assert negative.signed_percentile == Decimal("-0.7")


def test_q09_current_and_future_samples_cannot_leak_into_reference_distribution():
    historical = [point(i, "0.5") for i in range(20)]
    at_cutoff = point(100, "100", known_at=T0)
    future = point(101, "1000", known_at=T0 + timedelta(seconds=1))

    base = fit_relative_conviction_artifact(
        expert_id="0xexpert",
        coin="BTC",
        history=historical,
        training_cutoff=T0,
        revision="conv:base",
        min_support=5,
    )
    contaminated_input = fit_relative_conviction_artifact(
        expert_id="0xexpert",
        coin="BTC",
        history=[*historical, at_cutoff, future],
        training_cutoff=T0,
        revision="conv:contaminated-input",
        min_support=5,
    )

    assert contaminated_input.absolute_bias_history == base.absolute_bias_history
    assert contaminated_input.support == base.support == 20


def test_q09_drift_diagnostic_flags_large_recent_scale_shift():
    history = [
        point(i, "0.5")
        for i in range(20)
    ] + [
        point(
            100 + i,
            "5.0",
            known_at=T0 - timedelta(minutes=10 - i),
        )
        for i in range(10)
    ]
    artifact = fit_relative_conviction_artifact(
        expert_id="0xexpert",
        coin="BTC",
        history=history,
        training_cutoff=T0,
        revision="conv:drift",
        min_support=20,
        drift_window=10,
        drift_ratio_threshold=Decimal(2),
    )

    assert artifact.drift_status is DriftStatus.SHIFTED
    assert artifact.recent_to_history_ratio is not None
    assert artifact.recent_to_history_ratio > Decimal(2)


def test_q09_missing_bias_remains_unavailable():
    artifact = fit_relative_conviction_artifact(
        expert_id="0xexpert",
        coin="BTC",
        history=[point(i, "0.5") for i in range(20)],
        training_cutoff=T0,
        revision="conv:v1",
        min_support=5,
    )

    score = score_relative_conviction(artifact, raw_bias=None)
    assert score.status is ConvictionStatus.BIAS_UNAVAILABLE
    assert score.magnitude_percentile is None
