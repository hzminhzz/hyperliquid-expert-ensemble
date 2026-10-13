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


def test_board_respects_as_known_cut_and_reports_truncation(tmp_path:Path)->None:
    p=tmp_path/"copy.db"
    make_db(p)
    shortened=build_trade_flow_board(journal_db=p,now=NOW,max_source_rows=2)
    assert shortened["truncated"]
    prior=build_trade_flow_board(journal_db=p,now=NOW-timedelta(days=1))
    assert prior["observed_opportunities"]==0
