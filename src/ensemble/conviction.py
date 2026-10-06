"""Lagged trader-relative conviction artifacts for V2."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class ConvictionStatus(str, Enum):
    VALID = "VALID"
    INSUFFICIENT_SUPPORT = "INSUFFICIENT_SUPPORT"
    BIAS_UNAVAILABLE = "BIAS_UNAVAILABLE"


class DriftStatus(str, Enum):
    STABLE = "STABLE"
    SHIFTED = "SHIFTED"
    UNASSESSED = "UNASSESSED"


@dataclass(slots=True, frozen=True)
class BiasHistoryPoint:
    expert_id: str
    coin: str
    raw_bias: Decimal
    known_at: datetime
    evidence_id: str


@dataclass(slots=True, frozen=True)
class RelativeConvictionArtifact:
    revision: str
    expert_id: str
    coin: str
    training_cutoff: datetime
    absolute_bias_history: tuple[Decimal, ...]
    support: int
    min_support: int
    historical_median_abs_bias: Decimal | None
    recent_median_abs_bias: Decimal | None
    recent_to_history_ratio: Decimal | None
    drift_status: DriftStatus


@dataclass(slots=True, frozen=True)
class RelativeConviction:
    expert_id: str
    coin: str
    raw_bias: Decimal | None
    magnitude_percentile: Decimal | None
    signed_percentile: Decimal | None
    status: ConvictionStatus
    support: int
    artifact_revision: str


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    midpoint = n // 2
    if n % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal(2)


def fit_relative_conviction_artifact(
    *,
    expert_id: str,
    coin: str,
    history: Sequence[BiasHistoryPoint],
    training_cutoff: datetime,
    revision: str,
    min_support: int = 20,
    drift_window: int = 10,
    drift_ratio_threshold: Decimal = Decimal("2.0"),
) -> RelativeConvictionArtifact:
    """Fit an immutable empirical reference distribution using only lagged data."""
    eligible = sorted(
        (
            point
            for point in history
            if point.expert_id == expert_id
            and point.coin == coin
            and point.known_at < training_cutoff
        ),
        key=lambda point: (point.known_at, point.evidence_id),
    )
    absolute_history = tuple(abs(point.raw_bias) for point in eligible)
    support = len(absolute_history)

    historical_median = _median(absolute_history)
    recent_values = absolute_history[-drift_window:] if drift_window > 0 else ()
    recent_median = _median(recent_values)

    ratio: Decimal | None = None
    drift_status = DriftStatus.UNASSESSED
    if (
        support >= min_support
        and historical_median is not None
        and historical_median > 0
        and recent_median is not None
    ):
        ratio = recent_median / historical_median
        lower = Decimal(1) / drift_ratio_threshold
        drift_status = (
            DriftStatus.SHIFTED
            if ratio > drift_ratio_threshold or ratio < lower
            else DriftStatus.STABLE
        )

    return RelativeConvictionArtifact(
        revision=revision,
        expert_id=expert_id,
        coin=coin,
        training_cutoff=training_cutoff,
        absolute_bias_history=absolute_history,
        support=support,
        min_support=min_support,
        historical_median_abs_bias=historical_median,
        recent_median_abs_bias=recent_median,
        recent_to_history_ratio=ratio,
        drift_status=drift_status,
    )


def score_relative_conviction(
    artifact: RelativeConvictionArtifact,
    *,
    raw_bias: Decimal | None,
) -> RelativeConviction:
    """Score current bias against a frozen lagged empirical distribution."""
    if raw_bias is None:
        return RelativeConviction(
            expert_id=artifact.expert_id,
            coin=artifact.coin,
            raw_bias=None,
            magnitude_percentile=None,
            signed_percentile=None,
            status=ConvictionStatus.BIAS_UNAVAILABLE,
            support=artifact.support,
            artifact_revision=artifact.revision,
        )
    if artifact.support < artifact.min_support:
        return RelativeConviction(
            expert_id=artifact.expert_id,
            coin=artifact.coin,
            raw_bias=raw_bias,
            magnitude_percentile=None,
            signed_percentile=None,
            status=ConvictionStatus.INSUFFICIENT_SUPPORT,
            support=artifact.support,
            artifact_revision=artifact.revision,
        )

    magnitude = abs(raw_bias)
    less = sum(value < magnitude for value in artifact.absolute_bias_history)
    equal = sum(value == magnitude for value in artifact.absolute_bias_history)
    percentile = (
        Decimal(less) + (Decimal(equal) / Decimal(2))
    ) / Decimal(artifact.support)

    signed = percentile if raw_bias >= 0 else -percentile
    return RelativeConviction(
        expert_id=artifact.expert_id,
        coin=artifact.coin,
        raw_bias=raw_bias,
        magnitude_percentile=percentile,
        signed_percentile=signed,
        status=ConvictionStatus.VALID,
        support=artifact.support,
        artifact_revision=artifact.revision,
    )
