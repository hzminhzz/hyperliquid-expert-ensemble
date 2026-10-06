"""V22 qualification: point-in-time independence artifacts (Q08)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ensemble.independence import PairwiseIndependenceMetric, build_independence_artifact

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def metric(
    a: str,
    b: str,
    distance: str,
    *,
    steps: int = 20,
    episodes: int = 3,
    status: str = "VALID",
    as_of: datetime = T0,
) -> PairwiseIndependenceMetric:
    return PairwiseIndependenceMetric(
        expert_a=a,
        expert_b=b,
        distance=Decimal(distance),
        active_steps=steps,
        joint_episodes=episodes,
        status=status,
        as_of=as_of,
    )


def test_q08_fifteen_clones_share_one_independent_budget():
    clones = [f"clone-{i:02d}" for i in range(15)]
    pairs = [
        metric(a, b, "0.01")
        for i, a in enumerate(clones)
        for b in clones[i + 1 :]
    ]

    artifact = build_independence_artifact(
        experts=clones,
        pair_metrics=pairs,
        revision="ind:v2:clone",
        feature_schema_version="intent-state:v1",
        training_cutoff=T0,
        effective_from=T0 + timedelta(days=1),
    )

    assert len(artifact.clusters) == 1
    assert artifact.clusters[0].members == tuple(clones)
    assert artifact.clusters[0].budget == Decimal(1)
    assert artifact.effective_breadth == Decimal(1)


def test_q08_complete_link_avoids_chain_linking():
    artifact = build_independence_artifact(
        experts=["A", "B", "C"],
        pair_metrics=[
            metric("A", "B", "0.10"),
            metric("B", "C", "0.10"),
            metric("A", "C", "0.50"),
        ],
        revision="ind:v2:chain",
        feature_schema_version="intent-state:v1",
        training_cutoff=T0,
        effective_from=T0 + timedelta(days=1),
        threshold=Decimal("0.20"),
    )

    memberships = [set(cluster.members) for cluster in artifact.clusters]
    assert {"A", "B", "C"} not in memberships
    assert len(memberships) == 2


def test_q08_low_support_newcomers_share_conservative_unknown_pool():
    artifact = build_independence_artifact(
        experts=["A", "new-1", "new-2"],
        pair_metrics=[
            metric("A", "new-1", "0.05", steps=2, episodes=1),
            metric("A", "new-2", "0.05", status="INSUFFICIENT_OVERLAP"),
        ],
        revision="ind:v2:new",
        feature_schema_version="intent-state:v1",
        training_cutoff=T0,
        effective_from=T0 + timedelta(days=1),
    )

    assert artifact.unknown_experts == ("A", "new-1", "new-2")
    assert len(artifact.clusters) == 1
    assert artifact.clusters[0].status == "UNKNOWN_SIMILARITY"
    assert artifact.effective_breadth == Decimal(1)


def test_q08_supported_pair_does_not_make_unobserved_newcomer_independent():
    artifact = build_independence_artifact(
        experts=["A", "B", "new"],
        pair_metrics=[metric("A", "B", "0.40")],
        revision="ind:v2:mixed",
        feature_schema_version="intent-state:v1",
        training_cutoff=T0,
        effective_from=T0 + timedelta(days=1),
    )

    inferred = next(cluster for cluster in artifact.clusters if cluster.status == "INFERRED")
    newcomer = next(
        cluster for cluster in artifact.clusters if cluster.status == "UNKNOWN_SIMILARITY"
    )
    assert set(inferred.members) in ({"A"}, {"B"})
    assert newcomer.members == ("new",)
    assert artifact.effective_breadth == Decimal(3)


def test_q08_future_similarity_cannot_rewrite_as_known_artifact():
    future_metric = metric(
        "A",
        "B",
        "0.01",
        as_of=T0 + timedelta(days=2),
    )
    with pytest.raises(ValueError, match="after training_cutoff"):
        build_independence_artifact(
            experts=["A", "B"],
            pair_metrics=[future_metric],
            revision="ind:v2:future",
            feature_schema_version="intent-state:v1",
            training_cutoff=T0,
            effective_from=T0 + timedelta(days=1),
        )


def test_q08_strategy_drift_creates_new_revision_not_mutation():
    before = build_independence_artifact(
        experts=["A", "B"],
        pair_metrics=[metric("A", "B", "0.05")],
        revision="ind:v2:before",
        feature_schema_version="intent-state:v1",
        training_cutoff=T0,
        effective_from=T0 + timedelta(days=1),
    )
    later_cut = T0 + timedelta(days=30)
    after = build_independence_artifact(
        experts=["A", "B"],
        pair_metrics=[
            metric(
                "A",
                "B",
                "0.80",
                as_of=later_cut,
            )
        ],
        revision="ind:v2:after",
        feature_schema_version="intent-state:v1",
        training_cutoff=later_cut,
        effective_from=later_cut + timedelta(days=1),
    )

    assert len(before.clusters) == 1
    assert len(after.clusters) == 2
    assert before.revision == "ind:v2:before"
    assert before.clusters[0].members == ("A", "B")
