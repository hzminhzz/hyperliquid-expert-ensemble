"""Adversarial source-integrity regressions: never turn missing data into exits."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ensemble.projection import ProjectionStore

STAMP = datetime(2026, 10, 29, tzinfo=UTC).isoformat()
WALLET = "0xabc"
BTC = "hyperliquid:mainnet:perp:default:BTC"


def event(seq: int, kind: str, payload: dict | None, ledger: str = "ledger-one") -> dict:
    return {
        "cursor": {"ledger_id": ledger, "seq": seq},
        "kind": kind,
        "wallet_id": WALLET,
        "instrument_id": BTC,
        "event_time": STAMP,
        "known_at": STAMP,
        "after_revision": seq,
        "payload": payload,
    }


def snapshot(qty: str = "1") -> dict:
    return {"positions": [{"instrument_id": BTC, "quantity_after": qty,
                            "avg_entry": "100", "coverage": "Seeded"}],
            "equity": {"account_value": "10000", "observed_at": STAMP}}


def test_bad_snapshot_never_erases_state_or_advances_offset(tmp_path: Path) -> None:
    with_store = ProjectionStore(tmp_path / "projection.db")
    try:
        assert with_store.apply_canonical_events("ledger-one", [event(1, "ACCOUNT_SNAPSHOT", snapshot())]) == 1
        assert with_store.get_position(WALLET, BTC) is not None
        for bad in [None, {}, {"positions": None}, {"positions": "invalid"},
                    {"positions": [None]}, {"positions": [{}]}]:
            with pytest.raises((ValueError, TypeError)):
                with_store.apply_canonical_events("ledger-one", [event(2, "ACCOUNT_SNAPSHOT", bad)])
            assert with_store.get_offset("ledger-one") == 1
            assert with_store.get_position(WALLET, BTC).szi == 1
    finally:
        with_store.close()


def test_gapped_batch_rolls_back_all_projected_changes(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path / "projection.db")
    try:
        store.apply_canonical_events("ledger-one", [event(1, "ACCOUNT_SNAPSHOT", snapshot())])
        with pytest.raises(ValueError, match="gap"):
            store.apply_canonical_events("ledger-one", [
                event(2, "POSITION_CHANGE", {"transition": "ADD", "quantity_after": "2", "px": "100"}),
                event(4, "POSITION_CHANGE", {"transition": "CLOSE", "quantity_after": "0"}),
            ])
        assert store.get_offset("ledger-one") == 1
        assert store.get_position(WALLET, BTC).szi == 1
    finally:
        store.close()


def ledger(path: Path, *, current_seq: int, floor: int, payload: str) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE ledger_meta(ledger_id TEXT,current_seq INTEGER,retention_floor INTEGER);
            CREATE TABLE outbox_events(
              seq INTEGER PRIMARY KEY,event_id TEXT,schema_version TEXT,ledger_id TEXT,
              kind TEXT,instrument_id TEXT,wallet_id TEXT,event_time TEXT,
              received_at TEXT,known_at TEXT,before_revision INTEGER,
              after_revision INTEGER,payload TEXT,quality TEXT
            );
        """)
        db.execute("INSERT INTO ledger_meta VALUES (?,?,?)", ("ledger-one", current_seq, floor))
        db.execute("INSERT INTO outbox_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, "e1", "1", "ledger-one", "ACCOUNT_SNAPSHOT", BTC, WALLET,
             STAMP, STAMP, STAMP, 0, 1, payload, "valid"))


def test_invalid_ledger_json_does_not_advance_offset(tmp_path: Path) -> None:
    source = tmp_path / "observation.db"
    ledger(source, current_seq=1, floor=0, payload="{broken")
    store = ProjectionStore(tmp_path / "projection.db")
    try:
        with pytest.raises(ValueError, match="malformed source payload"):
            store.consume_from_ledger_db(source)
        assert store.get_offset("ledger-one") == 0
    finally:
        store.close()


def test_feed_gap_invalidates_projected_coverage_until_valid_snapshot(tmp_path: Path) -> None:
    store=ProjectionStore(tmp_path/"projection.db")
    try:
        store.apply_canonical_events("ledger-one", [
            event(1, "ACCOUNT_SNAPSHOT", snapshot()),
            event(2, "COVERAGE_CHANGE", {"status":"DISCONNECTED_GAP_UNVERIFIED",
                                         "history_during_gap":"UNVERIFIED"}),
        ])
        degraded=store.get_position(WALLET,BTC)
        assert degraded is not None and degraded.coverage=="Degraded"
        assert store.last_feed_gap_at()==datetime.fromisoformat(STAMP)
        store.apply_canonical_events("ledger-one", [
            event(3, "COVERAGE_CHANGE", {"status":"SUBSCRIBED_HISTORY_UNVERIFIED"})
        ])
        assert store.get_position(WALLET,BTC).coverage=="Degraded"
        store.apply_canonical_events("ledger-one", [
            event(4, "ACCOUNT_SNAPSHOT", snapshot()),
        ])
        assert store.get_position(WALLET,BTC).coverage=="Seeded"
    finally:
        store.close()


def test_expired_source_cursor_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "observation.db"
    ledger(source, current_seq=1, floor=1, payload=json.dumps(snapshot()))
    store = ProjectionStore(tmp_path / "projection.db")
    try:
        with pytest.raises(ValueError, match="expired"):
            store.consume_from_ledger_db(source)
        assert store.get_offset("ledger-one") == 0
    finally:
        store.close()
