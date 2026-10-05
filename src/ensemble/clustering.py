"""Deterministic complete-link clustering (Contract R4, Q11).

Groups redundant experts using maximum pairwise distance to avoid chain-linking traps.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(slots=True, frozen=True)
class ClusterArtifact:
    version: str
    threshold: Decimal
    clusters: list[list[str]]
    cluster_count: int


def complete_link_clustering(
    experts: list[str],
    distance_matrix: dict[tuple[str, str], Decimal],
    threshold: Decimal = Decimal("0.2"),
    version: str = "cluster:v1",
) -> ClusterArtifact:
    """Perform deterministic complete-link agglomerative clustering (R4).

    Distance between clusters C1 and C2 is the MAXIMUM pairwise distance:
      D(C1, C2) = max { D(u, v) : u in C1, v in C2 }

    Prevents chain-link traps:
      If A ~ B and B ~ C, but A !~ C (D(A,C) > threshold), A and C are NOT merged.
    """
    sorted_experts = sorted(set(experts))
    # Each expert starts in its own cluster
    clusters: list[list[str]] = [[e] for e in sorted_experts]

    def get_dist(e1: str, e2: str) -> Decimal:
        if e1 == e2:
            return Decimal("0.0")
        key = (e1, e2) if (e1, e2) in distance_matrix else (e2, e1)
        return distance_matrix.get(key, Decimal("1.0"))

    def cluster_distance(c1: list[str], c2: list[str]) -> Decimal:
        # Complete link: MAX pairwise distance
        return max(get_dist(u, v) for u in c1 for v in c2)

    while True:
        best_dist = Decimal("1.0")
        best_pair: tuple[int, int] | None = None

        # Deterministic scan of all cluster pairs
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                d = cluster_distance(clusters[i], clusters[j])
                if d <= threshold and (best_pair is None or d < best_dist):
                    best_dist = d
                    best_pair = (i, j)

        if best_pair is None:
            break

        i, j = best_pair
        merged = sorted(clusters[i] + clusters[j])
        # Remove j then i
        clusters.pop(j)
        clusters.pop(i)
        clusters.append(merged)
        clusters.sort(key=lambda c: c[0])

    return ClusterArtifact(
        version=version,
        threshold=threshold,
        clusters=clusters,
        cluster_count=len(clusters),
    )
