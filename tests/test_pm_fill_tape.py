"""Compact PM-01 HyperX fill tape for PM-03 shadow-only consumers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

import pytest

from ensemble.pm_fill_tape import build_wallet_tape
from ensemble.pm_sources import CohortEntry

START = datetime(2026, 10, 20, tzinfo=UTC)


def _entry():
    address = "0x" + "1" * 40
    return CohortEntry(
        "W044",
        "original",
        address,
        sha256(address.encode()).hexdigest(),
        "VENDOR_ROSTER_CACHED_HISTORICAL_ONLY",
        1,
        6,
    )


def _row(minutes, tid, coin, before, after, price=100):
    when = START + timedelta(minutes=minutes)
    before, after = Decimal(before), Decimal(after)
    return (when, tid, coin, before, after, after - before, Decimal(price))


def _two_coin_events():
    return [
        _row(0, 1, "BTC", "0", "2"),
        _row(1, 2, "ETH", "-3", "-2"),
        _row(2, 3, "BTC", "2", "3"),
        _row(3, 4, "BTC", "3", "1"),
        _row(4, 5, "BTC", "1", "-1"),
        _row(5, 6, "BTC", "-1", "0"),
    ]


def test_all_five_position_transitions_preserve_exact_observed_signed_book():
    tape = build_wallet_tape(_entry(), ["BTC", "ETH"], _two_coin_events(), [])
    assert tape["book_status"] == "HISTORICAL_OBSERVED_VENDOR_LISTED_ONLY"
    assert tape["causal_follower_actions_allowed"] is False
    assert tape["economic_replay_allowed"] is False
    assert tape["source_first_known_at"] is None
    assert tape["anchor_source_event_at"] == (START + timedelta(minutes=1)).isoformat()
    assert tape["initial_signed_book"] == [
        {
            "source_coin": "BTC",
            "instrument": "hyperliquid:mainnet:perp:default:BTC",
            "signed_quantity": "2",
            "instrument_resolution": "EXACT",
        },
        {
            "source_coin": "ETH",
            "instrument": "hyperliquid:mainnet:perp:default:ETH",
            "signed_quantity": "-2",
            "instrument_resolution": "EXACT",
        },
    ]
    assert [event["changes"][0]["transition"] for event in tape["events"]] == [
        "ADD",
        "REDUCE",
        "FLIP",
        "CLOSE",
    ]
    assert all(event["first_known_at"] is None for event in tape["events"])
    assert all(not event["can_feed_pm03_actionable_account"] for event in tape["events"])
    assert tape["post_anchor_source_gaps"] == 0


def test_gap_blocks_all_later_targets_but_preserves_raw_observations():
    rows = _two_coin_events()
    rows[2] = _row(2, 3, "BTC", "20", "21")
    tape = build_wallet_tape(_entry(), ["BTC", "ETH"], rows, ["BTC:POSITION_DISCONTINUITY"])
    assert tape["post_anchor_source_gaps"] >= 1
    assert tape["book_status"] == "UNKNOWN_AFTER_SOURCE_GAP"
    assert all(x["status"] == "UNKNOWN_AFTER_SOURCE_GAP" for x in tape["events"])
    assert tape["events"][0]["changes"][0]["observed_before"] == "20"
    assert tape["events"][0]["changes"][0]["source_continuous"] is False
    assert tape["causal_follower_actions_allowed"] is False


def test_unsupported_hedge_is_explicit_unknown_not_silently_removed():
    rows = [_row(0, 1, "BTC", "0", "2"), _row(1, 2, "#12270", "-2", "-1")]
    tape = build_wallet_tape(_entry(), ["BTC", "#12270"], rows, [])
    assert tape["book_status"] == "UNKNOWN_UNSUPPORTED_INSTRUMENT"
    assert tape["unsupported_vendor_instruments"] == ["#12270"]
    assert tape["initial_signed_book"][0]["instrument"] is None
    assert tape["initial_signed_book"][0]["signed_quantity"] == "-1"


def test_fatal_source_error_has_no_executable_or_synthetic_snapshot():
    tape = build_wallet_tape(
        _entry(), ["BTC", "ETH"], _two_coin_events(), ["ETH:source page SHA mismatch"]
    )
    assert tape["book_status"] == "BLOCKED_UNVERIFIED_SOURCE"
    assert tape["events"] == []
    assert tape["initial_signed_book"] is None


def test_cannot_import_new_future_fills_into_frozen_tape():
    from ensemble.pm_fill_replay import CUT

    future = (
        CUT + timedelta(minutes=1),
        10,
        "BTC",
        Decimal(0),
        Decimal(1),
        Decimal(1),
        Decimal(100),
    )
    with pytest.raises(ValueError, match="FROZEN_CUTOFF"):
        build_wallet_tape(_entry(), ["BTC"], [future], [])
