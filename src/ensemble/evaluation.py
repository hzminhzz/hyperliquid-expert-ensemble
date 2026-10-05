"""Baseline versus candidate evaluation and research promotion (Contracts R7, Q15).

Compares B1 equal baseline against B2 cluster candidate under strict evidence gates.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from .consensus import CausalChangeCategory, ConsensusTarget, ExpertContribution
from .posture import ExpertPosture


@dataclass(slots=True, frozen=True)
class CandidateEvaluation:
    coin: str
    baseline_target: ConsensusTarget
    candidate_target: ConsensusTarget
    target_difference: Decimal
    clone_adjusted: bool
    recommendation: str  # "ACCEPT_CANDIDATE", "RETAIN_BASELINE", "INCONCLUSIVE"
    evidence_rationale: str
    metrics: dict[str, Any]


def evaluate_baseline_vs_candidate(
    b1_target: ConsensusTarget,
    b2_target: ConsensusTarget,
    clone_detected: bool = False,
    turnover_penalty: Decimal = Decimal("0.0"),
) -> CandidateEvaluation:
    """Evaluate baseline B1 vs candidate B2 under research promotion gates (R7, Q15).

    Rule:
      If candidate demonstrably neutralizes clone inflation without excessive turnover,
      recommend promotion. Otherwise, retain baseline B1. A failed extra mechanism
      never displaces the baseline.
    """
    diff = abs(b2_target.observed_target - b1_target.observed_target)

    # Inconclusive if both targets are non-actionable or have excessive missing mass
    if not b1_target.is_actionable and not b2_target.is_actionable:
        return CandidateEvaluation(
            coin=b1_target.coin,
            baseline_target=b1_target,
            candidate_target=b2_target,
            target_difference=diff,
            clone_adjusted=clone_detected,
            recommendation="INCONCLUSIVE",
            evidence_rationale="Both baseline and candidate are currently non-actionable due to coverage loss",
            metrics={"diff": str(diff), "turnover_penalty": str(turnover_penalty)},
        )

    # If clones were detected and neutralized:
    if clone_detected:
        if turnover_penalty < Decimal("0.05"):
            recommendation = "ACCEPT_CANDIDATE"
            rationale = (
                "Candidate B2 neutralized clone inflation while maintaining acceptable turnover"
            )
        else:
            recommendation = "RETAIN_BASELINE"
            rationale = "Turnover penalty exceeds benefit of cluster grouping; retain baseline B1"
    else:
        # No clones detected and diff is small: retain simpler baseline B1 (Occam's razor / R7)
        recommendation = "RETAIN_BASELINE"
        rationale = "No redundant clusters detected; simpler baseline B1 retained"

    return CandidateEvaluation(
        coin=b1_target.coin,
        baseline_target=b1_target,
        candidate_target=b2_target,
        target_difference=diff,
        clone_adjusted=clone_detected,
        recommendation=recommendation,
        evidence_rationale=rationale,
        metrics={
            "diff": str(diff),
            "b1_target": str(b1_target.observed_target),
            "b2_target": str(b2_target.observed_target),
            "turnover_penalty": str(turnover_penalty),
        },
    )
class RegimeType(str, Enum):
    LOW_VOLATILITY = "LOW_VOLATILITY"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    TRENDING = "TRENDING"


@dataclass(slots=True, frozen=True)
class QualityMetric:
    expert_id: str
    profit_factor: Decimal
    win_rate: Decimal
    trade_count: int
    as_of: datetime


def compute_lagged_quality_weights(
    experts: list[str],
    metrics: dict[str, QualityMetric],
    min_support: int = 5,
) -> dict[str, Decimal]:
    """Compute normalized quality weights using lagged performance (R7, Q15).

    Train/eval separation invariant: Metrics must be computed strictly prior
    to the evaluation window. Experts lacking min_support receive neutral weights.
    """
    if not experts:
        return {}

    raw_weights: dict[str, Decimal] = {}
    for exp in experts:
        m = metrics.get(exp)
        if m is not None and m.trade_count >= min_support:
            score = max(Decimal("0.1"), m.profit_factor)
        else:
            score = Decimal("1.0")
        raw_weights[exp] = score

    total = sum(raw_weights.values())
    if total <= Decimal(0):
        equal = Decimal(1) / Decimal(len(experts))
        return {exp: equal for exp in experts}

    return {exp: raw_weights[exp] / total for exp in experts}


def compute_b3_quality_consensus(
    coin: str,
    postures: list[ExpertPosture],
    clusters: list[list[str]],
    quality_weights: dict[str, Decimal],
    now: datetime | None = None,
) -> ConsensusTarget:
    """B3: Cluster budget allocation weighted by lagged quality inside clusters (R7)."""
    current_time = now or datetime.now(UTC)
    active_clusters = [c for c in clusters if c]
    if not active_clusters or not postures:
        return ConsensusTarget(
            coin=coin,
            observed_target=Decimal(0),
            missing_mass=Decimal(0),
            lower_bound=Decimal(0),
            upper_bound=Decimal(0),
            cause=CausalChangeCategory.ECONOMIC_CHANGE,
            contributions=[],
            is_actionable=True,
            blocker_code=None,
            as_of=current_time,
        )

    cluster_budget = Decimal(1) / Decimal(len(active_clusters))
    posture_map = {p.expert_id: p for p in postures}
    contributions: list[ExpertContribution] = []
    observed_sum = Decimal(0)

    for cluster in active_clusters:
        cluster_experts = [exp for exp in cluster if exp in posture_map]
        if not cluster_experts:
            continue

        raw_q = {exp: quality_weights.get(exp, Decimal("1.0")) for exp in cluster_experts}
        total_q = sum(raw_q.values())
        if total_q <= Decimal(0):
            total_q = Decimal(1)

        for exp in cluster_experts:
            p = posture_map[exp]
            intra_weight = raw_q[exp] / total_q
            effective_weight = cluster_budget * intra_weight

            if p.is_voting:
                contrib = p.clipped_posture * effective_weight
                observed_sum += contrib
                contributions.append(
                    ExpertContribution(
                        expert_id=exp,
                        raw_posture=p.clipped_posture,
                        weight=effective_weight,
                        weighted_contribution=contrib,
                        state=p.state,
                        reason=p.reason,
                    )
                )
    observed_clamped = min(max(observed_sum, Decimal("-1.0")), Decimal("1.0"))
    return ConsensusTarget(
        coin=coin,
        observed_target=observed_clamped,
        missing_mass=Decimal(0),
        lower_bound=observed_clamped,
        upper_bound=observed_clamped,
        cause=CausalChangeCategory.ECONOMIC_CHANGE,
        contributions=contributions,
        is_actionable=True,
        blocker_code=None,
        as_of=current_time,
    )


def detect_lagged_regime(
    price_series: list[Decimal],
    high_vol_threshold_pct: Decimal = Decimal("0.03"),
) -> RegimeType:
    """Detect market regime using strictly lagged returns (R7, Q15)."""
    if len(price_series) < 3:
        return RegimeType.LOW_VOLATILITY

    returns = [
        abs((price_series[i] - price_series[i - 1]) / price_series[i - 1])
        for i in range(1, len(price_series))
        if price_series[i - 1] > Decimal(0)
    ]
    if not returns:
        return RegimeType.LOW_VOLATILITY

    avg_vol = sum(returns) / Decimal(len(returns))
    if avg_vol >= high_vol_threshold_pct:
        return RegimeType.HIGH_VOLATILITY
    return RegimeType.LOW_VOLATILITY


def compute_b4_regime_consensus(
    base_consensus: ConsensusTarget,
    regime: RegimeType,
) -> ConsensusTarget:
    """B4: Regime-conditioned target adjustment (R7).

    Under HIGH_VOLATILITY, dampens target posture by 0.70x to protect against tail risk.
    """
    if regime == RegimeType.HIGH_VOLATILITY:
        dampened_target = base_consensus.observed_target * Decimal("0.70")
        return ConsensusTarget(
            coin=base_consensus.coin,
            observed_target=dampened_target,
            missing_mass=base_consensus.missing_mass,
            lower_bound=dampened_target,
            upper_bound=dampened_target,
            cause=CausalChangeCategory.VALUATION_CHANGE,
            contributions=base_consensus.contributions,
            is_actionable=base_consensus.is_actionable,
            blocker_code=base_consensus.blocker_code,
            as_of=base_consensus.as_of,
        )
    return base_consensus


@dataclass(slots=True, frozen=True)
class ResearchPromotionRecord:
    candidate_id: str
    train_window: str
    eval_window: str
    search_count: int
    forward_sharpe_delta: Decimal
    turnover_delta: Decimal
    status: str  # "PROMOTED", "RETAIN_BASELINE", "AWAITING_APPROVAL"
    evidence_bar_cleared: bool
    retained_ablation: dict[str, Any]
    approver_grant: str | None


def evaluate_research_candidate(
    candidate_id: str,
    train_window: str,
    eval_window: str,
    forward_sharpe_delta: Decimal,
    turnover_delta: Decimal,
    search_count: int,
    required_sharpe_bar: Decimal = Decimal("0.15"),
    max_turnover_bar: Decimal = Decimal("0.10"),
    policy_grant: str | None = None,
) -> ResearchPromotionRecord:
    """Evaluate candidate B3/B4 against qualified baseline under Q15 promotion gates."""
    cleared = (forward_sharpe_delta >= required_sharpe_bar) and (turnover_delta <= max_turnover_bar)

    if cleared and policy_grant is not None:
        status = "PROMOTED"
    elif cleared and policy_grant is None:
        status = "AWAITING_APPROVAL"
    else:
        status = "RETAIN_BASELINE"

    ablation = {
        "candidate_id": candidate_id,
        "forward_sharpe_delta": str(forward_sharpe_delta),
        "turnover_delta": str(turnover_delta),
        "search_count": search_count,
        "required_sharpe_bar": str(required_sharpe_bar),
        "max_turnover_bar": str(max_turnover_bar),
        "cleared": cleared,
    }

    return ResearchPromotionRecord(
        candidate_id=candidate_id,
        train_window=train_window,
        eval_window=eval_window,
        search_count=search_count,
        forward_sharpe_delta=forward_sharpe_delta,
        turnover_delta=turnover_delta,
        status=status,
        evidence_bar_cleared=cleared,
        retained_ablation=ablation,
        approver_grant=policy_grant,
    )
