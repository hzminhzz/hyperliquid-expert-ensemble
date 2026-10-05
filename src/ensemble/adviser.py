"""Account adviser: risk ceilings, lot rounding, and bounded advice (Contracts R6, Q12).

Transforms consensus targets into bounded, account-aware advice under explicit rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import Enum

from .consensus import ConsensusTarget


class AdviceStatus(str, Enum):
    SIZED = "SIZED"
    UNSIZED = "UNSIZED"
    BLOCKED = "BLOCKED"


@dataclass(slots=True, frozen=True)
class AccountRulebook:
    account_id: str
    starting_equity: Decimal
    daily_starting_equity: Decimal
    max_total_drawdown_pct: Decimal  # e.g. 0.10 for 10%
    max_daily_drawdown_pct: Decimal  # e.g. 0.05 for 5%
    max_risk_per_trade_pct: Decimal  # e.g. 0.01 for 1%


@dataclass(slots=True, frozen=True)
class AccountState:
    current_equity: Decimal
    existing_open_risk: Decimal  # aggregate stress risk of already open positions
    positions: dict[str, Decimal] | None  # None = unobserved/unknown holdings
    as_of: datetime


@dataclass(slots=True, frozen=True)
class InstrumentSpec:
    symbol: str
    contract_size: Decimal
    min_lot: Decimal
    lot_step: Decimal
    fee_rate: Decimal
    slippage_rate: Decimal


@dataclass(slots=True, frozen=True)
class LossModel:
    stop_distance_pct: Decimal | None  # None = missing stop loss model
    stress_multiplier: Decimal = Decimal("1.0")


@dataclass(slots=True, frozen=True)
class AccountAdvice:
    account_id: str
    coin: str
    status: AdviceStatus
    target_posture: Decimal
    target_quantity: Decimal | None
    action_delta: Decimal | None
    allowable_risk_usd: Decimal | None
    estimated_risk_usd: Decimal | None
    expires_at: datetime
    blocker_code: str | None
    notes: str


def generate_account_advice(
    target: ConsensusTarget,
    rulebook: AccountRulebook,
    state: AccountState,
    spec: InstrumentSpec | None,
    loss_model: LossModel | None,
    valuation_price: Decimal,
    now: datetime | None = None,
    expiry_minutes: float = 15.0,
) -> AccountAdvice:
    """Generate bounded account advice under R6 & Q12."""
    current_time = now or datetime.now(UTC)
    expires_at = current_time + timedelta(minutes=expiry_minutes)

    # 1. Blocked if consensus is non-actionable
    if not target.is_actionable:
        return AccountAdvice(
            account_id=rulebook.account_id,
            coin=target.coin,
            status=AdviceStatus.BLOCKED,
            target_posture=target.observed_target,
            target_quantity=None,
            action_delta=None,
            allowable_risk_usd=None,
            estimated_risk_usd=None,
            expires_at=expires_at,
            blocker_code=target.blocker_code or "CONSENSUS_UNAVAILABLE",
            notes="Consensus target is non-actionable due to missing mass or ambiguity",
        )

    # 2. UNSIZED fallback if spec or stop loss model is missing (Q12)
    if spec is None or loss_model is None or loss_model.stop_distance_pct is None:
        return AccountAdvice(
            account_id=rulebook.account_id,
            coin=target.coin,
            status=AdviceStatus.UNSIZED,
            target_posture=target.observed_target,
            target_quantity=None,
            action_delta=None,
            allowable_risk_usd=None,
            estimated_risk_usd=None,
            expires_at=expires_at,
            blocker_code="LOSS_MODEL_MISSING",
            notes="Missing broker instrument spec or explicit stop loss model; informational only",
        )

    # 3. Risk headroom calculations
    total_floor = rulebook.starting_equity * (Decimal("1.0") - rulebook.max_total_drawdown_pct)
    total_headroom = max(
        Decimal("0.0"), state.current_equity - total_floor - state.existing_open_risk
    )

    daily_floor = rulebook.daily_starting_equity * (
        Decimal("1.0") - rulebook.max_daily_drawdown_pct
    )
    daily_headroom = max(
        Decimal("0.0"), state.current_equity - daily_floor - state.existing_open_risk
    )

    max_per_trade_risk = state.current_equity * rulebook.max_risk_per_trade_pct

    allowable_risk = min(max_per_trade_risk, total_headroom, daily_headroom)

    # If risk headroom is exhausted:
    if allowable_risk <= Decimal("0.0"):
        return AccountAdvice(
            account_id=rulebook.account_id,
            coin=target.coin,
            status=AdviceStatus.BLOCKED,
            target_posture=target.observed_target,
            target_quantity=Decimal("0.0"),
            action_delta=Decimal("0.0") if state.positions is not None else None,
            allowable_risk_usd=Decimal("0.0"),
            estimated_risk_usd=Decimal("0.0"),
            expires_at=expires_at,
            blocker_code="RISK_HEADROOM_EXHAUSTED",
            notes="Account drawdown headroom or risk ceiling exhausted",
        )

    # 4. Notional and lot sizing with downward rounding
    unit_risk_pct = (
        loss_model.stop_distance_pct + spec.fee_rate + spec.slippage_rate
    ) * loss_model.stress_multiplier

    if unit_risk_pct <= Decimal("0.0"):
        unit_risk_pct = Decimal("0.01")

    max_allowable_notional = allowable_risk / unit_risk_pct
    max_units = max_allowable_notional / valuation_price

    raw_units = max_units * abs(target.observed_target)

    # Round DOWNWARD to lot step
    steps = (raw_units / spec.lot_step).quantize(Decimal(1), rounding=ROUND_FLOOR)
    rounded_units = steps * spec.lot_step

    # Enforce minimum lot floor
    if rounded_units < spec.min_lot:
        rounded_units = Decimal("0.0")

    # Apply direction sign
    sign = Decimal("1.0") if target.observed_target > Decimal("0.0") else Decimal("-1.0")
    if target.observed_target == Decimal("0.0"):
        sign = Decimal("0.0")
    signed_target_qty = rounded_units * sign

    estimated_risk = rounded_units * valuation_price * unit_risk_pct

    # 5. Action Delta derivation (R6 rule: only if account position is known!)
    action_delta = None
    if state.positions is not None:
        current_held = state.positions.get(target.coin, Decimal("0.0"))
        action_delta = signed_target_qty - current_held

    return AccountAdvice(
        account_id=rulebook.account_id,
        coin=target.coin,
        status=AdviceStatus.SIZED,
        target_posture=target.observed_target,
        target_quantity=signed_target_qty,
        action_delta=action_delta,
        allowable_risk_usd=allowable_risk,
        estimated_risk_usd=estimated_risk,
        expires_at=expires_at,
        blocker_code=None,
        notes="Sized within drawdown and trade risk limits",
    )
