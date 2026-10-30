"""WP5 read-only operator views reproduce frozen decisions without peeking."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from tests.test_copy_forward import _deliver, _fixture, _record

from ensemble.copy_forward_evidence import (
    explain_arm,
    export_refs,
    list_cuts,
    measure_storage,
    replay_cut,
    verify_export,
)


def test_list_and_explain_do_not_expose_forward_returns(tmp_path):
    frozen,ledger,journal,prices,forward=_fixture(tmp_path)
    r=_record((frozen,ledger,journal,prices,forward))
    _deliver(forward,r["cut_id"])
    report=list_cuts(frozen=frozen,forward_db=forward,limit=1)
    assert report["cuts"][0]["cut_id"]==r["cut_id"]
    assert report["cuts"][0]["candidate_count"]==1
    assert report["truncated"] is False
    assert report["next_cursor"] is None
    assert report["outcomes_disclosed"] is False
    with closing(sqlite3.connect(forward)) as db:
        arm=db.execute("SELECT arm_id FROM forward_arms").fetchone()[0]
    explained=explain_arm(frozen=frozen,forward_db=forward,arm_id=arm)
    assert explained["return_values_redacted"] is True
    assert explained["immutable_decision"]["direction"]==1
    assert explained["source_references"]["ledger_id"]=="L1"
    assert explained["cut"]["cut_id"]==r["cut_id"]
    assert explained["delivery_history"][0]["status"]=="ACKNOWLEDGED"
    assert explained["expected_return"] is None
    assert explained["financial_execution"] is False
    assert explain_arm(
        frozen=frozen,forward_db=forward,arm_id="hour:unknown",
    )["status"]=="UNKNOWN_ARM"


def test_operator_replay_matches_and_refuses_retroactive_source_restatement(tmp_path):
    frozen,ledger,journal,prices,forward=_fixture(tmp_path)
    cut=_record((frozen,ledger,journal,prices,forward))
    period=cut["period"]
    replay=replay_cut(
        frozen=frozen,forward_db=forward,
        journal_db=journal,price_db=prices,period=period,
    )
    assert replay["status"]=="REPLAY_MATCH"
    assert replay["source_evidence_hash_matches"] is True
    assert replay["baseline_direction_hash_matches"] is True
    assert replay["predictive_qualified"] is False
    # Simulate a corrupted old checkpoint after the immutable cut. Replay
    # must expose drift rather than update the first decision or hide it.
    with closing(sqlite3.connect(journal)) as db,db:
        db.execute(
            "INSERT INTO source_coverage_barriers VALUES (?,?,?)",
            ("L1",8,(datetime(2026,11,1,12,4,30,tzinfo=UTC)).isoformat()),
        )
    changed=replay_cut(
        frozen=frozen,forward_db=forward,
        journal_db=journal,price_db=prices,period=period,
    )
    assert changed["status"]=="REPLAY_DRIFT"
    assert changed["source_evidence_hash_matches"] is False


def test_bounded_pagination_and_immutable_verified_export(tmp_path):
    frozen,ledger,journal,prices,forward=_fixture(tmp_path)
    _record((frozen,ledger,journal,prices,forward))
    at=datetime(2026,11,1,13,5,tzinfo=UTC)
    with closing(sqlite3.connect(ledger)) as db,db:
        db.execute(
            "UPDATE outbox_events SET known_at=? WHERE seq=10",
            ((at-timedelta(seconds=1)).isoformat(),),
        )
    _record((frozen,ledger,journal,prices,forward),at=at)
    first=list_cuts(frozen=frozen,forward_db=forward,limit=1)
    assert first["truncated"] is True
    assert first["next_cursor"]=="2026-11-01T12:00Z"
    second=list_cuts(
        frozen=frozen,forward_db=forward,after=first["next_cursor"],limit=1,
    )
    assert len(second["cuts"])==1
    assert second["cuts"][0]["period"]=="2026-11-01T13:00Z"
    assert second["next_cursor"] is None
    with pytest.raises(ValueError,match="bounded"):
        list_cuts(frozen=frozen,forward_db=forward,limit=101)
    output=tmp_path/"private"/"w1.ndjson.gz"
    saved=export_refs(
        frozen=frozen,forward_db=forward,output=output,limit=20,
    )
    assert saved["status"]=="IMMUTABLE_REFERENCE_EXPORT"
    assert saved["rows"]==2
    assert output.stat().st_mode & 0o777 == 0o600
    assert verify_export(output=output)["rows"]==2
    with pytest.raises(ValueError,match="destination"):
        export_refs(frozen=frozen,forward_db=forward,output=output)
    output.write_bytes(output.read_bytes()+b"modified")
    with pytest.raises(ValueError,match="hash mismatch"):
        verify_export(output=output)
    store=measure_storage(forward_db=forward)
    assert store["bytes_total"]>0
    assert store["retention_policy"].startswith("KEEP_ORIGINAL")


def test_unknown_cut_replay_is_bounded_no_forward_creation(tmp_path):
    frozen,_ledger,journal,prices,_forward=_fixture(tmp_path)
    result=replay_cut(
        frozen=frozen,forward_db=tmp_path/"unpopulated.db",
        journal_db=journal,price_db=prices,period="2026-11-01T12:00Z",
    )
    # Absent forward DB is a hard read-only failure; never synthesizes past cut.
    assert result["status"]=="NOT_YET_STARTED"
