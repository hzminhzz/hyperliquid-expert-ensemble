"""DI4 paired controls require independent time windows."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ensemble.copy_study import evaluate_incremental_edge


def test_two_wallets_same_coin_same_four_hour_shock_count_once(tmp_path:Path)->None:
    source=tmp_path/"s.db"
    outcomes=tmp_path/"o.db"
    base=datetime(2026,10,9,1,tzinfo=UTC)
    with closing(sqlite3.connect(source)) as db,db:
        db.execute("""CREATE TABLE opportunity_decisions(
            opportunity_id TEXT,instrument_id TEXT,family TEXT,
            first_known_at TEXT,wallet_id TEXT,first_ledger_seq INTEGER
        )""")
        for n in range(2):
            db.execute("INSERT INTO opportunity_decisions VALUES (?,?,?,?,?,?)",(
                f"o{n}","hyperliquid:mainnet:perp:default:BTC","ENTER",
                (base+timedelta(seconds=15*n)).isoformat(),f"w{n}",n+1,
            ))
    with closing(sqlite3.connect(outcomes)) as db,db:
        db.execute("""CREATE TABLE copy_markouts(
            opportunity_id TEXT,horizon TEXT,status TEXT,value_json TEXT
        )""")
        db.execute("""CREATE TABLE copy_controls(
            opportunity_id TEXT,horizon TEXT,control_revision TEXT,value_json TEXT
        )""")
        for n in range(2):
            db.execute("INSERT INTO copy_markouts VALUES (?,?,?,?)",(
                f"o{n}","1h","VALID_HYPOTHETICAL",
                json.dumps({"hypothetical_net_return":"0.01"}),
            ))
            db.execute("INSERT INTO copy_controls VALUES (?,?,?,?)",(
                f"o{n}","1h","control1",
                json.dumps({"market_only_net_return":"0.005"}),
            ))
    result=evaluate_incremental_edge(
        opportunity_db=source,outcome_db=outcomes,
        horizon="1h",control_revision="control1",
    )
    assert result["paired_events"]==2
    assert result["deduped_instrument_time_buckets"]==1
    assert result["observed_utc_days"]==1
    assert result["decision"]=="INCONCLUSIVE_INDEPENDENT_DAYS"
    assert result["mean_incremental_bps"]=="50.000"
