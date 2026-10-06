"""ER1 qualification: additive point-in-time wallet path evidence."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ensemble.wallet_evidence import (
    EvidenceCause,
    IntentKind,
    allocate_transition_legs,
    build_wallet_evidence,
    derive_intent_event,
)

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def event(
    event_id: str,
    before: str,
    after: str,
    *,
    event_seconds: int,
    known_seconds: int | None = None,
):
    return derive_intent_event(
        event_id=event_id,
        expert_id="wallet",
        coin="hyperliquid:mainnet:perp:default:BTC",
        quantity_before=Decimal(before),
        quantity_after=Decimal(after),
        valuation_price=Decimal(100),
        equity=Decimal(1000),
        event_time=T0 + timedelta(seconds=event_seconds),
        known_at=T0 + timedelta(
            seconds=event_seconds if known_seconds is None else known_seconds
        ),
    )


def build(events, *, quantity="1", equity=Decimal(1000), equity_age=0.0, as_of_seconds=60):
    return build_wallet_evidence(
        expert_id="wallet",
        coin="hyperliquid:mainnet:perp:default:BTC",
        quantity=Decimal(quantity),
        valuation_price=Decimal(100),
        equity=equity,
        observation_time=T0 + timedelta(seconds=as_of_seconds),
        as_of=T0 + timedelta(seconds=as_of_seconds),
        knowledge_time=T0 + timedelta(seconds=as_of_seconds),
        input_revision=1,
        current_cause=EvidenceCause.ECONOMIC_CHANGE,
        intent_events=[e for e in events if e is not None],
        equity_age_minutes=equity_age,
    )


def flow(evidence, horizon="1m"):
    return next(item for item in evidence.intent_flow if item.horizon == horizon)


def test_split_invariance_preserves_net_gross_and_normalized_flow():
    one = event("one", "0", "1", event_seconds=30)
    split = [
        event(f"s{i}", str(Decimal(i) / 10), str(Decimal(i + 1) / 10), event_seconds=30)
        for i in range(10)
    ]

    a = flow(build([one]))
    b = flow(build(split))

    assert a.net_quantity == b.net_quantity == Decimal(1)
    assert a.gross_quantity == b.gross_quantity == Decimal(1)
    assert a.delta_bias == b.delta_bias == Decimal("0.1")
    assert a.gross_bias == b.gross_bias == Decimal("0.1")
    assert a.quantity_excursion == b.quantity_excursion == Decimal(1)
    assert a.event_count == 1
    assert b.event_count == 10


def test_round_trip_cancels_net_but_preserves_gross_excursion_and_close_context():
    opened = event("open", "0", "1", event_seconds=10)
    closed = event("close", "1", "0", event_seconds=50)
    value = flow(build([opened, closed], quantity="0"))

    assert value.net_quantity == 0
    assert value.gross_quantity == 2
    assert value.delta_bias == 0
    assert value.gross_bias == Decimal("0.2")
    assert value.net_to_gross == 0
    assert value.quantity_excursion == 1
    assert value.recent_close is True


def test_flip_legs_are_disjoint_and_sum_to_real_fill():
    legs = allocate_transition_legs(Decimal(3), Decimal(-2))
    assert [leg.kind for leg in legs] == [IntentKind.CLOSE, IntentKind.OPEN]
    assert [leg.delta_quantity for leg in legs] == [Decimal(-3), Decimal(-2)]
    assert sum((leg.delta_quantity for leg in legs), start=Decimal(0)) == Decimal(-5)


def test_reduce_keeps_long_state_and_negative_additive_flow():
    reduced = event("reduce", "2", "1", event_seconds=30)
    evidence = build([reduced], quantity="1")
    value = flow(evidence)

    assert evidence.raw_bias == Decimal("0.1")
    assert evidence.bounded_influence == Decimal("0.1")
    assert value.net_quantity == Decimal(-1)
    assert value.delta_bias == Decimal("-0.1")


def test_old_backfill_does_not_become_fresh_when_known_recently():
    old = event("old", "0", "1", event_seconds=-3600, known_seconds=30)
    evidence = build([old], quantity="1")
    value = flow(evidence)

    assert value.net_quantity == 0
    assert value.gross_quantity == 0
    assert value.event_count == 0
    assert evidence.last_intent == old


def test_stale_or_missing_equity_preserves_raw_quantity_but_normalized_flow_is_missing():
    add = event("add", "0", "1", event_seconds=30)

    missing = flow(build([add], equity=None))
    stale = flow(build([add], equity=Decimal(1000), equity_age=20.0))

    for value in (missing, stale):
        assert value.net_quantity == 1
        assert value.gross_quantity == 1
        assert value.delta_bias is None
        assert value.gross_bias is None


def test_non_execution_causes_never_create_path_events():
    for cause in (
        EvidenceCause.VALUATION_CHANGE,
        EvidenceCause.EQUITY_CHANGE,
        EvidenceCause.RECONCILIATION,
    ):
        derived = derive_intent_event(
            event_id=cause.value,
            expert_id="wallet",
            coin="BTC",
            quantity_before=Decimal(1),
            quantity_after=Decimal(2),
            valuation_price=Decimal(100),
            equity=Decimal(1000),
            event_time=T0,
            known_at=T0,
            cause=cause,
        )
        assert derived is None
