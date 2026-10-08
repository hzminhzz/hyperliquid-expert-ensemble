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
    # Backward-compatible per-event view only. ER1 aggregation never sums this field.
    delta_bias: Decimal | None
    event_time: datetime
    known_at: datetime

    @property
    def delta_quantity(self) -> Decimal:
        return self.quantity_after - self.quantity_before


@dataclass(slots=True, frozen=True)
class FlowValue:
    horizon: str
    net_quantity: Decimal
    gross_quantity: Decimal
    delta_bias: Decimal | None
    gross_bias: Decimal | None
    net_to_gross: Decimal | None
    build_rate_per_second: Decimal
    quantity_excursion: Decimal
    recent_close: bool
    event_count: int
    first_event_time: datetime | None
    last_event_time: datetime | None


@dataclass(slots=True, frozen=True)
class TransitionLeg:
    kind: IntentKind
    quantity_before: Decimal
    quantity_after: Decimal

    @property
    def delta_quantity(self) -> Decimal:
        return self.quantity_after - self.quantity_before


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
    normalization_anchor_time: datetime | None
    normalization_available: bool


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


def allocate_transition_legs(
    quantity_before: Decimal, quantity_after: Decimal
) -> tuple[TransitionLeg, ...]:
    """Return disjoint lifecycle legs without creating extra executed quantity."""
    kind = classify_intent(quantity_before, quantity_after)
    if kind is None:
        return ()
    if kind is not IntentKind.FLIP:
        return (
            TransitionLeg(
                kind=kind,
                quantity_before=quantity_before,
                quantity_after=quantity_after,
            ),
        )
    return (
        TransitionLeg(
            kind=IntentKind.CLOSE,
            quantity_before=quantity_before,
            quantity_after=Decimal(0),
        ),
        TransitionLeg(
            kind=IntentKind.OPEN,
            quantity_before=Decimal(0),
            quantity_after=quantity_after,
        ),
    )


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
    as_of: datetime | None = None,
    valuation_price: Decimal | None = None,
    equity: Decimal | None = None,
    normalization_available: bool | None = None,
    windows: Sequence[tuple[str, timedelta]] = DEFAULT_FLOW_WINDOWS,
) -> tuple[FlowValue, ...]:
    """Compute additive flow at one event/knowledge cut.

    ``known_at`` controls visibility; ``event_time`` controls horizon membership.
    Quantity is accumulated before one common normalization anchor is applied.
    """
    cut_time = as_of or knowledge_time
    visible = [
        event
        for event in events
        if event.known_at <= knowledge_time and event.event_time <= cut_time
    ]
    can_normalize = (
        normalization_available
        if normalization_available is not None
        else valuation_price is not None and equity is not None and equity > 0
    )
    if can_normalize and (valuation_price is None or equity is None or equity <= 0):
        can_normalize = False

    values: list[FlowValue] = []
    for label, window in windows:
        start = cut_time - window
        selected = [
            event for event in visible if start < event.event_time <= cut_time
        ]
        net_quantity = sum(
            (event.delta_quantity for event in selected), start=Decimal(0)
        )
        gross_quantity = sum(
            (abs(event.delta_quantity) for event in selected), start=Decimal(0)
        )
        normalized_net: Decimal | None = None
        normalized_gross: Decimal | None = None
        if can_normalize and valuation_price is not None and equity is not None:
            scale = valuation_price / equity
            normalized_net = scale * net_quantity
            normalized_gross = scale * gross_quantity
        net_to_gross = (
            abs(net_quantity) / gross_quantity if gross_quantity > 0 else None
        )
        ordered = sorted(
            selected,
            key=lambda event: (event.event_time, event.known_at, event.event_id),
        )
        cumulative = Decimal(0)
        path = [Decimal(0)]
        for event in ordered:
            cumulative += event.delta_quantity
            path.append(cumulative)
        excursion = max(path) - min(path)
        window_seconds = Decimal(str(window.total_seconds()))
        build_rate = net_quantity / window_seconds if window_seconds > 0 else Decimal(0)
        values.append(
            FlowValue(
                horizon=label,
                net_quantity=net_quantity,
                gross_quantity=gross_quantity,
                delta_bias=normalized_net,
                gross_bias=normalized_gross,
                net_to_gross=net_to_gross,
                build_rate_per_second=build_rate,
                quantity_excursion=excursion,
                recent_close=any(
                    event.kind in (IntentKind.CLOSE, IntentKind.FLIP) for event in selected
                ),
                event_count=len(selected),
                first_event_time=ordered[0].event_time if ordered else None,
                last_event_time=ordered[-1].event_time if ordered else None,
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
        (
            event
            for event in intent_events
            if event.known_at <= knowledge_time and event.event_time <= as_of
        ),
        key=lambda event: (event.event_time, event.known_at, event.event_id),
    )
    last_intent = visible_events[-1] if visible_events else None

    instrument_notional = quantity * valuation_price
    missing_reasons = (posture.reason,) if posture.reason else ()
    normalization_available = posture.state in (
        EligibilityState.ELIGIBLE,
        EligibilityState.KNOWN_FLAT,
    )

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
            as_of=as_of,
            valuation_price=valuation_price,
            equity=equity,
            normalization_available=normalization_available,
            windows=flow_windows,
        ),
        input_revision=input_revision,
        as_of=as_of,
        knowledge_time=knowledge_time,
        evidence_refs=tuple(evidence_refs),
        missing_reasons=missing_reasons,
        normalization_anchor_time=(observation_time if normalization_available else None),
        normalization_available=normalization_available,
    )
