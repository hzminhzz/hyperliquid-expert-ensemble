"""Agent first-class inspect/explain uses actual frozen persisted opportunity identity."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from ensemble.copy_agent import explain_copy_decision, inspect_changes, inspect_wallet

AT=datetime(2026,10,13,11,tzinfo=UTC)


def create(path:Path)->None:
    with closing(sqlite3.connect(path)) as db,db:
        db.executescript("""
            CREATE TABLE opportunity_state(ledger_id TEXT,last_seq INTEGER);
            INSERT INTO opportunity_state VALUES ('ledger-1',4);
            CREATE TABLE opportunity_decisions(
                opportunity_id TEXT, wallet_id TEXT,instrument_id TEXT,
                family TEXT,first_known_at TEXT,first_ledger_seq INTEGER,
                source_action TEXT,copy_direction INTEGER,payload_json TEXT
            );
            CREATE TABLE opportunity_updates(
                opportunity_id TEXT,ledger_seq INTEGER,known_at TEXT,
                source_action TEXT,status TEXT,reason TEXT
            );
        """)
        db.execute("INSERT INTO opportunity_decisions VALUES (?,?,?,?,?,?,?,?,?)",(
            "copy:123","w1","hyperliquid:mainnet:perp:default:BTC",
            "ENTER",AT.isoformat(),4,"OPEN_LONG",1,
            json.dumps({"first_known_at":AT.isoformat(),"status":"ACTIVE","source":"e4"}),
        ))
        db.execute("INSERT INTO opportunity_updates VALUES (?,?,?,?,?,?)",(
            "copy:123",4,AT.isoformat(),"OPEN_LONG","ACTIVE",None
        ))


def test_explain_displays_latest_terminal_state_separately_from_original(tmp_path:Path)->None:
    from datetime import timedelta
    from decimal import Decimal

    from ensemble.copy_opportunities import OpportunityJournal, TradeTransition

    path=tmp_path/"lifecycle.db"
    j=OpportunityJournal(path)
    def event(seq:int,before:int,after:int,at:datetime)->TradeTransition:
        from ensemble.wallet_evidence import classify_intent

        return TradeTransition(
            source_id=f"event-{seq}",wallet_id="w1",
            instrument_id="hyperliquid:mainnet:perp:default:BTC",
            source_kind=classify_intent(Decimal(before),Decimal(after)).value,
            quantity_before=Decimal(before),quantity_after=Decimal(after),
            price=Decimal(100),event_at=at,known_at=at+timedelta(seconds=1),
            ledger_id="l",ledger_seq=seq,
        )
    try:
        original=j.observe(event(1,0,1,AT))
        assert original is not None
        j.observe(event(2,1,0,AT+timedelta(minutes=25)))
    finally:
        j.close()
    report=explain_copy_decision(
        journal_db=path,outcome_db=tmp_path/"missing.db",
        opportunity_id=original.opportunity_id,
    )
    assert report["immutable_as_known_decision"]["status"]=="ACTIVE"
    assert report["current_lifecycle_status"]=="INVALIDATED"
    assert report["latest_transition"]["reason"]=="SOURCE_POSITION_CLOSED_OR_FLIPPED"
    assert report["trade_entry_recommendation"] is None


def test_lookups_keep_immutable_cut_separate_from_unqualified_outcomes(tmp_path:Path)->None:
    db=tmp_path/"copy.db"
    create(db)
    info=explain_copy_decision(
        journal_db=db,outcome_db=tmp_path/"nonexistent.db",opportunity_id="copy:123",
    )
    assert info["status"]=="FOUND_SHADOW_ONLY"
    assert info["trade_entry_recommendation"] is None
    assert info["market_only_incremental_edge"]=="NOT_QUALIFIED"
    assert len(info["subsequent_source_transitions"])==1
    assert inspect_wallet(journal_db=db,wallet_id="W1")["profitability_qualified"] is False
    delta=inspect_changes(journal_db=db,since_seq=3)
    assert len(delta["changes"])==1
    assert delta["next_seq"]==4
    assert inspect_changes(journal_db=db,since_seq=4)["changes"]==[]
