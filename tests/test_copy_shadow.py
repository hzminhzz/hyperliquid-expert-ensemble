"""Integration tests for bounded shadow consumer and CLI (DI2-DI4/DI6)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from ensemble.copy_shadow import ShadowConsumer, ShadowSettings, evaluate

T0=datetime(2026,10,13,0,tzinfo=UTC)
BTC="hyperliquid:mainnet:perp:default:BTC"


def ledger(path) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE ledger_meta(ledger_id TEXT,current_seq INTEGER);
            INSERT INTO ledger_meta VALUES ('l',0);
            CREATE TABLE outbox_events(
                seq INTEGER PRIMARY KEY,event_id TEXT,kind TEXT,
                instrument_id TEXT,wallet_id TEXT,event_time TEXT,
                known_at TEXT,quality TEXT,payload TEXT
            );
        """)
        events=[
            (1,"ACCOUNT_SNAPSHOT",{},T0),
            (2,"POSITION_CHANGE",{"transition":"OPEN","delta":"1",
                    "quantity_after":"1","px":"100"},T0+timedelta(seconds=3)),
            (3,"POSITION_CHANGE",{"transition":"ADD","delta":"0.25",
                    "quantity_after":"1.25","px":"101"},T0+timedelta(seconds=5)),
            (4,"POSITION_CHANGE",{"transition":"REDUCE","delta":"-1",
                    "quantity_after":"0.25","px":"102"},T0+timedelta(seconds=7)),
        ]
        for seq,kind,payload,t in events:
            db.execute("INSERT INTO outbox_events VALUES (?,?,?,?,?,?,?,?,?)",(
                seq,f"e{seq}",kind,BTC,"W1",t.isoformat(),
                (t+timedelta(milliseconds=500)).isoformat(),
                "valid",json.dumps(payload),
            ))
        db.execute("UPDATE ledger_meta SET current_seq=4")


def test_retro_replay_then_restart_is_idempotent(tmp_path) -> None:
    path=tmp_path/"ledger.db"
    ledger(path)
    cfg=ShadowSettings(path,tmp_path/"copy.db",batch_size=2,
                       max_batches=4,retrospective=True)
    runner=ShadowConsumer(cfg)
    try:
        first=runner.scan()
        assert first["cursor"]==4
        assert first["new_opportunities"]==2  # open + no-copy REDUCE
        assert first["nontrade_events"]==1
        assert runner.journal.stats()["opportunities"]=={"ENTER":1,"DE_RISK":1}
    finally:
        runner.close()
    restart=ShadowConsumer(cfg)
    try:
        assert restart.scan()["processed"]==0
    finally:
        restart.close()


def test_prospective_activation_never_relabels_old_history(tmp_path) -> None:
    path=tmp_path/"ledger.db"
    ledger(path)
    cfg=ShadowSettings(path,tmp_path/"copy.db")
    runner=ShadowConsumer(cfg)
    try:
        assert runner.scan()["processed"]==0
        assert runner.journal.cursor("l")==4
    finally:
        runner.close()


def test_priced_shadow_stores_hypothetical_and_censors_unsupported(tmp_path) -> None:
    path=tmp_path/"ledger.db"
    ledger(path)
    cfg=ShadowSettings(path,tmp_path/"copy.db",
        tmp_path/"prices.db",tmp_path/"results.db",
        retrospective=True,max_batches=2)
    consumer=ShadowConsumer(cfg)
    try:
        consumer.scan()
    finally:
        consumer.close()
    with closing(sqlite3.connect(cfg.price_db)) as db, db:
        db.execute("""
            CREATE TABLE reference_mids(
                instrument_id TEXT,minute_utc TEXT,known_at TEXT,mid TEXT,
                PRIMARY KEY(instrument_id,minute_utc)
            )""")
        # The first minute mid is after 2-second action latency.
        for n in range(7):
            at=T0+timedelta(minutes=n,seconds=15)
            db.execute("INSERT INTO reference_mids VALUES (?,?,?,?)",
                       (BTC,at.replace(second=0).isoformat(),
                        at.isoformat(),str(100+n)))
    result=evaluate(cfg,now=T0+timedelta(hours=3),max_opportunities=3)
    assert result["valid_hypothetical"]>=1
    assert result["no_copy"]>=1
    with closing(sqlite3.connect(cfg.outcome_db)) as db:
        outcomes=[json.loads(r[0]) for r in db.execute(
            "SELECT value_json FROM copy_markouts"
        ).fetchall()]
    assert any(o["status"]=="VALID_HYPOTHETICAL" for o in outcomes)
    assert all(o["price_model"]=="MID_TO_MID_HYPOTHETICAL_NOT_EXECUTABLE"
               for o in outcomes)


def test_journal_prospectivity_mode_is_pinned_and_cannot_be_retroactively_relabelled(tmp_path) -> None:
    path=tmp_path/"ledger.db"
    ledger(path)
    live=ShadowSettings(path,tmp_path/"copy.db")
    recorder=ShadowConsumer(live)
    recorder.close()
    with pytest.raises(RuntimeError,match="mode mismatch"):
        ShadowConsumer(ShadowSettings(path,tmp_path/"copy.db",retrospective=True))


def test_copy_report_never_claims_executable_pnl(tmp_path) -> None:
    path=tmp_path/"ledger.db"
    ledger(path)
    cfg=ShadowSettings(path,tmp_path/"copy.db",retrospective=True)
    runner=ShadowConsumer(cfg)
    try:
        stats=runner.scan()
        assert not stats["financial_execution"]
        recent=runner.journal.recent()
        assert all(item["revision"]=="DI2-shadow-v1" for item in recent)
    finally:
        runner.close()
