"""Copy flow is descriptive, deduplicated per wallet and time-cut."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ensemble.copy_board import build_trade_flow_board, render_flow_digest

NOW=datetime(2026,10,13,11,tzinfo=UTC)


def make_db(path:Path)->None:
    with closing(sqlite3.connect(path)) as db,db:
        db.execute(
            "CREATE TABLE opportunity_decisions("
            "opportunity_id TEXT,instrument_id TEXT,wallet_id TEXT,"
            "first_known_at TEXT,family TEXT,source_action TEXT,"
            "copy_direction INTEGER,blocker TEXT)"
        )
        for index,(coin,wallet,direction,family) in enumerate([
            ("BTC","wallet1",1,"ENTER"),
            ("BTC","wallet1",1,"INCREASE"),
            ("BTC","wallet2",-1,"ENTER"),
            ("BTC","wallet3",None,"DE_RISK"),
            ("ETH","wallet4",1,"ENTER"),
        ]):
            db.execute("INSERT INTO opportunity_decisions VALUES (?,?,?,?,?,?,?,?)",(
                f"o{index}",f"hyperliquid:mainnet:perp:default:{coin}",wallet,
                (NOW-timedelta(minutes=10+index)).isoformat(),family,
                "OPEN_LONG",direction,None,
            ))


def test_board_dedup_addresses_and_never_confuses_de_risk_for_short(tmp_path:Path)->None:
    p=tmp_path/"copy.db"
    make_db(p)
    result=build_trade_flow_board(journal_db=p,now=NOW)
    assert result["financial_execution"] is False
    assert result["predictive_qualified"] is False
    btc=next(x for x in result["coins"] if x["coin"]=="BTC")
    assert btc["long_entry_wallets"]==1
    assert btc["short_entry_wallets"]==1
    assert btc["de_risk_wallets"]==1
    assert btc["independent_cluster_count"] is None
    assert btc["directional_trading_signal"] is None
    assert "NOT a copy/entry signal" in render_flow_digest(result)
    assert "Entry price/size: NOT AVAILABLE" in render_flow_digest(result)
    assert btc["observed_directional_bias"] == "MIXED"


def test_hourly_long_short_watch_deduplicates_last_direction(tmp_path:Path)->None:
    p=tmp_path/"copy.db"
    make_db(p)
    with closing(sqlite3.connect(p)) as db,db:
        rows=[
            ("SOL","w1",-1,30),  # Older opposite direction must not count.
            ("SOL","w1",1,4),
            ("SOL","w2",1,7),
            ("SOL","w3",1,8),
            ("ETH","w5",-1,6),
            ("ETH","w6",-1,9),
            ("ETH","w7",-1,10),
        ]
        for i,(coin,wallet,direction,minutes_ago) in enumerate(rows):
            db.execute("INSERT INTO opportunity_decisions VALUES (?,?,?,?,?,?,?,?)",(
                f"extra{i}",f"hyperliquid:mainnet:perp:default:{coin}",
                wallet,(NOW-timedelta(minutes=minutes_ago)).isoformat(),
                "ENTER","OPEN_LONG" if direction==1 else "OPEN_SHORT",
                direction,None,
            ))
    board=build_trade_flow_board(journal_db=p,now=NOW,hours=1,max_coins=20)
    sol=next(c for c in board["coins"] if c["coin"]=="SOL")
    eth=next(c for c in board["coins"] if c["coin"]=="ETH")
    assert sol["long_entry_wallets"]==3
    assert sol["short_entry_wallets"]==0
    assert sol["observed_directional_bias"]=="LONG_WATCH"
    assert eth["observed_directional_bias"]=="SHORT_WATCH"
    assert eth["long_entry_wallets"]==1
    text=render_flow_digest(board)
    assert "LONG WATCH" in text and "SHORT WATCH" in text
    assert "MIXED" in text and "SOL" in text and "ETH" in text
    assert "NOT QUALIFIED" in text and "Entry price/size: NOT AVAILABLE" in text


def test_exit_pressure_downgrades_long_bias_to_caution(tmp_path:Path)->None:
    p=tmp_path/"copy.db"
    make_db(p)
    with closing(sqlite3.connect(p)) as db,db:
        for i in range(3):
            db.execute("INSERT INTO opportunity_decisions VALUES (?,?,?,?,?,?,?,?)",(
                f"buy{i}","hyperliquid:mainnet:perp:default:NEAR",
                f"long{i}",(NOW-timedelta(minutes=i+3)).isoformat(),
                "ENTER","OPEN_LONG",1,None,
            ))
            db.execute("INSERT INTO opportunity_decisions VALUES (?,?,?,?,?,?,?,?)",(
                f"exit{i}","hyperliquid:mainnet:perp:default:NEAR",
                f"exit{i}",(NOW-timedelta(minutes=i+5)).isoformat(),
                "EXIT","CLOSE",None,None,
            ))
    board=build_trade_flow_board(journal_db=p,now=NOW,hours=4)
    near=next(c for c in board["coins"] if c["coin"]=="NEAR")
    assert near["underlying_lean"]=="LONG_WATCH"
    assert near["observed_directional_bias"]=="CAUTION"
    assert near["risk_reduction_caution"] is True
    text=render_flow_digest(board)
    assert "NEAR (long-leaning)" in text
    assert "NEAR" not in text.split("SHORT WATCH")[0]


def test_two_wallets_never_become_a_directional_watch(tmp_path:Path)->None:
    p=tmp_path/"copy.db"
    make_db(p)
    with closing(sqlite3.connect(p)) as db,db:
        for i in range(2):
            db.execute("INSERT INTO opportunity_decisions VALUES (?,?,?,?,?,?,?,?)",(
                f"x{i}","hyperliquid:mainnet:perp:default:LINK",
                f"w{i}",(NOW-timedelta(minutes=6)).isoformat(),
                "ENTER","OPEN_SHORT",-1,None,
            ))
    board=build_trade_flow_board(journal_db=p,now=NOW,hours=1)
    link=next(c for c in board["coins"] if c["coin"]=="LINK")
    assert link["observed_directional_bias"]=="INSUFFICIENT_SUPPORT"
    assert "LINK" not in render_flow_digest(board)


def test_board_uses_latest_as_known_lifecycle_not_initial_entry_status(tmp_path:Path)->None:
    from decimal import Decimal

    from ensemble.copy_opportunities import OpportunityJournal, TradeTransition

    path = tmp_path / "lifecycle.db"
    j = OpportunityJournal(path)
    t0 = NOW - timedelta(minutes=50)
    instrument = "hyperliquid:mainnet:perp:default:SOL"

    def change(seq:int, wallet:str, before:str, after:str, at:datetime)->TradeTransition:
        from ensemble.wallet_evidence import classify_intent

        return TradeTransition(
            source_id=f"scope-{seq}", wallet_id=wallet, instrument_id=instrument,
            source_kind=classify_intent(Decimal(before), Decimal(after)).value,
            quantity_before=Decimal(before), quantity_after=Decimal(after),
            price=Decimal(100), event_at=at,
            known_at=at + timedelta(seconds=1), ledger_id="ledger-board",
            ledger_seq=seq,
        )

    try:
        for seq, wallet in enumerate(("w1", "w2", "w3"), start=1):
            assert j.observe(change(seq, wallet, "0", "1", t0)) is not None
        assert j.observe(change(4,"w1","1","2",t0+timedelta(minutes=11))) is not None
        before = build_trade_flow_board(
            journal_db=path, now=t0+timedelta(minutes=20), hours=1
        )
        assert next(c for c in before["coins"] if c["coin"]=="SOL")["observed_directional_bias"] == "LONG_WATCH"
        assert j.observe(change(5,"w1","2","0",t0+timedelta(minutes=25))) is not None
        after = build_trade_flow_board(journal_db=path, now=NOW, hours=1)
        sol = next(c for c in after["coins"] if c["coin"]=="SOL")
        assert sol["long_entry_wallets"] == 2
        assert sol["observed_directional_bias"] != "LONG_WATCH"
    finally:
        j.close()


def test_durable_source_gap_hides_prior_entry_votes_without_fabricating_exits(tmp_path:Path)->None:
    from decimal import Decimal

    from ensemble.copy_opportunities import OpportunityJournal, TradeTransition

    p=tmp_path/"gap.db"
    j=OpportunityJournal(p)
    at=NOW-timedelta(minutes=45)
    instrument="hyperliquid:mainnet:perp:default:BTC"
    try:
        for seq,wallet in enumerate(("w1","w2","w3"),start=1):
            entry=TradeTransition(
                source_id=f"entry-{seq}", wallet_id=wallet, instrument_id=instrument,
                source_kind="OPEN", quantity_before=Decimal(0),
                quantity_after=Decimal(1), price=Decimal(100),
                event_at=at, known_at=at+timedelta(seconds=1),
                ledger_id="ledger-gap",ledger_seq=seq,
            )
            assert j.observe(entry) is not None
        before=build_trade_flow_board(journal_db=p,now=at+timedelta(minutes=10),hours=1)
        assert before["coins"][0]["observed_directional_bias"]=="LONG_WATCH"
        j.record_source_gap(
            ledger_id="ledger-gap",ledger_seq=4,
            source_id="gap-4",known_at=at+timedelta(minutes=20),
            reason="DISCONNECTED_GAP_UNVERIFIED",
        )
        after=build_trade_flow_board(journal_db=p,now=NOW,hours=1)
        assert after["fresh_entry_events"]==0
        assert after["coverage_gap_boundary"] is not None
        assert j.get("copy:nonexistent") is None
        assert j.stats()["opportunities"]=={"ENTER":3}
        earlier=build_trade_flow_board(journal_db=p,now=at+timedelta(minutes=10),hours=1)
        assert earlier["coins"][0]["observed_directional_bias"]=="LONG_WATCH"
    finally:
        j.close()


def test_board_respects_as_known_cut_and_reports_truncation(tmp_path:Path)->None:
    p=tmp_path/"copy.db"
    make_db(p)
    shortened=build_trade_flow_board(journal_db=p,now=NOW,max_source_rows=2)
    assert shortened["truncated"]
    prior=build_trade_flow_board(journal_db=p,now=NOW-timedelta(days=1))
    assert prior["observed_opportunities"]==0
