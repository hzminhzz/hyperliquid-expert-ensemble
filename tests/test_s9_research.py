"""S9 Research Suite: B3 Quality Weighting, B4 Regime Conditioning, and Q15 Promotion Gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ensemble.consensus import (
    CausalChangeCategory,
    ConsensusTarget,
)
from ensemble.evaluation import (
    QualityMetric,
    RegimeType,
    compute_b3_quality_consensus,
    compute_b4_regime_consensus,
    compute_lagged_quality_weights,
    detect_lagged_regime,
    evaluate_research_candidate,
)
from ensemble.posture import compute_posture


def test_b3_lagged_quality_weighting_and_min_support_neutrality():
    """B3: Higher profit factor earns higher weight; insufficient support receives neutral default."""
    experts = ["0xskilled", "0xmediocre", "0xnewcomer"]
    now = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)

    metrics = {
        "0xskilled": QualityMetric(
            expert_id="0xskilled",
            profit_factor=Decimal("2.5"),
            win_rate=Decimal("0.65"),
            trade_count=20,
            as_of=now - timedelta(hours=1),
        ),
        "0xmediocre": QualityMetric(
            expert_id="0xmediocre",
            profit_factor=Decimal("0.5"),
            win_rate=Decimal("0.40"),
            trade_count=15,
            as_of=now - timedelta(hours=1),
        ),
        # Newcomer has only 2 trades (< min_support 5)
        "0xnewcomer": QualityMetric(
            expert_id="0xnewcomer",
            profit_factor=Decimal("10.0"),  # High but unproven
            win_rate=Decimal("1.0"),
            trade_count=2,
            as_of=now - timedelta(hours=1),
        ),
    }

    weights = compute_lagged_quality_weights(experts, metrics, min_support=5)

    # Skilled gets weight corresponding to 2.5
    # Mediocre gets weight corresponding to 0.5
    # Newcomer gets neutral default score 1.0 (not inflated 10.0!)
    # Total score = 2.5 + 0.5 + 1.0 = 4.0
    assert weights["0xskilled"] == Decimal("2.5") / Decimal("4.0")
    assert weights["0xmediocre"] == Decimal("0.5") / Decimal("4.0")
    assert weights["0xnewcomer"] == Decimal("1.0") / Decimal("4.0")
    assert sum(weights.values()) == Decimal(1)


def test_b3_quality_weighted_intra_cluster_consensus():
    """B3: Cluster budget is preserved, while skilled expert drives intra-cluster posture."""
    # Cluster 1: 0xskilled (Long 1.0) and 0xmediocre (Short -1.0)
    # Cluster 2: 0xindependent (Long 1.0)
    p_skilled = compute_posture("0xskilled", "BTC", Decimal(1), Decimal(60000), Decimal(60000))
    p_mediocre = compute_posture("0xmediocre", "BTC", Decimal(-1), Decimal(60000), Decimal(60000))
    p_indep = compute_posture("0xindep", "BTC", Decimal(1), Decimal(60000), Decimal(60000))

    clusters = [["0xskilled", "0xmediocre"], ["0xindep"]]
    postures = [p_skilled, p_mediocre, p_indep]

    # Without quality weights (equal intra-cluster share 0.5/0.5), Cluster 1 net is 0.0
    # With quality weights (skilled 0.80, mediocre 0.20), Cluster 1 net is 0.50 * (0.80 - 0.20) = +0.30
    quality_weights = {
        "0xskilled": Decimal("0.80"),
        "0xmediocre": Decimal("0.20"),
        "0xindep": Decimal("1.0"),
    }

    b3_target = compute_b3_quality_consensus("BTC", postures, clusters, quality_weights)
    assert b3_target.is_actionable
    # Cluster 1 (+0.30) + Cluster 2 (+0.50) = +0.80
    assert b3_target.observed_target == Decimal("0.80")


def test_b4_regime_detection_and_volatility_dampening():
    """B4: High volatility regime dampens target posture to protect against tail stress."""
    # Normal calm price series (< 3% returns)
    calm_prices = [Decimal(100), Decimal(101), Decimal(100), Decimal(102), Decimal(101)]
    regime_calm = detect_lagged_regime(calm_prices, high_vol_threshold_pct=Decimal("0.03"))
    assert regime_calm == RegimeType.LOW_VOLATILITY

    base_consensus = ConsensusTarget(
        coin="BTC",
        observed_target=Decimal("1.0"),
        missing_mass=Decimal(0),
        lower_bound=Decimal("1.0"),
        upper_bound=Decimal("1.0"),
        cause=CausalChangeCategory.ECONOMIC_CHANGE,
        contributions=[],
        is_actionable=True,
        blocker_code=None,
        as_of=datetime.now(UTC),
    )

    # In calm regime, target is unchanged
    b4_calm = compute_b4_regime_consensus(base_consensus, regime_calm)
    assert b4_calm.observed_target == Decimal("1.0")

    # Turbulent price series (> 3% return swings)
    turbulent_prices = [Decimal(100), Decimal(105), Decimal(98), Decimal(107), Decimal(95)]
    regime_high_vol = detect_lagged_regime(turbulent_prices, high_vol_threshold_pct=Decimal("0.03"))
    assert regime_high_vol == RegimeType.HIGH_VOLATILITY

    # In high volatility regime, target posture is dampened by 0.70x
    b4_turbulent = compute_b4_regime_consensus(base_consensus, regime_high_vol)
    assert b4_turbulent.observed_target == Decimal("0.70")
    assert b4_turbulent.cause == CausalChangeCategory.VALUATION_CHANGE


def test_q15_candidate_promotion_and_retained_negative_results():
    """Q15: Promotion requires cleared bar + explicit approval; failed candidate retains negative ablation."""
    # 1. Successful candidate clearing bar AND having approver grant
    record_promoted = evaluate_research_candidate(
        candidate_id="B3-quality-weights-v1",
        train_window="2026-01-01..2026-06-30",
        eval_window="2026-07-01..2026-09-30",
        forward_sharpe_delta=Decimal("0.25"),  # >= 0.15 required
        turnover_delta=Decimal("0.04"),        # <= 0.10 max
        search_count=3,
        policy_grant="GRANT-POLICY-APPROVER-001",
    )
    assert record_promoted.evidence_bar_cleared is True
    assert record_promoted.status == "PROMOTED"

    # 2. Candidate clearing bar BUT lacking approver grant cannot self-promote
    record_awaiting = evaluate_research_candidate(
        candidate_id="B3-quality-weights-v1",
        train_window="2026-01-01..2026-06-30",
        eval_window="2026-07-01..2026-09-30",
        forward_sharpe_delta=Decimal("0.25"),
        turnover_delta=Decimal("0.04"),
        search_count=3,
        policy_grant=None,
    )
    assert record_awaiting.evidence_bar_cleared is True
    assert record_awaiting.status == "AWAITING_APPROVAL"

    # 3. Failed candidate with insufficient forward Sharpe improvement retains negative result
    record_failed_sharpe = evaluate_research_candidate(
        candidate_id="B4-regime-conditioning-v1",
        train_window="2026-01-01..2026-06-30",
        eval_window="2026-07-01..2026-09-30",
        forward_sharpe_delta=Decimal("0.05"),  # Failed bar < 0.15
        turnover_delta=Decimal("0.02"),
        search_count=12,  # Multiple comparisons recorded
        policy_grant=None,
    )
    assert record_failed_sharpe.evidence_bar_cleared is False
    assert record_failed_sharpe.status == "RETAIN_BASELINE"
    # Ablation is explicitly retained in record
    assert record_failed_sharpe.retained_ablation["search_count"] == 12
    assert record_failed_sharpe.retained_ablation["cleared"] is False

    # 4. Failed candidate with excessive turnover penalty retains negative result
    record_failed_turnover = evaluate_research_candidate(
        candidate_id="B3-hyperactive-weights",
        train_window="2026-01-01..2026-06-30",
        eval_window="2026-07-01..2026-09-30",
        forward_sharpe_delta=Decimal("0.30"),
        turnover_delta=Decimal("0.35"),  # Excessive turnover > 0.10
        search_count=5,
        policy_grant=None,
    )
    assert record_failed_turnover.evidence_bar_cleared is False
    assert record_failed_turnover.status == "RETAIN_BASELINE"
    assert record_failed_turnover.retained_ablation["turnover_delta"] == "0.35"
