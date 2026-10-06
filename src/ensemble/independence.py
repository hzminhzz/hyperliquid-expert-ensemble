"""Point-in-time independence artifacts for V2 ensemble evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .clustering import complete_link_clustering


@dataclass(slots=True, frozen=True)
class PairwiseIndependenceMetric:
    expert_a: str
    expert_b: str
    distance: Decimal
    active_steps: int
    joint_episodes: int
    status: str
    as_of: datetime


@dataclass(slots=True, frozen=True)
class IndependenceCluster:
    cluster_id: str
    members: tuple[str, ...]
    budget: Decimal
    status: str  # INFERRED or UNKNOWN_SIMILARITY


@dataclass(slots=True, frozen=True)
class IndependenceArtifact:
    revision: str
    feature_schema_version: str
    training_cutoff: datetime
    effective_from: datetime
    update_schedule: str
    threshold: Decimal
    clusters: tuple[IndependenceCluster, ...]
    unknown_experts: tuple[str, ...]
    effective_breadth: Decimal
    pair_metrics: tuple[PairwiseIndependenceMetric, ...]


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _cluster_id(members: Sequence[str], prefix: str = "cluster") -> str:
    return f"{prefix}:" + ",".join(sorted(members))


def build_independence_artifact(
    *,
    experts: Sequence[str],
    pair_metrics: Sequence[PairwiseIndependenceMetric],
    revision: str,
    feature_schema_version: str,
    training_cutoff: datetime,
    effective_from: datetime,
    threshold: Decimal = Decimal("0.20"),
    min_active_steps: int = 5,
    min_joint_episodes: int = 1,
    update_schedule: str = "weekly",
) -> IndependenceArtifact:
    """Build one immutable, conservative point-in-time independence artifact."""
    if effective_from < training_cutoff:
        raise ValueError("effective_from cannot precede training_cutoff")

    unique_experts = tuple(sorted(set(experts)))
    metrics_by_pair: dict[tuple[str, str], PairwiseIndependenceMetric] = {}
    for metric in pair_metrics:
        if metric.as_of > training_cutoff:
            raise ValueError("pair metric uses information after training_cutoff")
        metrics_by_pair[_pair_key(metric.expert_a, metric.expert_b)] = metric

    valid_metrics = {
        key: metric
        for key, metric in metrics_by_pair.items()
        if metric.status == "VALID"
        and metric.active_steps >= min_active_steps
        and metric.joint_episodes >= min_joint_episodes
    }

    known_experts: list[str] = []
    unknown_experts: list[str] = []
    for expert in unique_experts:
        peers = [peer for peer in unique_experts if peer != expert]
        if not peers:
            known_experts.append(expert)
            continue
        has_supported_pair = any(_pair_key(expert, peer) in valid_metrics for peer in peers)
        if has_supported_pair:
            known_experts.append(expert)
        else:
            unknown_experts.append(expert)

    distance_matrix = {
        key: metric.distance
        for key, metric in valid_metrics.items()
        if metric.expert_a in known_experts and metric.expert_b in known_experts
    }
    inferred = complete_link_clustering(
        known_experts,
        distance_matrix,
        threshold=threshold,
        version=revision,
    ).clusters

    cluster_specs: list[tuple[tuple[str, ...], str]] = [
        (tuple(sorted(members)), "INFERRED") for members in inferred
    ]
    if unknown_experts:
        cluster_specs.append((tuple(sorted(unknown_experts)), "UNKNOWN_SIMILARITY"))

    if not cluster_specs:
        effective_breadth = Decimal(0)
        clusters: tuple[IndependenceCluster, ...] = ()
    else:
        budget = Decimal(1) / Decimal(len(cluster_specs))
        clusters = tuple(
            IndependenceCluster(
                cluster_id=_cluster_id(
                    members,
                    prefix="newcomer" if status == "UNKNOWN_SIMILARITY" else "cluster",
                ),
                members=members,
                budget=budget,
                status=status,
            )
            for members, status in sorted(cluster_specs, key=lambda item: item[0])
        )
        effective_breadth = Decimal(1) / sum(
            (cluster.budget * cluster.budget for cluster in clusters),
            start=Decimal(0),
        )

    return IndependenceArtifact(
        revision=revision,
        feature_schema_version=feature_schema_version,
        training_cutoff=training_cutoff,
        effective_from=effective_from,
        update_schedule=update_schedule,
        threshold=threshold,
        clusters=clusters,
        unknown_experts=tuple(sorted(unknown_experts)),
        effective_breadth=effective_breadth,
        pair_metrics=tuple(
            sorted(
                pair_metrics,
                key=lambda metric: (metric.expert_a, metric.expert_b, metric.as_of),
            )
        ),
    )
