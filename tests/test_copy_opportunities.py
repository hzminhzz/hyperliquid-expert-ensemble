"""DI1/DI2 causal trade interpretation and append-only opportunity replay."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ensemble.copy_opportunities import (
    CopyKind,
    OpportunityJournal,
    OpportunityStatus,
    TradeTransition,
    classify_copy_action,
)

BASE = datetime(2026, 10, 13, 0, tzinfo=UTC)
COIN = "hyperliquid:mainnet:perp:default:BTC"


def trade(
    seq: int, before: str, after: str, *,
    at: datetime = BASE, wallet: str = "w1", coverage: str = "SUPPORTED",
) -> TradeTransition:
    from ensemble.wallet_evidence import classify_intent

    q0, q1 = Decimal(before), Decimal(after)
    return TradeTransition(
        source_id=f"event-{seq}", wallet_id=wallet, instrument_id=COIN,
        source_kind=classify_intent(q0, q1).value,
        quantity_before=q0, quantity_after=q1, price=Decimal(100),
        event_at=at, known_at=at + timedelta(seconds=1),
        ledger_id="ledger-1", ledger_seq=seq, coverage=coverage,
    )


@pytest.mark.parametrize(("before","after","action","direction"),[
    ("0","1","OPEN_LONG",1),
    ("1","2","ADD_LONG",1),
    ("2","1","REDUCE_LONG",None),
    ("1","0","CLOSE_LONG",None),
    ("0","-1","OPEN_SHORT",-1),
    ("-1","-2","ADD_SHORT",-1),
    ("-2","-1","REDUCE_SHORT",None),
    ("-1","0","CLOSE_SHORT",None),
    ("2","-3","FLIP_TO_SHORT",-1),
    ("-3","2","FLIP_TO_LONG",1),
])
def test_semantics_are_not_raw_delta_direction(
    before: str,after: str,action: str,direction: int | None,
) -> None:
    _, result, actual = classify_copy_action(Decimal(before),Decimal(after))
    assert result == action
    assert actual == direction


def test_append_only_open_add_coalesced_and_risk_reduction_not_new_short(tmp_path) -> None:
    journal = OpportunityJournal(tmp_path/"opp.db")
    try:
        initial = journal.observe(trade(1,"0","1"))
        assert initial is not None
        assert initial.status is OpportunityStatus.ACTIVE
        assert initial.earliest_actionable_at == BASE + timedelta(seconds=3)
        assert journal.observe(trade(2,"1","1.4",at=BASE+timedelta(seconds=3))) is None
        red = journal.observe(trade(3,"1.4","0.4",at=BASE+timedelta(seconds=5)))
        assert red is not None and red.status is OpportunityStatus.NO_COPY
        assert red.family == CopyKind.DE_RISK.value
        assert red.copy_direction is None
        assert journal.observe(trade(3,"1.4","0.4",at=BASE+timedelta(seconds=5))) is None
        assert journal.stats()["opportunities"] == {"ENTER":1,"DE_RISK":1}
        original = journal.get(initial.opportunity_id)
        assert original is not None
        assert original["first_source_id"] == "event-1"
        assert len(original["updates"]) == 2
        assert original["first_known_at"] == (BASE + timedelta(seconds=1)).isoformat()
    finally:
        journal.close()


def test_short_covering_is_no_copy_and_flip_is_new_direction(tmp_path) -> None:
    j = OpportunityJournal(tmp_path/"opp.db")
    try:
        entry = j.observe(trade(1,"0","-2"))
        cover = j.observe(trade(2,"-2","-1",at=BASE+timedelta(minutes=1)))
        flip = j.observe(trade(3,"-1","1",at=BASE+timedelta(minutes=2)))
        assert entry is not None and entry.copy_direction == -1
        assert cover is not None and cover.copy_direction is None
        assert flip is not None and flip.copy_direction == 1
        assert flip.source_action == "FLIP_TO_LONG"
    finally:
        j.close()


def test_outage_does_not_convert_old_arrival_into_fresh_copy(tmp_path) -> None:
    j = OpportunityJournal(tmp_path/"opp.db")
    try:
        first = j.observe(
            trade(1,"0","1"), observation_at=BASE+timedelta(hours=2)
        )
        assert first is None
        assert j.stats()["exclusions"] == {"DELAYED_LEDGER_CONSUMPTION":1}
        assert j.cursor("ledger-1") == 1
    finally:
        j.close()


def test_partial_coverage_blocks_entry_but_preserves_reason(tmp_path) -> None:
    j = OpportunityJournal(tmp_path/"opp.db")
    try:
        assert j.observe(trade(1,"0","1",coverage="GAP")) is None
        assert j.stats()["exclusions"]["PARTIAL_SOURCE_COVERAGE"] == 1
    finally:
        j.close()


def test_unverified_coverage_is_not_new_predictive_evidence(tmp_path) -> None:
    journal = OpportunityJournal(tmp_path/"opp.db")
    try:
        assert journal.observe(trade(1,"0","1",coverage="UNSPECIFIED")) is None
        assert journal.stats()["exclusions"]["SOURCE_COVERAGE_UNVERIFIED"] == 1
        accepted=journal.observe(trade(2,"0","1",wallet="w2",coverage="Seeded"))
        assert accepted is not None and accepted.copy_direction == 1
        assert accepted.source_coverage == "Seeded"
    finally:
        journal.close()


def test_restart_reuses_cursor_and_identity(tmp_path) -> None:
    path=tmp_path/"opp.db"
    j=OpportunityJournal(path)
    first=j.observe(trade(1,"0","1"))
    j.close()
    j=OpportunityJournal(path)
    try:
        assert j.cursor("ledger-1")==1
        assert j.observe(trade(1,"0","1")) is None
        second=j.observe(trade(2,"1","3",at=BASE+timedelta(hours=1)))
        assert second is not None and first is not None
        assert second.opportunity_id != first.opportunity_id
    finally:
        j.close()


def test_late_exit_invalidates_all_open_lifecycles_without_rewriting_first_decisions(tmp_path) -> None:
    p = tmp_path / "opp.db"
    j = OpportunityJournal(p)
    try:
        at = BASE + timedelta(hours=1)
        first = j.observe(trade(1, "0", "1", at=at))
        assert first is not None
        second = j.observe(trade(2, "1", "2", at=at + timedelta(minutes=11)))
        assert second is not None and second.opportunity_id != first.opportunity_id
        first_before = j.db.execute(
            "SELECT payload_json FROM opportunity_decisions WHERE opportunity_id=?",
            (first.opportunity_id,),
        ).fetchone()[0]
        assert j.observe(trade(3, "2", "0", at=at + timedelta(minutes=25))) is not None
        for decision in (first, second):
            latest = j.get(decision.opportunity_id)
            assert latest is not None
            assert latest["current_status"] == "INVALIDATED"
            assert latest["latest_update"]["reason"] == "SOURCE_POSITION_CLOSED_OR_FLIPPED"
            assert latest["status"] == "ACTIVE"  # Immutable as-known first decision
        assert j.db.execute(
            "SELECT payload_json FROM opportunity_decisions WHERE opportunity_id=?",
            (first.opportunity_id,),
        ).fetchone()[0] == first_before
        assert len(j.get(first.opportunity_id)["updates"]) == 2
    finally:
        j.close()


def test_policy_upgrade_does_not_leave_legacy_entry_open_after_exit(tmp_path) -> None:
    from ensemble.copy_opportunities import OpportunityPolicy

    p = tmp_path / "opp.db"
    old = OpportunityJournal(p, policy=OpportunityPolicy(revision="DI2-shadow-v1"))
    first = old.observe(trade(1, "0", "1"))
    assert first is not None
    old.close()
    updated = OpportunityJournal(p)
    try:
        assert updated.observe(
            trade(2, "1", "0", at=BASE + timedelta(minutes=30))
        ) is not None
        entry = updated.get(first.opportunity_id)
        assert entry is not None and entry["current_status"] == "INVALIDATED"
        assert entry["revision"] == "DI2-shadow-v1"
    finally:
        updated.close()


def test_gap_rejected_and_does_not_advance_cursor(tmp_path) -> None:
    j=OpportunityJournal(tmp_path/"opp.db")
    try:
        j.observe(trade(1,"0","1"))
        with pytest.raises(ValueError,match="ledger gap"):
            j.observe(trade(3,"1","2",at=BASE+timedelta(seconds=5)))
        assert j.cursor("ledger-1")==1
    finally:
        j.close()


def test_source_transition_validation_catches_delta_conflict() -> None:
    ledger={
        "kind":"POSITION_CHANGE","event_id":"x","wallet_id":"W1",
        "instrument_id":COIN,"quality":"valid","seq":9,
        "event_time":BASE.isoformat(),
        "known_at":(BASE+timedelta(seconds=1)).isoformat(),
        "payload":json.dumps({"transition":"OPEN","quantity_after":"2",
                              "delta":"-1","px":"100"}),
    }
    with pytest.raises(ValueError,match="disagrees"):
        TradeTransition.from_ledger(ledger,ledger_id="ledger-1")
    ledger["payload"]=json.dumps({"transition":"ADD","quantity_after":"2",
                                  "delta":"1","px":"100"})
    result=TradeTransition.from_ledger(ledger,ledger_id="ledger-1")
    assert result is not None and result.quantity_before==Decimal(1)
