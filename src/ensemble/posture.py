"""Expert posture models and fixed-scale normalization (Contracts R2, Q10).

Normalizes raw observed position size and valuation price against scoped account equity.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class EligibilityState(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    KNOWN_FLAT = "KNOWN_FLAT"
    ABSTAINING = "ABSTAINING"
    UNAVAILABLE = "UNAVAILABLE"
    REMOVED = "REMOVED"


@dataclass(slots=True, frozen=True)
class ExpertPosture:
    expert_id: str
    coin: str
    quantity: Decimal
    valuation_price: Decimal
    equity: Decimal | None
    raw_exposure: Decimal | None
    clipped_posture: Decimal
    state: EligibilityState
    reason: str | None = None

    @property
    def is_voting(self) -> bool:
        """True if the expert has valid voting posture (eligible or known flat)."""
        return self.state in (EligibilityState.ELIGIBLE, EligibilityState.KNOWN_FLAT)


def compute_posture(
    expert_id: str,
    coin: str,
    quantity: Decimal,
    valuation_price: Decimal,
    equity: Decimal | None,
    is_in_scope: bool = True,
    is_unseeded: bool = False,
    is_quarantined: bool = False,
    equity_age_minutes: float = 0.0,
    max_equity_age_minutes: float = 15.0,
    k_scale: Decimal = Decimal("1.0"),
) -> ExpertPosture:
    """Compute normalized expert posture under declared eligibility rules (R2).

    Formula:
      e(i,a) = quantity * valuation_price / equity
      s(i,a) = clip(e(i,a) / k, -1.0, 1.0)
    """
    # 1. Out of scope experts abstain
    if not is_in_scope:
        return ExpertPosture(
            expert_id=expert_id,
            coin=coin,
            quantity=quantity,
            valuation_price=valuation_price,
            equity=equity,
            raw_exposure=None,
            clipped_posture=Decimal("0.0"),
            state=EligibilityState.ABSTAINING,
            reason="expert is out of horizon or instrument scope",
        )

    # 2. Quarantined or unseeded states are unavailable
    if is_quarantined:
        return ExpertPosture(
            expert_id=expert_id,
            coin=coin,
            quantity=quantity,
            valuation_price=valuation_price,
            equity=equity,
            raw_exposure=None,
            clipped_posture=Decimal("0.0"),
            state=EligibilityState.UNAVAILABLE,
            reason="observation state is quarantined",
        )

    if is_unseeded:
        return ExpertPosture(
            expert_id=expert_id,
            coin=coin,
            quantity=quantity,
            valuation_price=valuation_price,
            equity=equity,
            raw_exposure=None,
            clipped_posture=Decimal("0.0"),
            state=EligibilityState.UNAVAILABLE,
            reason="expert has not completed startup seed",
        )

    # 3. Missing equity means there is no authoritative account snapshot to normalize against.
    if equity is None:
        return ExpertPosture(
            expert_id=expert_id,
            coin=coin,
            quantity=quantity,
            valuation_price=valuation_price,
            equity=equity,
            raw_exposure=None,
            clipped_posture=Decimal("0.0"),
            state=EligibilityState.UNAVAILABLE,
            reason="equity is missing or non-positive",
        )

    # 4. Stale account state cannot silently produce fresh posture, even when currently flat.
    if equity_age_minutes > max_equity_age_minutes:
        return ExpertPosture(
            expert_id=expert_id,
            coin=coin,
            quantity=quantity,
            valuation_price=valuation_price,
            equity=equity,
            raw_exposure=None,
            clipped_posture=Decimal("0.0"),
            state=EligibilityState.UNAVAILABLE,
            reason=f"equity is stale ({equity_age_minutes:.1f}m > {max_equity_age_minutes:.1f}m)",
        )

    # 5. A fresh authoritative flat snapshot is exactly zero exposure even if account value is
    # zero. No division is required, so zero equity must not create artificial missing mass.
    if quantity == Decimal(0):
        return ExpertPosture(
            expert_id=expert_id,
            coin=coin,
            quantity=quantity,
            valuation_price=valuation_price,
            equity=equity,
            raw_exposure=Decimal("0.0"),
            clipped_posture=Decimal("0.0"),
            state=EligibilityState.KNOWN_FLAT,
            reason="reliably observed flat position",
        )

    # 6. Non-positive equity makes non-flat normalization unavailable.
    if equity <= Decimal(0):
        return ExpertPosture(
            expert_id=expert_id,
            coin=coin,
            quantity=quantity,
            valuation_price=valuation_price,
            equity=equity,
            raw_exposure=None,
            clipped_posture=Decimal("0.0"),
            state=EligibilityState.UNAVAILABLE,
            reason="equity is missing or non-positive",
        )

    # 7. Eligible position: calculate normalized posture
    notional = quantity * valuation_price
    raw_exposure = notional / equity
    scaled = raw_exposure / k_scale
    clipped = min(Decimal("1.0"), max(Decimal("-1.0"), scaled))

    return ExpertPosture(
        expert_id=expert_id,
        coin=coin,
        quantity=quantity,
        valuation_price=valuation_price,
        equity=equity,
        raw_exposure=raw_exposure,
        clipped_posture=clipped,
        state=EligibilityState.ELIGIBLE,
        reason=None,
    )
