"""Migration health describes price-only stores without inventing signal cuts."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from ensemble.copy_health import inspect_integrity
from ensemble.price_checkpoints import PriceCheckpointStore


def test_price_only_health_keeps_alpha_blocked_and_distinguishes_archives(tmp_path):
    ledger=tmp_path/"ledger.db"
    mids=tmp_path/"market-reference.db"
    health=tmp_path/"health.json"
    now=datetime.now(UTC)
    with closing(sqlite3.connect(ledger)) as db,db:
        db.execute("CREATE TABLE ledger_meta(ledger_id TEXT,current_seq INTEGER,retention_floor INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE outbox_events(seq INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO ledger_meta(ledger_id,current_seq) VALUES ('l',100)")
    store=PriceCheckpointStore(mids)
    store.close()
    health.write_text(json.dumps({
        "timestamp":now.isoformat(),"research":{
            "status":"EVENT_DRIVEN_PRIMARY_REFERENCE_ONLY_NOT_ER4_QUALIFIED",
            "mode":"price_checkpoints_only",
            "legacy_consensus_cuts_enabled":False,
        },
    }))
    report=inspect_integrity(
        ledger_db=ledger,research_db=mids,health_path=health,now=now
    )
    assert report["scoring"]["status"]=="EVENT_DRIVEN_PRICE_CHECKPOINTS_NO_LEGACY_CUTS"
    assert report["scoring"]["legacy_per_minute_scoring_enabled"] is False
    assert report["research"]["qualification"].startswith("EVENT_DRIVEN")
    assert "PREDICTIVE_ALPHA_NOT_QUALIFIED" in report["blockers"]
