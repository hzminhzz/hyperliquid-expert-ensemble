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
