"""Pairwise posture similarity and redundancy estimation (Contract R4, Q11).

Measures redundancy on aligned posture feature grids while excluding flat-flat agreements.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(slots=True, frozen=True)
class SimilarityMetric:
    expert_a: str
    expert_b: str
    distance: Decimal  # 0.0 (identical) to 1.0 (maximally dissimilar / opposite)
    support_steps: int
    status: str  # "VALID", "INSUFFICIENT_OVERLAP"


def compute_pairwise_distance(
    expert_a: str,
    expert_b: str,
    series_a: list[Decimal],
    series_b: list[Decimal],
    min_support: int = 5,
) -> SimilarityMetric:
    """Compute pairwise posture distance excluding flat-flat agreement (R4).

    Rules:
      1. Flat-flat points (where both experts have 0 posture) do NOT establish similarity.
      2. If active support < min_support, returns distance 1.0 with status INSUFFICIENT_OVERLAP.
      3. For active steps, distance is normalized mean absolute posture difference:
         D(a, b) = mean(|s_a - s_b|) / 2.0
    """
    if len(series_a) != len(series_b):
        raise ValueError("Series lengths must match for aligned similarity computation")

    active_diffs: list[Decimal] = []
    zero = Decimal("0.0")

    for sa, sb in zip(series_a, series_b, strict=True):
        # Exclude flat-flat agreement: both are flat
        if sa == zero and sb == zero:
            continue
        active_diffs.append(abs(sa - sb))

    support = len(active_diffs)
    if support < min_support:
        return SimilarityMetric(
            expert_a=expert_a,
            expert_b=expert_b,
            distance=Decimal("1.0"),
            support_steps=support,
            status="INSUFFICIENT_OVERLAP",
        )

    # Average difference across active support
    avg_diff = sum(active_diffs) / Decimal(str(support))
    normalized_distance = min(Decimal("1.0"), max(Decimal("0.0"), avg_diff / Decimal("2.0")))

    return SimilarityMetric(
        expert_a=expert_a,
        expert_b=expert_b,
        distance=normalized_distance,
        support_steps=support,
        status="VALID",
    )
