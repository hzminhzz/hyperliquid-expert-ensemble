"""PM-02 immutable report-chain security, reproducibility and revision tests."""

from datetime import UTC, datetime, timedelta

import pytest

from ensemble.pm02_audit import (
    digest,
    immutable_report_write,
    read_previous,
    validate_parent,
    verify_report,
)

BASE = datetime(2026, 10, 31, tzinfo=UTC)


def decision(seq: int = 1) -> dict:
    record = {
        "schema": "pm02_manager_governance_v1",
        "cards": [
            {
                "wallet": f"TEST-{i:02d}",
                "lifecycle": {"state": "CANDIDATE", "decided_at": BASE.isoformat()},
            }
            for i in range(36)
        ],
        "asof": (BASE + timedelta(days=seq - 1)).isoformat(),
        "pm01_source_sha256": "pm01-pinned-digest",
        "audit_chain": {"sequence": seq, "parent_sha256": None},
        "untrusted_prospective_input": False,
        "live_trading_authorized": False,
    }
    record["sha256"] = digest(record)
    return record


def test_repeated_run_keeps_same_file_and_hash(tmp_path):
    archive = tmp_path / "private"
    report = decision()
    first = immutable_report_write(archive, report)
    original = first.read_bytes()
    second = immutable_report_write(archive, report)
    assert first == second
    assert second.read_bytes() == original
    assert first.stat().st_mode & 0o077 == 0
    assert archive.stat().st_mode & 0o077 == 0
    assert read_previous(first) == report


def test_report_content_tampering_is_detected(tmp_path):
    report = decision()
    archive = tmp_path / "private"
    path = immutable_report_write(archive, report)
    report["cards"][0]["lifecycle"]["state"] = "CORE"
    with pytest.raises(ValueError, match="DECISION_REPORT_HASH_MISMATCH"):
        verify_report(report)
    path.write_bytes(b'{"malicious":"edit"}')
    with pytest.raises(ValueError):
        read_previous(path)


def test_revision_or_prior_timestamp_cannot_be_retroactively_replayed():
    previous = decision()
    with pytest.raises(ValueError, match="NONMONOTONIC_DECISION_HISTORY"):
        validate_parent(previous, previous["asof"], "pm01-pinned-digest")
    with pytest.raises(ValueError, match="PM01_SOURCE_REVISED"):
        validate_parent(previous, (BASE + timedelta(days=1)).isoformat(), "new source")
    expected = validate_parent(
        previous, (BASE + timedelta(days=1)).isoformat(), "pm01-pinned-digest"
    )
    assert expected["parent_sha256"] == previous["sha256"]
    assert expected["sequence"] == 2
    assert len(expected["states"]) == 36


def test_synthetic_history_cannot_be_promoted_to_research_chain():
    previous = decision()
    previous["untrusted_prospective_input"] = True
    previous["sha256"] = digest({key: value for key, value in previous.items() if key != "sha256"})
    with pytest.raises(ValueError, match="SYNTHETIC_HISTORY"):
        validate_parent(previous, (BASE + timedelta(days=1)).isoformat(), "pm01-pinned-digest")


def test_noncanonical_or_open_permissions_fail_closed(tmp_path):
    report = decision()
    archive = tmp_path / "insecure"
    archive.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="INSECURE_REPORT_DIRECTORY"):
        immutable_report_write(archive, report)
    archive.chmod(0o700)
    path = immutable_report_write(archive, report)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="INSECURE_EXISTING"):
        immutable_report_write(archive, report)
