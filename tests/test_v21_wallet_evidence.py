"""V21 qualification: canonical WalletEvidence semantics (Q04-Q07)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ensemble.posture import EligibilityState
from ensemble.wallet_evidence import (
    EvidenceCause,
    IntentKind,
    build_wallet_evidence,
    classify_intent,
    derive_intent_event,
)

T0 = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)


def _event(
    before: str,
    after: str,
    *,
    minutes: int = 0,
    cause: EvidenceCause = EvidenceCause.ECONOMIC_CHANGE,
):
    return derive_intent_event(
        event_id=f"e:{before}:{after}:{minutes}",
        expert_id="0xexpert",
        coin="BTC",
        quantity_before=Decimal(before),
        quantity_after=Decimal(after),
        valuation_price=Decimal(100),
        equity=Decimal(100),
        event_time=T0 + timedelta(minutes=minutes),
        known_at=T0 + timedelta(minutes=minutes),
        cause=cause,
    )


def test_q04_preserves_raw_bias_and_bounds_only_influence():
    evidence = build_wallet_evidence(
        expert_id="0xexpert",
        coin="BTC",
        quantity=Decimal(5),
        valuation_price=Decimal(100),
        equity=Decimal(100),
        portfolio_notionals={"ETH": Decimal(1500)},
        observation_time=T0,
        as_of=T0,
        knowledge_time=T0,
        input_revision=7,
        current_cause=EvidenceCause.ECONOMIC_CHANGE,
    )

    assert evidence.raw_bias == Decimal(5)
    assert evidence.bounded_influence == Decimal(1)
    assert evidence.portfolio_share == Decimal("0.25")
    assert evidence.state is EligibilityState.ELIGIBLE


def test_q05_classifies_only_economic_intent_transitions():
    assert classify_intent(Decimal(0), Decimal(1)) is IntentKind.OPEN
    assert classify_intent(Decimal(1), Decimal(2)) is IntentKind.ADD
    assert classify_intent(Decimal(2), Decimal(1)) is IntentKind.REDUCE
    assert classify_intent(Decimal(1), Decimal(0)) is IntentKind.CLOSE
    assert classify_intent(Decimal(1), Decimal(-1)) is IntentKind.FLIP
    assert classify_intent(Decimal(1), Decimal(1)) is None

    for cause in (
        EvidenceCause.VALUATION_CHANGE,
        EvidenceCause.EQUITY_CHANGE,
        EvidenceCause.RECONCILIATION,
    ):
        assert _event("1", "2", cause=cause) is None


def test_q06_reduce_keeps_positive_state_and_creates_negative_flow():
    reduce_event = _event("2", "1")
    assert reduce_event is not None
    assert reduce_event.kind is IntentKind.REDUCE
    assert reduce_event.delta_bias == Decimal(-1)

    evidence = build_wallet_evidence(
        expert_id="0xexpert",
        coin="BTC",
        quantity=Decimal(1),
        valuation_price=Decimal(100),
        equity=Decimal(100),
        observation_time=T0,
        as_of=T0 + timedelta(seconds=30),
        knowledge_time=T0 + timedelta(seconds=30),
        input_revision=8,
        current_cause=EvidenceCause.ECONOMIC_CHANGE,
        intent_events=[reduce_event],
        position_opened_at=T0 - timedelta(hours=2),
    )

    assert evidence.raw_bias == Decimal(1)
    assert evidence.bounded_influence == Decimal(1)
    flow_1m = next(value for value in evidence.intent_flow if value.horizon == "1m")
    assert flow_1m.delta_bias == Decimal(-1)
    assert flow_1m.event_count == 1


def test_q06_tiny_add_on_large_position_remains_small_flow():
    tiny_add = derive_intent_event(
        event_id="tiny-add",
        expert_id="0xexpert",
        coin="BTC",
        quantity_before=Decimal(10),
        quantity_after=Decimal("10.1"),
        valuation_price=Decimal(100),
        equity=Decimal(1000),
        event_time=T0,
        known_at=T0,
    )
    assert tiny_add is not None
    assert tiny_add.kind is IntentKind.ADD
    assert tiny_add.delta_bias == Decimal("0.01")

    evidence = build_wallet_evidence(
        expert_id="0xexpert",
        coin="BTC",
        quantity=Decimal("10.1"),
        valuation_price=Decimal(100),
        equity=Decimal(1000),
        observation_time=T0,
        as_of=T0 + timedelta(seconds=5),
        knowledge_time=T0 + timedelta(seconds=5),
        input_revision=9,
        current_cause=EvidenceCause.ECONOMIC_CHANGE,
        intent_events=[tiny_add],
    )

    assert evidence.raw_bias == Decimal("1.01")
    assert evidence.bounded_influence == Decimal(1)
    assert next(v for v in evidence.intent_flow if v.horizon == "1m").delta_bias == Decimal(
        "0.01"
    )


def test_q06_persistent_state_does_not_repeat_flow_and_late_events_do_not_leak():
    old_open = _event("0", "0.7", minutes=-(25 * 60))
    late_add = derive_intent_event(
        event_id="late",
        expert_id="0xexpert",
        coin="BTC",
        quantity_before=Decimal("0.7"),
        quantity_after=Decimal("0.8"),
        valuation_price=Decimal(100),
        equity=Decimal(100),
        event_time=T0 - timedelta(minutes=1),
        known_at=T0 + timedelta(minutes=10),
    )
    assert old_open is not None
    assert late_add is not None

    evidence = build_wallet_evidence(
        expert_id="0xexpert",
        coin="BTC",
        quantity=Decimal("0.7"),
        valuation_price=Decimal(100),
        equity=Decimal(100),
        observation_time=T0,
        as_of=T0,
        knowledge_time=T0,
        input_revision=10,
        current_cause=EvidenceCause.VALUATION_CHANGE,
        intent_events=[old_open, late_add],
    )

    assert evidence.raw_bias == Decimal("0.7")
    for flow in evidence.intent_flow:
        assert flow.delta_bias == Decimal(0)
        assert flow.event_count == 0
    assert evidence.last_intent == old_open


def test_q07_position_intent_and_observation_ages_are_distinct():
    add = _event("1", "1.2", minutes=-5)
    assert add is not None

    evidence = build_wallet_evidence(
        expert_id="0xexpert",
        coin="BTC",
        quantity=Decimal("1.2"),
        valuation_price=Decimal(100),
        equity=Decimal(100),
        observation_time=T0 - timedelta(seconds=20),
        as_of=T0,
        knowledge_time=T0,
        input_revision=11,
        current_cause=EvidenceCause.ECONOMIC_CHANGE,
        intent_events=[add],
        position_opened_at=T0 - timedelta(hours=3),
    )

    assert evidence.position_age_seconds == 3 * 60 * 60
    assert evidence.intent_age_seconds == 5 * 60
    assert evidence.observation_age_seconds == 20

    partial_history = build_wallet_evidence(
        expert_id="0xexpert",
        coin="BTC",
        quantity=Decimal("1.2"),
        valuation_price=Decimal(100),
        equity=Decimal(100),
        observation_time=T0,
        as_of=T0,
        knowledge_time=T0,
        input_revision=12,
        current_cause=EvidenceCause.RECONCILIATION,
        position_opened_at=None,
    )
    assert partial_history.position_age_seconds is None


def test_unavailable_equity_never_turns_missing_bias_into_zero():
    evidence = build_wallet_evidence(
        expert_id="0xexpert",
        coin="BTC",
        quantity=Decimal(1),
        valuation_price=Decimal(100),
        equity=None,
        observation_time=T0,
        as_of=T0,
        knowledge_time=T0,
        input_revision=13,
        current_cause=EvidenceCause.EQUITY_CHANGE,
    )

    assert evidence.state is EligibilityState.UNAVAILABLE
    assert evidence.raw_bias is None
    assert evidence.bounded_influence is None
    assert evidence.missing_reasons == ("equity is missing or non-positive",)
