"""S6 Qualification Suite: Q11 Correlation & Clone Resistance and Q15 Research Promotion."""

from decimal import Decimal

from ensemble.clustering import complete_link_clustering
from ensemble.consensus import (
    compute_equal_budget_consensus,
    compute_hierarchical_cluster_consensus,
)
from ensemble.evaluation import evaluate_baseline_vs_candidate
from ensemble.posture import compute_posture
from ensemble.similarity import compute_pairwise_distance


def test_q11_clone_resistance_neutralizes_replicated_experts():
    """Q11: Replicated identical experts do not gain independent cluster influence.

    Demonstrates that in hierarchical clustering B2, cloning an expert K times
    collapses the clones into one cluster whose aggregate influence stays bounded,
    whereas unadjusted baseline B1 would suffer severe clone inflation.
    """
    # 2 independent base experts:
    # Expert 1: Long (+1.0)
    # Expert 2: Short (-1.0)
    p_orig = compute_posture("0xexp1", "BTC", Decimal("1.0"), Decimal(100), Decimal(100))
    p_short = compute_posture("0xexp2", "BTC", Decimal("-1.0"), Decimal(100), Decimal(100))

    # Now clone Expert 1 nine more times (10 clones total of Long +1.0)
    clones = [
        compute_posture(f"0xexp1_clone_{i}", "BTC", Decimal("1.0"), Decimal(100), Decimal(100))
        for i in range(1, 10)
    ]
    all_postures = [p_orig] + clones + [p_short]

    # Baseline B1 (unadjusted equal weights: 11 experts total)
    # 10 Longs (+1.0) vs 1 Short (-1.0) -> B1 target is heavily inflated by clones:
    # C_B1 = (10 * 1.0 - 1.0) / 11 = 9 / 11 = +0.818
    b1_target = compute_equal_budget_consensus("BTC", all_postures)
    assert b1_target.observed_target > Decimal("0.80")  # Suffers clone inflation!

    # Candidate B2 (hierarchical complete-link clustering):
    # Clones of Expert 1 have 0 distance between them -> collapsed into Cluster 1.
    # Expert 2 (Short) is opposite -> Cluster 2.
    # Total clusters = 2.
    clone_cluster = ["0xexp1"] + [f"0xexp1_clone_{i}" for i in range(1, 10)]
    short_cluster = ["0xexp2"]
    clusters = [clone_cluster, short_cluster]

    b2_target = compute_hierarchical_cluster_consensus("BTC", all_postures, clusters)

    # CLONE RESISTANCE GUARANTEE:
    # Cluster 1 (10 clones) has budget 0.50 (each clone has weight 0.50 / 10 = 0.05).
    # Cluster 2 (1 short) has budget 0.50 (weight 0.50).
    # C_B2 = 0.50 * (+1.0) + 0.50 * (-1.0) = 0.0!
    # The 10 clones did NOT overpower the single independent short expert!
    assert b2_target.observed_target == Decimal("0.0"), (
        f"Expected neutral 0.0 under clone-resistant clustering, got {b2_target.observed_target}"
    )


def test_q11_avoid_chain_link_clustering_traps():
    """Q11: Dissimilar experts connected only through a third do not merge automatically.

    Chain-link trap:
      A is close to B (D=0.1 <= 0.2)
      B is close to C (D=0.1 <= 0.2)
      A is far from C (D=0.5 > 0.2)

    Complete-link clustering calculates D({A,B}, {C}) = max(0.5, 0.1) = 0.5 > 0.2,
    strictly refusing to merge them into a single cluster.
    """
    experts = ["A", "B", "C"]
    distance_matrix = {
        ("A", "B"): Decimal("0.10"),
        ("B", "C"): Decimal("0.10"),
        ("A", "C"): Decimal("0.50"),  # Dissimilar!
    }

    artifact = complete_link_clustering(experts, distance_matrix, threshold=Decimal("0.20"))

    # Must result in 2 clusters (e.g. ['A', 'B'] and ['C']), NEVER 1 single merged cluster ['A', 'B', 'C']
    assert artifact.cluster_count == 2
    assert ["A", "B", "C"] not in artifact.clusters
    assert ["C"] in artifact.clusters or ["A"] in artifact.clusters


