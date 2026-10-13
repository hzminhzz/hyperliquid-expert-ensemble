"""DI3/DI5 forward outcomes, blocker and immutable-control tests."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ensemble.copy_opportunities import OpportunityJournal, TradeTransition
from ensemble.copy_research import (
    CopyResearchStore,
    PairedControl,
    RecordedMid,
    copy_markout,
    shrink_wallet_skill,
)

AT=datetime(2026,10,13,12,tzinfo=UTC)
COIN="hyperliquid:mainnet:perp:default:BTC"


def opportunity(tmp_path, *, before="0", after="1"):
    from ensemble.wallet_evidence import classify_intent

    j=OpportunityJournal(tmp_path/"opportunities.db")
    t=TradeTransition(
        source_id="e1", wallet_id="w1", instrument_id=COIN,
        source_kind=classify_intent(Decimal(before),Decimal(after)).value,
        quantity_before=Decimal(before),quantity_after=Decimal(after),
        price=Decimal(100),event_at=AT,known_at=AT+timedelta(seconds=1),
        ledger_id="ledger",ledger_seq=1,
    )
    o=j.observe(t)
    j.close()
    assert o is not None
    return o


def test_follower_latency_and_cost_applied_after_first_actionable_time(tmp_path) -> None:
    o=opportunity(tmp_path)
    samples=[
        RecordedMid(COIN,AT+timedelta(seconds=1),Decimal(100)),
        RecordedMid(COIN,AT+timedelta(seconds=4),Decimal(101)),
        RecordedMid(COIN,AT+timedelta(minutes=5,seconds=4),Decimal(102)),
    ]
    r=copy_markout(o,"5m",samples,now=AT+timedelta(minutes=7))
    assert r.status=="VALID_HYPOTHETICAL"
    assert r.entry_mid==Decimal(101)
    assert r.hypothetical_net_return==Decimal(102)/Decimal(101)-1-Decimal("0.001")
    assert r.price_model=="MID_TO_MID_HYPOTHETICAL_NOT_EXECUTABLE"


def test_short_cost_never_reverses_sign_and_flat_prices_lose(tmp_path) -> None:
    o=opportunity(tmp_path,before="0",after="-2")
    samples=[
        RecordedMid(COIN,AT+timedelta(seconds=4),Decimal(100)),
        RecordedMid(COIN,AT+timedelta(minutes=5,seconds=5),Decimal(100)),
    ]
    r=copy_markout(o,"5m",samples,now=AT+timedelta(minutes=7))
    assert r.hypothetical_net_return==Decimal("-0.001")


def test_de_risk_is_not_a_short_outcome(tmp_path) -> None:
    o=opportunity(tmp_path,before="2",after="1")
    r=copy_markout(o,"5m",[],now=AT+timedelta(hours=1))
    assert r.status=="NO_COPY"
    assert r.direction==0 and r.hypothetical_net_return is None


def test_gap_and_immature_outcome_are_not_backfilled(tmp_path) -> None:
    o=opportunity(tmp_path)
    future=RecordedMid(COIN,AT+timedelta(minutes=6),Decimal(120))
    assert copy_markout(o,"5m",[future],now=AT+timedelta(minutes=5)).status=="PENDING"
    assert copy_markout(o,"5m",[future],now=AT+timedelta(minutes=8)).status=="CENSORED"


def test_outcome_is_immutable_and_requires_market_control(tmp_path) -> None:
    o=opportunity(tmp_path)
    r=copy_markout(
        o,"5m",[
            RecordedMid(COIN,AT+timedelta(seconds=4),Decimal(100)),
            RecordedMid(COIN,AT+timedelta(minutes=5,seconds=4),Decimal(102)),
        ],now=AT+timedelta(minutes=7)
    )
    store=CopyResearchStore(tmp_path/"research.db")
    try:
        assert store.record(r,recorded_at=AT+timedelta(minutes=7))
        assert not store.record(r,recorded_at=AT+timedelta(minutes=7))
        assert store.summary(horizon="5m")["qualification"]=="INCONCLUSIVE_NO_MATCHED_CONTROLS"
        with pytest.raises(ValueError,match="immutable"):
            store.record(replace(r,cost_bps=Decimal(20)),recorded_at=AT+timedelta(minutes=7))
        control=PairedControl(
            opportunity_id=o.opportunity_id,horizon="5m",
            market_only_net_return=Decimal("0.015"),
            control_family="historically_matched_momentum",
            control_revision="frozen-v1",eligible_at=AT-timedelta(minutes=1),
        )
        assert store.attach_control(control)
        assert not store.attach_control(control)
        assert store.summary(
            horizon="5m",control_revision="frozen-v1"
        )["qualification"]=="RESEARCH_DIAGNOSTIC_ONLY_NO_FORWARD_GATE"
    finally:
        store.close()


def test_lagged_wallet_skill_cannot_use_future_or_single_day(tmp_path) -> None:
    deltas=[
        ("w1",AT-timedelta(days=1),Decimal("0.1")),
        ("w1",AT+timedelta(days=1),Decimal("0.9")),
    ]
    r=shrink_wallet_skill(deltas,evaluation_at=AT)
    assert r["w1"]["status"]=="UNKNOWN_INSUFFICIENT_INDEPENDENT_DAYS"
    assert r["w1"]["n"]==1
