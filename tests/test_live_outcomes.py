"""Prospective source admission and immutable forward markout tests."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ensemble.live_outcomes import LiveOutcomeRecorder, RecorderSettings

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
INSTRUMENT = "hyperliquid:mainnet:perp:default:BTC"


class Clock:
    def __init__(self) -> None:
        self.at = T0

    def now(self) -> datetime:
        return self.at


def create_ledger(path: Path) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(
            """
            CREATE TABLE ledger_meta (
                ledger_id TEXT PRIMARY KEY, created_at TEXT,
                current_seq INTEGER, retention_floor INTEGER
            );
            INSERT INTO ledger_meta VALUES ('ledger-1','2026-10-08T12:00:00+00:00',0,0);
            CREATE TABLE outbox_events (
                seq INTEGER PRIMARY KEY, event_id TEXT, kind TEXT,
                instrument_id TEXT, wallet_id TEXT, event_time TEXT,
                known_at TEXT, quality TEXT, payload TEXT
            );
            """
        )
    conn.close()


def add_event(
    path: Path, clock: Clock, *,
    seq: int = 1,
    kind: str = "POSITION_CHANGE",
    instrument: str = INSTRUMENT,
    px: str = "100",
    delta: str = "0.5",
) -> None:
    conn = sqlite3.connect(path)
    at = clock.at.isoformat()
    with conn:
        conn.execute(
            """INSERT INTO outbox_events
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                seq, f"event-{seq}", kind, instrument, "0xwallet", at, at, "valid",
                json.dumps({"transition": "OPEN", "px": px, "delta": delta}),
            ),
        )
        conn.execute("UPDATE ledger_meta SET current_seq=?", (seq,))
    conn.close()


def collector(
    tmp_path: Path, clock: Clock, mids: dict[str, str] | None = None,
) -> LiveOutcomeRecorder:
    settings = RecorderSettings(
        ledger_db=tmp_path / "ledger.db",
        outcome_db=tmp_path / "outcomes.db",
        poll_seconds=5,
        latency_ms=2000,
        cost_bps=Decimal(10),
        price_lateness_s=25,
        source_delay_s=25,
    )
    return LiveOutcomeRecorder(settings, now=clock.now, mids_fetcher=lambda: mids or {})