def test_q11_flat_flat_and_low_support_excluded():
    """Q11: Flat-flat series cannot establish similarity; low support remains unknown."""
    # 1. All flat series
    flat_series = [Decimal("0.0")] * 10
    metric_flat = compute_pairwise_distance("0xa", "0xb", flat_series, flat_series, min_support=5)
    assert metric_flat.status == "INSUFFICIENT_OVERLAP"
    assert metric_flat.support_steps == 0
    assert metric_flat.distance == Decimal("1.0")

    # 2. Sparse overlap: only 3 active steps (< min_support 5)
    series_a = [Decimal("1.0"), Decimal("1.0"), Decimal("1.0")] + [Decimal("0.0")] * 7
    series_b = [Decimal("1.0"), Decimal("1.0"), Decimal("1.0")] + [Decimal("0.0")] * 7
    metric_sparse = compute_pairwise_distance("0xa", "0xb", series_a, series_b, min_support=5)
    assert metric_sparse.status == "INSUFFICIENT_OVERLAP"
    assert metric_sparse.support_steps == 3
    assert metric_sparse.distance == Decimal("1.0")

    # 3. Sufficient overlap (5 active steps with identical postures)
    series_active = [Decimal("1.0")] * 5 + [Decimal("0.0")] * 5
    metric_valid = compute_pairwise_distance(
        "0xa", "0xb", series_active, series_active, min_support=5
    )
    assert metric_valid.status == "VALID"
    assert metric_valid.support_steps == 5
    assert metric_valid.distance == Decimal("0.0")  # Exact match on active steps


def test_q15_research_promotion_gates_and_baseline_retention():
    """Q15: Promotion requires evidence; failed extra mechanism retains baseline."""
    # Scenario 1: Clone inflation detected and neutralized without turnover
    b1_inflated = compute_equal_budget_consensus(
        "BTC",
        [
            compute_posture("0xa", "BTC", Decimal(1), Decimal(100), Decimal(100)),
            compute_posture("0xa_clone", "BTC", Decimal(1), Decimal(100), Decimal(100)),
            compute_posture("0xb", "BTC", Decimal(-1), Decimal(100), Decimal(100)),
        ],
    )
    b2_neutralized = compute_hierarchical_cluster_consensus(
        "BTC",
        [
            compute_posture("0xa", "BTC", Decimal(1), Decimal(100), Decimal(100)),
            compute_posture("0xa_clone", "BTC", Decimal(1), Decimal(100), Decimal(100)),
            compute_posture("0xb", "BTC", Decimal(-1), Decimal(100), Decimal(100)),
        ],
        clusters=[["0xa", "0xa_clone"], ["0xb"]],
    )

    eval_promote = evaluate_baseline_vs_candidate(
        b1_inflated,
        b2_neutralized,
        clone_detected=True,
        turnover_penalty=Decimal("0.01"),
    )
    assert eval_promote.recommendation == "ACCEPT_CANDIDATE"

    # Scenario 2: No clones detected -> retain simpler baseline B1
    eval_retain = evaluate_baseline_vs_candidate(
        b1_inflated,
        b1_inflated,
        clone_detected=False,
    )
    assert eval_retain.recommendation == "RETAIN_BASELINE"

    # Scenario 3: High turnover churn penalty exceeds benefit -> retain baseline B1
    eval_turnover = evaluate_baseline_vs_candidate(
        b1_inflated,
        b2_neutralized,
        clone_detected=True,
        turnover_penalty=Decimal("0.10"),  # High turnover penalty > 0.05
    )
    assert eval_turnover.recommendation == "RETAIN_BASELINE"
