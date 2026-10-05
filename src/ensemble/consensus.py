"""Fixed-budget hierarchical consensus and missing-mass bounds (Contracts R3, Q10).

Calculates baseline equal-budget consensus (B1) and maintains missing-mass uncertainty bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from .posture import EligibilityState, ExpertPosture


class CausalChangeCategory(str, Enum):
    ECONOMIC_CHANGE = "ECONOMIC_CHANGE"
    VALUATION_CHANGE = "VALUATION_CHANGE"
    EQUITY_CHANGE = "EQUITY_CHANGE"
    RECONCILIATION = "RECONCILIATION"
    POLICY_CHANGE = "POLICY_CHANGE"


@dataclass(slots=True, frozen=True)
class ExpertContribution:
    expert_id: str
    raw_posture: Decimal
    weight: Decimal
    weighted_contribution: Decimal
    state: EligibilityState
    reason: str | None


@dataclass(slots=True, frozen=True)
class ConsensusTarget:
    coin: str
    observed_target: Decimal
    missing_mass: Decimal
    lower_bound: Decimal
    upper_bound: Decimal
    cause: CausalChangeCategory
    contributions: list[ExpertContribution]
    is_actionable: bool
    blocker_code: str | None
    as_of: datetime

    @property
    def is_ambiguous(self) -> bool:
        """True if the missing-mass interval spans zero while observed target is non-zero."""
        return self.lower_bound < Decimal(0) < self.upper_bound and self.observed_target != Decimal(
            0
        )


def compute_equal_budget_consensus(
    coin: str,
    postures: list[ExpertPosture],
    prior_target: ConsensusTarget | None = None,
    cause: CausalChangeCategory = CausalChangeCategory.ECONOMIC_CHANGE,
    as_of: datetime | None = None,
) -> ConsensusTarget:
    """Compute baseline B1 equal-budget consensus under R3 & Q10.

    CRITICAL INVARIANT:
      Missing mass 'm' from unavailable experts is NOT reallocated to survivors.
      Surviving expert votes are not amplified by missing peers.
    """
    now = as_of or datetime.now(UTC)
    total_experts = len(postures)

    if total_experts == 0:
        return ConsensusTarget(
            coin=coin,
            observed_target=Decimal("0.0"),
            missing_mass=Decimal("0.0"),
            lower_bound=Decimal("0.0"),
            upper_bound=Decimal("0.0"),
            cause=cause,
            contributions=[],
            is_actionable=False,
            blocker_code="UNIVERSE_EMPTY",
            as_of=now,
        )

    # Equal weight budget: w_i = 1 / N
    expert_weight = Decimal("1.0") / Decimal(str(total_experts))

    observed_target = Decimal("0.0")
    missing_mass = Decimal("0.0")
    contributions = []

    for p in postures:
        if p.state in (EligibilityState.ELIGIBLE, EligibilityState.KNOWN_FLAT):
            # Contributes its weighted posture
            w_contrib = p.clipped_posture * expert_weight
            observed_target += w_contrib
            contributions.append(
                ExpertContribution(
                    expert_id=p.expert_id,
                    raw_posture=p.clipped_posture,
                    weight=expert_weight,
                    weighted_contribution=w_contrib,
                    state=p.state,
                    reason=p.reason,
                )
            )
        elif p.state == EligibilityState.UNAVAILABLE:
            # Missing mass is accumulated; NOT reallocated to other experts!
            missing_mass += expert_weight
            contributions.append(
                ExpertContribution(
                    expert_id=p.expert_id,
                    raw_posture=Decimal("0.0"),
                    weight=expert_weight,
                    weighted_contribution=Decimal("0.0"),
                    state=p.state,
                    reason=p.reason,
                )
            )
        elif p.state == EligibilityState.ABSTAINING:
            # Out of scope: does not vote
            contributions.append(
                ExpertContribution(
                    expert_id=p.expert_id,
                    raw_posture=Decimal("0.0"),
                    weight=expert_weight,
                    weighted_contribution=Decimal("0.0"),
                    state=p.state,
                    reason=p.reason,
                )
            )
        elif p.state == EligibilityState.REMOVED:
            contributions.append(
                ExpertContribution(
                    expert_id=p.expert_id,
                    raw_posture=Decimal("0.0"),
                    weight=Decimal("0.0"),
                    weighted_contribution=Decimal("0.0"),
                    state=p.state,
                    reason="removed from universe",
                )
            )

    # Missing-information bounds: [C_observed - m, C_observed + m] clipped to [-1, 1]
    lower_bound = max(Decimal("-1.0"), min(Decimal("1.0"), observed_target - missing_mass))
    upper_bound = max(Decimal("-1.0"), min(Decimal("1.0"), observed_target + missing_mass))

    is_actionable = True
    blocker_code = None

    # Check for ambiguity or excessive missing mass
    if missing_mass >= Decimal("0.5"):
        is_actionable = False
        blocker_code = "INSUFFICIENT_COVERAGE"
    elif lower_bound < Decimal(0) < upper_bound and observed_target != Decimal(0):
        is_actionable = False
        blocker_code = "AMBIGUOUS_BOUNDS"

    return ConsensusTarget(
        coin=coin,
        observed_target=observed_target,
        missing_mass=missing_mass,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        cause=cause,
        contributions=contributions,
        is_actionable=is_actionable,
        blocker_code=blocker_code,
        as_of=now,
    )
