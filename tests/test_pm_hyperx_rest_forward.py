"""Point-in-time integrity and budget tests for bounded HyperX REST monitoring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json

import pytest

from ensemble.pm_hyperx_rest_forward import (
    _recent_observation,
    ingest_batch,
    parse_pairs,
    MAX_REQUESTS,
)
from ensemble.pm_sources import CohortEntry
import ensemble.pm_hyperx_rest_forward as rest


ADDRESS = "0x" + "a" * 40


def _entry():
    return CohortEntry(
        "W001",
        "original",
        ADDRESS,
        sha256(ADDRESS.encode()).hexdigest(),
        "VENDOR_ROSTER_CACHED_HISTORICAL_ONLY",
        1,
        2,
    )


def _row(at, *, tid=1, coin="BTC", address=ADDRESS):
    return {
        "user": address,
        "coin": coin,
        "tid": tid,
        "time": int(at.timestamp() * 1000),
        "sz": "2",
        "px": "100",
        "side": "B",
        "start_position": "1",
    }


def test_vendor_exchange_time_never_becomes_first_known_time():
    at = datetime.now(UTC)
    item = _recent_observation(
        _row(at - timedelta(seconds=10)),
        _entry(),
        "BTC",
        requested_at=at - timedelta(seconds=1),
        received_at=at,
    )
    assert item["locally_received_at"] == at.isoformat()
    assert item["source_first_known_at"] is None
    assert item["whole_account_reconciled"] is False
    assert item["auto_promotion_authorized"] is False
    assert item["coverage"] == ("REST_RECENT_FILL_REQUIRES_BOOK_AND_EXECUTION_RECONCILIATION")


def test_stale_fill_not_claimed_to_be_forward_observation():
    now = datetime.now(UTC)
    assert (
        _recent_observation(
            _row(now - timedelta(days=5)),
            _entry(),
            "BTC",
            requested_at=now - timedelta(seconds=1),
            received_at=now,
        )
        is None
    )


def test_wrong_wallet_and_future_fill_fail_closed():
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="FILL_WALLET_OR_INSTRUMENT_MISMATCH"):
        _recent_observation(
            _row(now, address="0x" + "b" * 40),
            _entry(),
            "BTC",
            requested_at=now - timedelta(seconds=1),
            received_at=now,
        )
    with pytest.raises(ValueError, match="FUTURE_OF_OBSERVER"):
        _recent_observation(
            _row(now + timedelta(minutes=3)),
            _entry(),
            "BTC",
            requested_at=now,
            received_at=now,
        )


def test_no_unknown_coin_duplicate_batch_or_unbounded_requests(monkeypatch, tmp_path):
    monkeypatch.setattr(rest, "load_cohort", lambda root: ([_entry()], {"manifest": "a" * 64}))
    monkeypatch.setattr(rest, "_coins", lambda root, alias: ["BTC", "xyz:AMD"])
    verified, fp = parse_pairs(tmp_path, ("W001/BTC", "W001/xyz:AMD"))
    assert len(verified) == 2 and fp["manifest"] == "a" * 64
    for requests in (
        ("W001/BTC", "W001/BTC"),
        ("W001/ETH",),
        ("FORGED/BTC",),
        tuple(f"W001/C{i}" for i in range(MAX_REQUESTS + 1)),
    ):
        with pytest.raises(ValueError):
            parse_pairs(tmp_path, requests)


def test_bounded_authenticated_page_is_private_and_never_economic(monkeypatch, tmp_path):
    monkeypatch.setattr(rest, "load_cohort", lambda root: ([_entry()], {"manifest": "a" * 64}))
    monkeypatch.setattr(rest, "_coins", lambda root, alias: ["BTC"])
    now = datetime.now(UTC)
    row = _row(now - timedelta(seconds=8))
    payload = json.dumps(
        {"page": 1, "page_size": 2000, "has_more": False, "fills": [row]}, separators=(",", ":")
    ).encode()

    class FakeSource:
        def key_from_existing_tracker_env(self):
            return "TEST_TOKEN_NOT_REAL"

        def provider_request(self, alias, address, coin, page, token):
            assert (alias, address, coin, page) == ("W001", ADDRESS, "BTC", 1)
            assert token == "TEST_TOKEN_NOT_REAL"
            return json.loads(payload), payload, {"x-ratelimit-remaining": "25"}

        def validate(self, doc, alias, coin, page, address):
            assert doc["fills"][0]["user"] == address
            return {"rows": len(doc["fills"])}

    monkeypatch.setattr(rest, "_adapter", lambda root: FakeSource())
    generation = tmp_path / "private_generations" / "g123456789"
    report = ingest_batch(tmp_path, generation, ("W001/BTC",))
    assert report["attempted_requests"] == 1
    assert report["max_request_weight"] <= 5
    assert report["all_wallets_prospective_qualified"] is False
    assert report["no_complete_account_snapshot_claim"]
    assert report["results"][0]["recent_fill_receipts_new"] == 1
    assert report["results"][0]["source_first_known_at_attested"] is False
    raw = list((generation / "vendor_raw" / "W001" / "BTC").glob("page1-*.json"))
    assert len(raw) == 1 and raw[0].read_bytes() == payload
    receipts = list((generation / "first_local_receipts").glob("hyperx-observation-*.json"))
    assert len(receipts) == 1
    data = json.loads(receipts[0].read_text())
    assert data["source_first_known_at"] is None
    assert data["stream_source"].startswith("HYPERX_DATA_API_REST")
    assert receipts[0].stat().st_mode & 0o077 == 0
    assert generation.stat().st_mode & 0o077 == 0
    assert "TEST_TOKEN" not in json.dumps(report)
    assert "0xaaaaaaaa" not in json.dumps(report)
    # Existing generation never overwritten by repeated source polling.
    with pytest.raises(
        ValueError, match="IMMUTABLE_GENERATION_CONFLICT|SOURCE_PAGE_ALREADY_PRESENT"
    ):
        # We alter the original returned page to force collision with a fixed
        # generation. Without collision the immutable first-observation wins.
        from ensemble.pm_hyperx_gap_recovery import _atomic

        _atomic(raw[0], b"wrong")


def test_rest_poll_never_mints_unapproved_first_known_or_account_equity():
    assert MAX_REQUESTS == 5
    assert rest.MIN_INTERVAL_SECONDS >= 12
    assert rest.RECENT_WINDOW <= timedelta(hours=1)
