"""HyperX Data API WS forward observations: no broker or false first-known."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from ensemble.pm_hyperx_forward import (
    extract_observations,
    store_first_local_receipt,
    MAX_MESSAGES,
    MAX_SECONDS,
)

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=UTC)
ADDRESS = "0x" + "a" * 40


def _frame(event_at=NOW, *, user=ADDRESS, snapshot=False, after=None, tid=123):
    return json.dumps(
        {
            "type": "fills",
            "data": {
                "user": user,
                "isSnapshot": snapshot,
                "fills": [
                    {
                        "coin": "xyz:AMD",
                        "time": int(event_at.timestamp() * 1000),
                        "tid": tid,
                        "px": "254.3",
                        "sz": "1.5",
                        "side": "B",
                        "start_position": after if after is not None else "0",
                    }
                ],
            },
        }
    )


def test_forward_received_clock_is_first_local_only_not_account_qualified():
    connected = NOW - timedelta(seconds=5)
    receipt = extract_observations(
        _frame(),
        NOW + timedelta(seconds=1),
        {"CA-005": ADDRESS},
        connected_at=connected,
    )
    assert len(receipt) == 1
    row = receipt[0]
    assert row["wallet"] == "CA-005"
    assert row["source_event_at"] == NOW.isoformat()
    assert row["locally_received_at"] == (NOW + timedelta(seconds=1)).isoformat()
    assert row["coverage"] == "PROSPECTIVE_RECEIPT_OBSERVED_BOOK_NOT_ATTESTED"
    assert not row["whole_account_reconciled"]
    assert not row["real_follower_order_sent"]
    assert "first_known_at" not in row  # recorded receipt alone != attested first-known


def test_snapshot_old_event_never_promoted_as_forward_action():
    connected = NOW - timedelta(seconds=5)
    rows = extract_observations(
        _frame(NOW - timedelta(days=1), snapshot=True),
        NOW,
        {"CA-005": ADDRESS},
        connected_at=connected,
    )
    assert len(rows) == 1
    assert rows[0]["coverage"] == "RECOVERED_OR_STALE_NOT_FOR_FORWARD_QUALIFICATION"


def test_unknown_wallet_and_future_timestamp_do_not_become_receipts():
    connected = NOW - timedelta(seconds=5)
    assert (
        extract_observations(
            _frame(user="0x" + "b" * 40),
            NOW,
            {"CA-005": ADDRESS},
            connected_at=connected,
        )
        == []
    )
    assert (
        extract_observations(
            _frame(NOW + timedelta(minutes=3)),
            NOW,
            {"CA-005": ADDRESS},
            connected_at=connected,
        )
        == []
    )
    assert (
        extract_observations(
            '{"type":"heartbeat","data":{}}',
            NOW,
            {"CA-005": ADDRESS},
            connected_at=connected,
        )
        == []
    )
    assert MAX_MESSAGES <= 5 and MAX_SECONDS <= 60


def test_first_seen_file_is_immutable_and_conflicting_tids_blocked(tmp_path):
    observation = extract_observations(
        _frame(),
        NOW,
        {"CA-005": ADDRESS},
        connected_at=NOW - timedelta(seconds=1),
    )[0]
    folder = tmp_path / "events"
    assert store_first_local_receipt(folder, observation)
    assert not store_first_local_receipt(folder, observation)
    paths = list(folder.glob("hyperx-observation-*.json"))
    assert len(paths) == 1
    original = paths[0].read_bytes()
    different = dict(observation, source_fill_quantity="2")
    with pytest.raises(ValueError, match="PAYLOAD_CONFLICT"):
        store_first_local_receipt(folder, different)
    assert paths[0].read_bytes() == original
    assert folder.stat().st_mode & 0o077 == 0
    assert paths[0].stat().st_mode & 0o077 == 0


def test_same_fill_reconnected_cannot_change_first_local_receipt(tmp_path):
    first = extract_observations(
        _frame(),
        NOW,
        {"CA-005": ADDRESS},
        connected_at=NOW - timedelta(seconds=1),
    )[0]
    folder = tmp_path / "events"
    assert store_first_local_receipt(folder, first)
    retry = dict(first, locally_received_at=(NOW + timedelta(minutes=1)).isoformat())
    assert store_first_local_receipt(folder, retry) is False
    saved = json.loads(next(folder.iterdir()).read_text())
    assert saved["locally_received_at"] == NOW.isoformat()