def test_startup_excludes_old_history_and_only_admits_new_events(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    create_ledger(path)
    clock = Clock()
    add_event(path, clock)
    recorder = collector(tmp_path, clock, {"BTC": "100"})
    try:
        assert recorder.stats()["cursor"] == 1
        assert recorder.ingest_ledger() == (0, 0)
        clock.at += timedelta(seconds=5)
        add_event(path, clock, seq=2)
        assert recorder.ingest_ledger() == (1, 1)
        assert recorder.stats()["registered_sources"] == 1
        assert recorder.db.execute(
            "SELECT source_json FROM recorder_sources"
        ).fetchone() is not None
    finally:
        recorder.close()


def test_forward_1m_is_known_only_after_maturity_and_immutable(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    create_ledger(path)
    clock = Clock()
    mids = {"BTC": "101"}
    recorder = collector(tmp_path, clock, mids)
    try:
        clock.at += timedelta(seconds=1)
        add_event(path, clock)
        assert recorder.step()["sources_admitted"] == 1
        assert recorder.stats()["outcomes"] == 0
        clock.at += timedelta(seconds=5)
        recorder.step()  # first price after the declared 2s latency
        clock.at += timedelta(seconds=52)
        mids["BTC"] = "103"
        recorder.step()
        assert recorder.stats()["outcomes"] == 0
        clock.at += timedelta(seconds=7)
        recorder.step()
        assert recorder.stats()["outcomes"] == 1
        row = recorder.db.execute(
            "SELECT payload_json FROM outcome_records"
        ).fetchone()
        assert row is not None
        data = json.loads(row[0])
        assert data["horizon"] == "1m"
        assert Decimal(data["signal_price"]) == Decimal(100)
        assert Decimal(data["latency_price"]) == Decimal(101)
        assert Decimal(data["horizon_price"]) == Decimal(103)
        assert Decimal(data["net_return"]) == Decimal(103) / Decimal(101) - 1 - Decimal("0.001")
        clock.at += timedelta(seconds=5)
        recorder.step()
        assert recorder.stats()["outcomes"] == 1
    finally:
        recorder.close()


def test_missing_price_is_censored_without_fabricated_return(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    create_ledger(path)
    clock = Clock()
    recorder = collector(tmp_path, clock)
    try:
        clock.at += timedelta(seconds=1)
        add_event(path, clock)
        recorder.step()
        clock.at += timedelta(minutes=2)
        recorder.step()
        assert recorder.stats()["outcomes"] == 0
        assert recorder.db.execute(
            "SELECT reason FROM recorder_censored WHERE horizon='1m'"
        ).fetchone() == ("PRICE_MISSING_OR_TOO_LATE",)
    finally:
        recorder.close()


def test_late_source_is_skipped_and_cannot_be_backfilled(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    create_ledger(path)
    clock = Clock()
    recorder = collector(tmp_path, clock, {"BTC": "100"})
    try:
        clock.at += timedelta(seconds=1)
        add_event(path, clock)
        clock.at += timedelta(minutes=2)
        assert recorder.ingest_ledger() == (1, 0)
        assert recorder.db.execute(
            "SELECT reason FROM recorder_skipped"
        ).fetchone() == ("LATE_LEDGER_CONSUMPTION",)
    finally:
        recorder.close()


def test_restart_reuses_registered_source_and_outcome_identity(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    create_ledger(path)
    clock = Clock()
    recorder = collector(tmp_path, clock, {"BTC": "101"})
    clock.at += timedelta(seconds=1)
    add_event(path, clock)
    assert recorder.step()["sources_admitted"] == 1
    clock.at += timedelta(seconds=5)
    recorder.step()  # capture a causal entry-side mid before process restart
    recorder.close()

    clock.at += timedelta(seconds=55)
    resumed = collector(tmp_path, clock, {"BTC": "105"})
    try:
        assert resumed.stats()["cursor"] == 1
        assert resumed.stats()["registered_sources"] == 1
        resumed.step()
        assert resumed.stats()["outcomes"] == 1
        assert resumed.ingest_ledger() == (0, 0)
    finally:
        resumed.close()


def test_nondefault_instrument_is_explicitly_skipped(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    create_ledger(path)
    clock = Clock()
    recorder = collector(tmp_path, clock)
    try:
        clock.at += timedelta(seconds=1)
        add_event(path, clock, instrument="hyperliquid:mainnet:perp:xyz:SP500")
        assert recorder.ingest_ledger() == (1, 0)
        assert recorder.db.execute(
            "SELECT reason FROM recorder_skipped"
        ).fetchone() == ("UNSUPPORTED_VENUE",)
    finally:
        recorder.close()


def test_outcome_policy_drift_is_rejected_without_overwriting(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    create_ledger(path)
    clock = Clock()
    recorder = collector(tmp_path, clock, {"BTC": "101"})
    clock.at += timedelta(seconds=1)
    add_event(path, clock)
    recorder.step()
    recorder.close()
    clock.at += timedelta(minutes=1)
    updated = replace(
        RecorderSettings(ledger_db=path, outcome_db=tmp_path / "outcomes.db"),
        cost_bps=Decimal(99),
    )
    drifted = LiveOutcomeRecorder(updated, now=clock.now, mids_fetcher=lambda: {"BTC": "105"})
    try:
        with pytest.raises(RuntimeError, match="manifest hash/config drift"):
            drifted.step()
        assert drifted.stats()["outcomes"] == 0
    finally:
        drifted.close()
