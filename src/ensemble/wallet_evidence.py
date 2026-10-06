"""Canonical per-wallet descriptive evidence for V2.

This module preserves raw exposure, trader intent flow, age semantics, and
reliability before any ensemble or predictive compression.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from .posture import EligibilityState, compute_posture


class EvidenceCause(str, Enum):
    ECONOMIC_CHANGE = "ECONOMIC_CHANGE"
    VALUATION_CHANGE = "VALUATION_CHANGE"
    EQUITY_CHANGE = "EQUITY_CHANGE"
    RECONCILIATION = "RECONCILIATION"


class IntentKind(str, Enum):
    OPEN = "OPEN"
    ADD = "ADD"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"
    FLIP = "FLIP"


DEFAULT_FLOW_WINDOWS: tuple[tuple[str, timedelta], ...] = (
    ("1m", timedelta(minutes=1)),
    ("5m", timedelta(minutes=5)),
    ("15m", timedelta(minutes=15)),
    ("1h", timedelta(hours=1)),
    ("4h", timedelta(hours=4)),
    ("24h", timedelta(hours=24)),
)


@dataclass(slots=True, frozen=True)
class IntentEvent:
    event_id: str
    expert_id: str
    coin: str
    kind: IntentKind
    quantity_before: Decimal
    quantity_after: Decimal
    delta_bias: Decimal | None
    event_time: datetime
    known_at: datetime


@dataclass(slots=True, frozen=True)
class FlowValue:
    horizon: str
    delta_bias: Decimal
    event_count: int


@dataclass(slots=True, frozen=True)
class WalletEvidence:
    expert_id: str
    coin: str
    quantity: Decimal
    valuation_price: Decimal
    equity: Decimal | None
    raw_bias: Decimal | None
    bounded_influence: Decimal | None
    portfolio_share: Decimal | None
    state: EligibilityState
    current_cause: EvidenceCause
    position_age_seconds: int | None
    intent_age_seconds: int | None
    observation_age_seconds: int
    last_intent: IntentEvent | None
    intent_flow: tuple[FlowValue, ...]
    input_revision: int
    as_of: datetime
    knowledge_time: datetime
    evidence_refs: tuple[str, ...]
    missing_reasons: tuple[str, ...]


def _sign(value: Decimal) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def classify_intent(quantity_before: Decimal, quantity_after: Decimal) -> IntentKind | None:
    """Classify one economic signed-position transition."""
    if quantity_before == quantity_after:
        return None
    if quantity_before == 0 and quantity_after != 0:
        return IntentKind.OPEN
    if quantity_before != 0 and quantity_after == 0:
        return IntentKind.CLOSE
    if _sign(quantity_before) != _sign(quantity_after):
        return IntentKind.FLIP
    if abs(quantity_after) > abs(quantity_before):
        return IntentKind.ADD
    if abs(quantity_after) < abs(quantity_before):
        return IntentKind.REDUCE
    return None


def derive_intent_event(
    *,
    event_id: str,
    expert_id: str,
    coin: str,
    quantity_before: Decimal,
    quantity_after: Decimal,
    valuation_price: Decimal,
    equity: Decimal | None,
    event_time: datetime,
    known_at: datetime,
    cause: EvidenceCause = EvidenceCause.ECONOMIC_CHANGE,
) -> IntentEvent | None:
    """Create an intent event only for a verified economic position transition."""
    if cause is not EvidenceCause.ECONOMIC_CHANGE:
        return None

    kind = classify_intent(quantity_before, quantity_after)
    if kind is None:
        return None

    delta_bias: Decimal | None = None
    if equity is not None and equity > 0:
        delta_bias = (quantity_after - quantity_before) * valuation_price / equity

    return IntentEvent(
        event_id=event_id,
        expert_id=expert_id,
        coin=coin,
        kind=kind,
        quantity_before=quantity_before,
        quantity_after=quantity_after,
        delta_bias=delta_bias,
        event_time=event_time,
        known_at=known_at,
    )


def compute_intent_flow(
    events: Sequence[IntentEvent],
    *,
    knowledge_time: datetime,
    windows: Sequence[tuple[str, timedelta]] = DEFAULT_FLOW_WINDOWS,
) -> tuple[FlowValue, ...]:
    """Compute no-decay point-in-time intent-flow window sums.

    Window membership uses known_at so later-arriving events cannot leak into
    an earlier as-known decision.
    """
    visible = [event for event in events if event.known_at <= knowledge_time]
    values: list[FlowValue] = []
    for label, window in windows:
        start = knowledge_time - window
        selected = [
            event
            for event in visible
            if start < event.known_at <= knowledge_time and event.delta_bias is not None
        ]
        values.append(
            FlowValue(
                horizon=label,
                delta_bias=sum(
                    (event.delta_bias for event in selected if event.delta_bias is not None),
                    start=Decimal(0),
                ),
                event_count=len(selected),
            )
        )
    return tuple(values)


def _elapsed_seconds(later: datetime, earlier: datetime | None) -> int | None:
    if earlier is None or earlier > later:
        return None
    return int((later - earlier).total_seconds())


def _portfolio_share(
    *,
    coin: str,
    instrument_notional: Decimal,
    portfolio_notionals: Mapping[str, Decimal] | None,
) -> Decimal | None:
    if portfolio_notionals is None:
        return None

    scoped = dict(portfolio_notionals)
    scoped[coin] = instrument_notional
    total = sum((abs(value) for value in scoped.values()), start=Decimal(0))
    if total == 0:
        return Decimal(0)
    return abs(instrument_notional) / total


def build_wallet_evidence(
    *,
    expert_id: str,
    coin: str,
    quantity: Decimal,
    valuation_price: Decimal,
    equity: Decimal | None,
    observation_time: datetime,
    as_of: datetime,
    knowledge_time: datetime,
    input_revision: int,
    current_cause: EvidenceCause,
    intent_events: Sequence[IntentEvent] = (),
    position_opened_at: datetime | None = None,
    portfolio_notionals: Mapping[str, Decimal] | None = None,
    evidence_refs: Sequence[str] = (),
    is_in_scope: bool = True,
    is_unseeded: bool = False,
    is_quarantined: bool = False,
    equity_age_minutes: float = 0.0,
    max_equity_age_minutes: float = 15.0,
    k_scale: Decimal = Decimal("1.0"),
    flow_windows: Sequence[tuple[str, timedelta]] = DEFAULT_FLOW_WINDOWS,
) -> WalletEvidence:
    """Build immutable descriptive evidence at one coherent knowledge cut."""
    posture = compute_posture(
        expert_id=expert_id,
        coin=coin,
        quantity=quantity,
        valuation_price=valuation_price,
        equity=equity,
        is_in_scope=is_in_scope,
        is_unseeded=is_unseeded,
        is_quarantined=is_quarantined,
        equity_age_minutes=equity_age_minutes,
        max_equity_age_minutes=max_equity_age_minutes,
        k_scale=k_scale,
    )

    bounded_influence: Decimal | None
    if posture.state in (EligibilityState.ELIGIBLE, EligibilityState.KNOWN_FLAT):
        bounded_influence = posture.clipped_posture
    else:
        bounded_influence = None

    visible_events = sorted(
        (event for event in intent_events if event.known_at <= knowledge_time),
        key=lambda event: (event.known_at, event.event_time, event.event_id),
    )
    last_intent = visible_events[-1] if visible_events else None

    instrument_notional = quantity * valuation_price
    missing_reasons = (posture.reason,) if posture.reason else ()

    position_age = (
        _elapsed_seconds(as_of, position_opened_at)
        if quantity != 0 and position_opened_at is not None
        else None
    )
    intent_age = _elapsed_seconds(as_of, last_intent.event_time) if last_intent else None
    observation_age = _elapsed_seconds(as_of, observation_time)
    if observation_age is None:
        observation_age = 0

    return WalletEvidence(
        expert_id=expert_id,
        coin=coin,
        quantity=quantity,
        valuation_price=valuation_price,
        equity=equity,
        raw_bias=posture.raw_exposure,
        bounded_influence=bounded_influence,
        portfolio_share=_portfolio_share(
            coin=coin,
            instrument_notional=instrument_notional,
            portfolio_notionals=portfolio_notionals,
        ),
        state=posture.state,
        current_cause=current_cause,
        position_age_seconds=position_age,
        intent_age_seconds=intent_age,
        observation_age_seconds=observation_age,
        last_intent=last_intent,
        intent_flow=compute_intent_flow(
            visible_events,
            knowledge_time=knowledge_time,
            windows=flow_windows,
        ),
        input_revision=input_revision,
        as_of=as_of,
        knowledge_time=knowledge_time,
        evidence_refs=tuple(evidence_refs),
        missing_reasons=missing_reasons,
    )
