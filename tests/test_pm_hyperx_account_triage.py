"""PM-01 HyperX account-state triage never promotes partial vendor snapshots."""

from __future__ import annotations

import json

import pytest

from ensemble.pm_hyperx_account_triage import (
    ENDPOINTS,
    MAX_WALLETS,
    _verify_response,
    _write_immutable_private,
)


def test_account_analysis_metadata_is_not_a_reconciled_account_history():
    address = "0x" + "a" * 40
    doc = {
        "address": address,
        "wallet_metrics": {"1d": {}, "all": {}},
        "users_historical_leverage": [
            {"timestamp_ms": 1_000, "margin_summary_account_value": 10_000}
        ],
        "user_event_stats": {"asset_flow_events_count": 7},
        "start_timestamp_ms": 1_000,
        "end_timestamp_ms": 8_000,
    }
    out = _verify_response("wallet_analysis", doc, address)
    assert out["leverage_points"] == 1
    assert out["asset_flow_event_count"] == 7
    assert out["historic_account_equity_reconciled"] is False
    assert out["positions_verified_at_cutoff"] is False


def test_funding_partial_pagination_and_margin_limited_to_7d():
    address = "0x" + "a" * 40
    funding = _verify_response(
        "trader_funding",
        {
            "address": address,
            "days": 30,
            "records": [{"time": "2026-10-30T15:00:00+00:00"}] * 100,
            "has_more": True,
        },
        address,
    )
    assert funding["rows_first_page"] == 100
    assert funding["has_more_funding_pages"]
    assert funding["first_page_oldest_utc"] == "2026-10-30T15:00:00+00:00"
    assert funding["first_page_newest_utc"] == "2026-10-30T15:00:00+00:00"
    assert funding["funding_complete_study"] is False
    margin = _verify_response(
        "trader_margin",
        {"address": address, "data_window_days": 7, "points": [{}] * 733},
        address,
    )
    assert margin["reported_days"] == 7
    assert margin["whole_study_margin_complete"] is False


def test_identity_inconsistent_or_missing_fields_rejected():
    address = "0x" + "a" * 40
    with pytest.raises(ValueError, match="IDENTITY"):
        _verify_response(
            "trader_funding",
            {"address": "0x" + "b" * 40, "records": [], "has_more": False},
            address,
        )
    with pytest.raises(ValueError, match="FUNDING_CONTRACT"):
        _verify_response("trader_funding", {"address": address, "records": []}, address)
    with pytest.raises(ValueError, match="MARGIN_CONTRACT"):
        _verify_response("trader_margin", {"address": address, "points": []}, address)
    with pytest.raises(ValueError, match="FUNDING_RECORD_CLOCK"):
        _verify_response(
            "trader_funding",
            {"address": address, "records": [{"time": "2026-10-30 15:00:00"}], "has_more": True},
            address,
        )


def test_owner_only_generation_and_no_overwrite(tmp_path):
    payload = json.dumps({"address": "0x" + "a" * 40}).encode()
    path = tmp_path / "private" / "W035" / "trader_margin.json"
    sha = _write_immutable_private(path, payload)
    assert len(sha) == 64
    assert path.read_bytes() == payload
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match="GENERATION_REUSE"):
        _write_immutable_private(path, payload)


def test_bounded_data_api_only_three_read_endpoints():
    assert MAX_WALLETS == 2
    assert {k for k, _, _ in ENDPOINTS} == {"wallet_analysis", "trader_funding", "trader_margin"}
    assert all(method in ("GET", "POST") for _, method, _ in ENDPOINTS)
    assert all("/" in endpoint for _, _, endpoint in ENDPOINTS)
