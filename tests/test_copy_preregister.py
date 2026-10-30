"""WP3 frozen research protocol must predate held-out outcomes and retain roster."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from ensemble.copy_preregister import (
    ForwardProtocol,
    freeze,
    fresh_universe,
)

NOW = datetime(2026, 10, 30, 13, tzinfo=UTC)
START = datetime(2026, 10, 31, tzinfo=UTC)
END = datetime(2026, 11, 30, tzinfo=UTC)
CODE_SHA = "a" * 64


def payload(**overrides):
    values = {
        "frozen_at": NOW, "holdout_start": START,
        "holdout_end": END, "research_code_sha": CODE_SHA,
        "source_release_ensemble_sha": "b" * 64,
        "source_release_tracker_sha": "c" * 64,
        "source_ledger_id": "ledger-test", "ledger_seq_cut": 45,
        "opportunity_cursor_cut": 40,
        "universe_sha256": "d" * 64,
        "universe_wallet_count": 20,
        "historical_report_sha256": "e" * 64,
    }
    values.update(overrides)
    return ForwardProtocol(**values)


def test_future_holdout_and_deterministic_id() -> None:
    p=payload()
    assert p.sealed()==payload().sealed()
    protocol=p.sealed()["protocol"]
    assert protocol["holdout"]["start_inclusive"]==START.isoformat()
    assert protocol["policy"]["decision_status"]=="NOT_EMITTING_FROZEN_HOURLY_FORECASTS_UNTIL_WP4"
    assert protocol["comparison"]["baseline_families"]==[
        "cash","long_only","lagged_15m_trend","lagged_1h_trend",
        "lagged_1h_reversal",
    ]
    assert protocol["comparison"]["price_source"]=="MINUTE_MID_DIAGNOSTIC_ONLY"
    assert protocol["success_rule"]["promotion"]=="BLOCKED_UNTIL_NEW_HUMAN_APPROVAL"


def test_protocol_refuses_expired_holdout_or_unproven_source_cut() -> None:
    with pytest.raises(ValueError,match="freeze"):
        payload(frozen_at=START)
    with pytest.raises(ValueError,match="freeze"):
        payload(holdout_end=START+timedelta(days=2))
    with pytest.raises(ValueError,match="cursor"):
        payload(opportunity_cursor_cut=48)
    with pytest.raises(ValueError,match="wallet"):
        payload(universe_wallet_count=2)
    with pytest.raises(ValueError,match="hash"):
        payload(research_code_sha="incorrect")


def _sources(tmp_path):
    ledger=tmp_path/"source.db"
    journal=tmp_path/"opportunity.db"
    release=tmp_path/"release.json"
    history=tmp_path/"history.json"
    with closing(sqlite3.connect(ledger)) as db,db:
        db.execute("CREATE TABLE ledger_meta(ledger_id TEXT,current_seq INTEGER)")
        db.execute("INSERT INTO ledger_meta VALUES ('ledger-test',45)")
        db.execute(
            "CREATE TABLE outbox_events(wallet_id TEXT,known_at TEXT,"
            "kind TEXT,quality TEXT,seq INTEGER)"
        )
        for i in range(25):
            db.execute("INSERT INTO outbox_events VALUES (?,?,?,?,?)",(
                f"wallet-{i:03d}",(NOW-timedelta(minutes=5)).isoformat(),
                "ACCOUNT_SNAPSHOT","valid",i+1,
            ))
        db.execute("INSERT INTO outbox_events VALUES (?,?,?,?,?)",(
            "stale-wallet",(NOW-timedelta(days=3)).isoformat(),
            "ACCOUNT_SNAPSHOT","valid",26,
        ))
        db.execute("INSERT INTO outbox_events VALUES (?,?,?,?,?)",(
            "invalid-wallet",(NOW-timedelta(minutes=1)).isoformat(),
            "ACCOUNT_SNAPSHOT","degraded",27,
        ))
    with closing(sqlite3.connect(journal)) as db,db:
        db.execute("CREATE TABLE opportunity_state(ledger_id TEXT,last_seq INTEGER)")
        db.execute("INSERT INTO opportunity_state VALUES ('ledger-test',40)")
    release.write_text(json.dumps({
        "schema":"copytrade-pinned-release-v1",
        "ensemble_sha":"b" * 64,"tracker_sha":"c" * 64,
    }))
    history.write_text('{"status":"INCONCLUSIVE"}\n')
    return ledger,journal,release,history


def test_frozen_roster_excludes_stale_and_writes_private_once(tmp_path,monkeypatch):
    ledger,journal,release,history=_sources(tmp_path)
    lid,seq,wallets=fresh_universe(ledger,cut=NOW)
    assert lid=="ledger-test" and seq==45
    assert len(wallets)==25
    assert "stale-wallet" not in wallets
    assert "invalid-wallet" not in wallets
    monkeypatch.setattr("ensemble.copy_preregister._git_head",lambda repo:CODE_SHA)
    root=tmp_path/"private-release"
    args={
        "ensemble_repo":tmp_path,"ledger_db":ledger,"journal_db":journal,
        "release_manifest":release,"historical_report":history,
        "destination":root,"now":NOW,
    }
    result=freeze(**args)
    assert result["status"]=="FROZEN_COLLECTION_ONLY_AWAIT_WP4"
    assert result["wallet_count"]==25
    assert root.stat().st_mode & 0o777 == 0o700
    for f in root.iterdir():
        assert f.stat().st_mode & 0o777 == 0o600
    frozen=json.loads((root/"protocol.json").read_text())
    roster=json.loads((root/"wallet-universe.PRIVATE.json").read_text())
    assert frozen["protocol"]["source_cut"]["universe_sha256"]
    assert frozen["protocol"]["source_cut"]["universe_wallets"]==25
    assert roster["wallets"] == wallets
    assert frozen["protocol"]["source_cut"]["historical_attribution_sha256"] == (
        hashlib.sha256(history.read_bytes()).hexdigest()
    )
    with pytest.raises(FileExistsError,match="already exists"):
        freeze(**args)
    assert json.loads((root/"protocol.json").read_text())==frozen


def test_freeze_rejects_starting_holdout_without_writing(tmp_path,monkeypatch):
    ledger,journal,release,history=_sources(tmp_path)
    monkeypatch.setattr("ensemble.copy_preregister._git_head",lambda repo:CODE_SHA)
    dest=tmp_path/"future"
    with pytest.raises(ValueError,match="future holdout"):
        freeze(
            ensemble_repo=tmp_path,ledger_db=ledger,journal_db=journal,
            release_manifest=release,historical_report=history,
            destination=dest,now=START+timedelta(minutes=1),
        )
    assert not dest.exists()
