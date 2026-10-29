"""Production descriptive digest is bounded, idempotent and non-predictive."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from ensemble.copy_digest import publish_digest

AT=datetime(2026,10,13,10,tzinfo=UTC)


def fixtures(tmp_path:Path)->tuple[Path,Path,Path]:
    source=tmp_path/"source.db"
    journal=tmp_path/"journal.db"
    delivery=tmp_path/"delivery.db"
    with closing(sqlite3.connect(source)) as db,db:
        db.execute("CREATE TABLE ledger_meta(current_seq INTEGER)")
        db.execute("INSERT INTO ledger_meta VALUES (5)")
        db.execute("CREATE TABLE outbox_events(seq INTEGER,known_at TEXT)")
        db.execute("INSERT INTO outbox_events VALUES (?,?)",(5,AT.isoformat()))
    with closing(sqlite3.connect(journal)) as db,db:
        db.execute("CREATE TABLE opportunity_state(last_seq INTEGER)")
        db.execute("INSERT INTO opportunity_state VALUES (5)")
        db.execute(
            "CREATE TABLE opportunity_decisions("
            "opportunity_id TEXT,instrument_id TEXT,wallet_id TEXT,"
            "first_known_at TEXT,family TEXT,source_action TEXT,"
            "copy_direction INTEGER,blocker TEXT)"
        )
        db.execute("INSERT INTO opportunity_decisions VALUES (?,?,?,?,?,?,?,?)",(
            "id1","hyperliquid:mainnet:perp:default:BTC",
            "wallet1",AT.isoformat(),"ENTER","OPEN_LONG",1,None,
        ))
    return source,journal,delivery


def test_digest_idempotence_and_no_trading_recommendation(tmp_path:Path)->None:
    source,journal,delivery=fixtures(tmp_path)
    calls=[]
    def accept(token:str,chat_id:str,message:str)->bool:
        calls.append(message)
        return True
    opts={"journal_db":journal,"state_db":delivery,"source_db":source,
          "now":AT,"token":"dummy","chat_id":"somechat","send":accept}
    result=publish_digest(**opts)
    assert result["status"]=="DELIVERED_DESCRIPTIVE_ONLY"
    assert publish_digest(**opts)["status"]=="ALREADY_DELIVERED"
    assert len(calls)==1
    assert "NOT a copy/entry signal" in calls[0]
    assert "Entry price/size: NOT AVAILABLE" in calls[0]


def test_disk_pressure_warns_without_presenting_trading_advice(tmp_path:Path,monkeypatch)->None:
    source,journal,delivery=fixtures(tmp_path)
    monkeypatch.setattr(
        "ensemble.copy_digest.shutil.disk_usage",
        lambda _: SimpleNamespace(total=100,used=90,free=10),
    )
    sent=[]
    result=publish_digest(
        journal_db=journal,state_db=delivery,source_db=source,now=AT,
        token="dummy",chat_id="somechat",
        send=lambda _token,_chat,message: sent.append(message) or True,
    )
    assert result["status"]=="DELIVERED_DESCRIPTIVE_ONLY"
    assert len(sent)==1
    assert "OPS WARNING: VPS disk 90% used" in sent[0]
    assert "NOT a copy/entry signal" in sent[0]


def test_failed_delivery_is_retriable_and_observation_staleness_blocks(tmp_path:Path)->None:
    source,journal,delivery=fixtures(tmp_path)
    opts={"journal_db":journal,"state_db":delivery,"source_db":source,
          "now":AT,"token":"dummy","chat_id":"somechat"}
    assert publish_digest(**opts,send=lambda *_:False)["status"]=="DELIVERY_NOT_ACKNOWLEDGED"
    assert publish_digest(**opts,send=lambda *_:True)["status"]=="DELIVERED_DESCRIPTIVE_ONLY"
    with closing(sqlite3.connect(source)) as db,db:
        db.execute("UPDATE ledger_meta SET current_seq=1000")
    assert publish_digest(**opts,send=lambda *_:True)["status"]=="BLOCKED_SHADOW_CURSOR_STALE"
