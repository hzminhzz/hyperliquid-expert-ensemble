"""Baseline versus candidate evaluation and research promotion (Contracts R7, Q15).

Compares B1 equal baseline against B2 cluster candidate under strict evidence gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .consensus import ConsensusTarget


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
