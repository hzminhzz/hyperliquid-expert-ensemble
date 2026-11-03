"""DI7: price-only migration retains historical quotes and blocks old cuts."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ensemble.price_checkpoints import PriceCheckpointStore, backfill_known_prices
from ensemble.runtime import EnsembleRuntime, RuntimeSettings

NOW=datetime.now(UTC).replace(second=0,microsecond=0)
BTC="hyperliquid:mainnet:perp:default:BTC"
HIP="hyperliquid:mainnet:perp:hip3:BTC"


def test_price_store_writes_only_stamped_supported_prices_and_is_restart_safe(tmp_path):
    path=tmp_path/"prices.db"
    store=PriceCheckpointStore(path)
    try:
        accepted=store.capture(
            known_at=NOW,
            prices={
                BTC:(Decimal(100),NOW-timedelta(seconds=3)),
                HIP:(Decimal(5),NOW-timedelta(seconds=3)),
                "hyperliquid:mainnet:perp:default:ETH":(
                    Decimal(5),NOW-timedelta(minutes=10)),
            },
        )
        assert accepted==1
        assert store.capture(
            known_at=NOW,prices={BTC:(Decimal(101),NOW)}
        )==0
        assert store.stats()["price_rows"]==1
        with closing(sqlite3.connect(path)) as conn:
            row=conn.execute(
                "SELECT instrument_id,mid,known_at FROM reference_mids"
            ).fetchone()
            assert row==(BTC,"100",(NOW-timedelta(seconds=3)).isoformat())
            assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='forward_outcomes'").fetchone() is None
    finally:
        store.close()


def test_backfill_carries_historical_knowledge_time_unchanged(tmp_path):
    old=tmp_path/"old.db"
    new=tmp_path/"new.db"
    with closing(sqlite3.connect(old)) as conn,conn:
        conn.execute(
            "CREATE TABLE reference_mids(instrument_id TEXT,minute_utc TEXT,"
            "known_at TEXT,mid TEXT,source TEXT)"
        )
        conn.execute("INSERT INTO reference_mids VALUES (?,?,?,?,?)",(
            BTC,(NOW-timedelta(minutes=15)).isoformat(),
            (NOW-timedelta(minutes=15)).isoformat(),"100",
            "hyperliquid_info_allMids_received_at",
        ))
    result=backfill_known_prices(
        old_db=old,new_db=new,cutoff=NOW-timedelta(hours=1))
    assert result["inserted"]==1
    assert backfill_known_prices(
        old_db=old,new_db=new,cutoff=NOW-timedelta(hours=1)
    )["inserted"]==0
    with closing(sqlite3.connect(new)) as conn:
        assert conn.execute("SELECT known_at FROM reference_mids").fetchone()[0]==(
            NOW-timedelta(minutes=15)).isoformat()


def test_runtime_price_only_mode_captures_no_legacy_signals(monkeypatch,tmp_path):
    now=datetime.now(UTC)
    settings=RuntimeSettings(
        ledger_db=tmp_path/"ledger.db",
        projection_db=tmp_path/"projection.db",
        health_path=tmp_path/"health.json",
        research_db=tmp_path/"legacy-unused.db",
        price_checkpoint_db=tmp_path/"market-reference.db",
        research_mode="price_only",
        telegram_chat_id_path=tmp_path/"none",
        experts=["0x"+"11"*20],
    )
    runtime=EnsembleRuntime(settings)
    try:
        runtime.mids={"BTC":Decimal(100)}
        runtime.market_known_at=now
        runtime.capture_research({BTC:object(),HIP:object()})
        assert runtime.recorder is None
        assert runtime.price_recorder is not None
        assert runtime.price_recorder.stats()["price_rows"]==1
        assert not settings.research_db.exists()
        assert runtime.research_stats is not None
        assert runtime.research_stats["legacy_forward_outcomes_enabled"] is False
    finally:
        runtime.close()
