"""Integration tests for bounded shadow consumer and CLI (DI2-DI4/DI6)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ensemble.copy_opportunities import OpportunityJournal, TradeTransition
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
                    "quantity_after":"1","px":"100","coverage":"Seeded"},T0+timedelta(seconds=3)),
            (3,"POSITION_CHANGE",{"transition":"ADD","delta":"0.25",
                    "quantity_after":"1.25","px":"101","coverage":"Seeded"},T0+timedelta(seconds=5)),
            (4,"POSITION_CHANGE",{"transition":"REDUCE","delta":"-1",
                    "quantity_after":"0.25","px":"102","coverage":"Seeded"},T0+timedelta(seconds=7)),
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


def test_collector_seals_durable_coverage_gap_without_relabeling_history(tmp_path) -> None:
    path=tmp_path/"ledger.db"
    ledger(path)
    with closing(sqlite3.connect(path)) as db,db:
        db.execute(
            "UPDATE outbox_events SET kind='COVERAGE_CHANGE',quality='degraded',"
            "payload=? WHERE seq=3",
            (json.dumps({"status":"DISCONNECTED_GAP_UNVERIFIED",
                         "history_during_gap":"UNVERIFIED"}),),
        )
    cfg=ShadowSettings(path,tmp_path/"copy.db",retrospective=True)
    consumer=ShadowConsumer(cfg)
    try:
        assert consumer.scan()["cursor"]==4
        assert consumer.journal.cursor("l")==4
        row=consumer.journal.db.execute(
            "SELECT ledger_seq,reason FROM source_coverage_barriers"
        ).fetchone()
        assert tuple(row)==(3,"DISCONNECTED_GAP_UNVERIFIED")
        assert consumer.scan()["processed"]==0
        assert consumer.journal.stats()["opportunities"]["ENTER"]==1
    finally:
        consumer.close()


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
    assert result["no_copy"]==0  # Non-entry opportunities remain in journal, not 5 label rows.
    with closing(sqlite3.connect(cfg.outcome_db)) as db:
        outcomes=[json.loads(r[0]) for r in db.execute(
            "SELECT value_json FROM copy_markouts"
        ).fetchall()]
    assert any(o["status"]=="VALID_HYPOTHETICAL" for o in outcomes)
    assert all(o["price_model"]=="MID_TO_MID_HYPOTHETICAL_NOT_EXECUTABLE"
               for o in outcomes)


def test_mature_short_horizons_do_not_starve_behind_pending_24h(tmp_path) -> None:
    prices=tmp_path/"prices.db"
    outcomes=tmp_path/"results.db"
    cfg=ShadowSettings(
        tmp_path/"unused-ledger.db", tmp_path/"copy.db",prices,outcomes,
    )
    journal=OpportunityJournal(cfg.journal_db)
    decisions=[]
    try:
        for seq,hours in ((1,0),(2,0),(3,0),(4,2)):
            known_at=T0+timedelta(hours=hours,seconds=1)
            decision=journal.observe(TradeTransition(
                source_id=f"e{seq}",wallet_id=f"w{seq}",
                instrument_id=BTC,source_kind="OPEN",
                quantity_before=Decimal(0),quantity_after=Decimal(1),
                price=Decimal(100),event_at=known_at,
                known_at=known_at,ledger_id="l",ledger_seq=seq,
            ),observation_at=known_at)
            assert decision is not None
            decisions.append(decision)
    finally:
        journal.close()

    with closing(sqlite3.connect(prices)) as db, db:
        db.execute("""
            CREATE TABLE reference_mids(
                instrument_id TEXT,minute_utc TEXT,known_at TEXT,mid TEXT,
                PRIMARY KEY(instrument_id,minute_utc)
            )""")
        for minute in range(181):
            at=T0+timedelta(minutes=minute,seconds=15)
            db.execute("INSERT INTO reference_mids VALUES (?,?,?,?)",
                (BTC,at.replace(second=0).isoformat(),
                 at.isoformat(),str(100+minute/1000)))

    now=T0+timedelta(hours=3)
    result=evaluate(cfg,now=now,max_opportunities=2)
    assert result["inspected"]==2
    with closing(sqlite3.connect(outcomes)) as db:
        recent_rows=db.execute(
            "SELECT horizon,status FROM copy_markouts WHERE opportunity_id=?",
            (decisions[-1].opportunity_id,),
        ).fetchall()
        oldest_rows=db.execute(
            "SELECT horizon,status FROM copy_markouts WHERE opportunity_id=?",
            (decisions[0].opportunity_id,),
        ).fetchall()
    assert ("5m","VALID_HYPOTHETICAL") in recent_rows
    assert ("15m","VALID_HYPOTHETICAL") in recent_rows
    assert ("5m","VALID_HYPOTHETICAL") in oldest_rows
    assert all(horizon!="24h" for horizon,_ in recent_rows)



def test_bounded_evaluator_drains_old_work_while_servicing_new_work(tmp_path) -> None:
    cfg=ShadowSettings(
        tmp_path/"unused.db",tmp_path/"opportunities.db",
        tmp_path/"prices.db",tmp_path/"outcomes.db",
    )
    journal=OpportunityJournal(cfg.journal_db)
    decisions=[]
    try:
        for seq in range(1,65):
            known=T0+timedelta(hours=2 if seq>60 else 0,seconds=seq)
            decision=journal.observe(TradeTransition(
                source_id=f"load-{seq}",wallet_id=f"wallet-{seq}",
                instrument_id=BTC,source_kind="OPEN",
                quantity_before=Decimal(0),quantity_after=Decimal(1),
                price=Decimal(100),event_at=known,known_at=known,
                ledger_id="load-ledger",ledger_seq=seq,
            ),observation_at=known)
            assert decision is not None
            decisions.append(decision)
    finally:
        journal.close()
    with closing(sqlite3.connect(cfg.price_db)) as db,db:
        db.execute("""
            CREATE TABLE reference_mids(instrument_id TEXT,minute_utc TEXT,
            known_at TEXT,mid TEXT,PRIMARY KEY(instrument_id,minute_utc))""")
        for m in range(200):
            at=T0+timedelta(minutes=m,seconds=15)
            db.execute("INSERT INTO reference_mids VALUES (?,?,?,?)",(
                BTC,at.replace(second=0).isoformat(),
                at.isoformat(),str(100+m/1000),
            ))
    now=T0+timedelta(hours=3,minutes=10)
    first=evaluate(cfg,now=now,max_opportunities=6)
    assert first["inspected"]==6
    with closing(sqlite3.connect(cfg.outcome_db)) as db:
        assert db.execute(
            "SELECT count(*) FROM copy_markouts WHERE horizon='5m'"
        ).fetchone()[0]>=2
    for _ in range(20):
        evaluate(cfg,now=now,max_opportunities=6)
    with closing(sqlite3.connect(cfg.outcome_db)) as db:
        assert db.execute(
            "SELECT count(*) FROM copy_markouts WHERE horizon='5m'"
        ).fetchone()[0]==len(decisions)
        assert db.execute(
            "SELECT count(*) FROM copy_markouts WHERE horizon='24h'"
        ).fetchone()[0]==0


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
        assert all(item["revision"]=="DI2-shadow-v2" for item in recent)
    finally:
        runner.close()
