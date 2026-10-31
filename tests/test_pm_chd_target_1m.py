"""Bounded CHD research acquisition contracts — synthetic, no provider calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ensemble.pm_chd_target_1m import fetch_days, _day_grid_quality


def test_day_sha_is_not_completeness_or_executable_capacity():
    full = _day_grid_quality({"bars": 1440, "zero_volume_minutes": 1100})
    assert full["full_utc_minute_grid"] is True
    assert full["zero_volume_minutes"] == 1100
    assert full["market_capacity_verified"] is False
    missing = _day_grid_quality({"bars": 1424, "zero_volume_minutes": 883})
    assert missing["full_utc_minute_grid"] is False
    assert missing["missing_utc_minutes"] == 16
    assert missing["market_capacity_verified"] is False
    unknown = _day_grid_quality({"sha256": "a" * 64})
    assert unknown["full_utc_minute_grid"] is None
    assert unknown["missing_utc_minutes"] is None
    assert unknown["market_capacity_verified"] is False


def _audit(tmp_path: Path):
    target = tmp_path / "market-audit.json"
    target.write_text(
        json.dumps(
            {
                "schema": "pm01_native_1m_36_wallet_coverage_v1",
                "wallet_count": 36,
                "missing_or_invalid_instrument_day_queue": [
                    {
                        "provider_symbol": "ALGO",
                        "date_utc": "2026-10-29",
                        "reason": "MISSING_NATIVE_MARKET_DAY",
                    },
                    {
                        "provider_symbol": "POL",
                        "date_utc": "2026-10-28",
                        "reason": "MISSING_NATIVE_MARKET_DAY",
                    },
                    {
                        "provider_symbol": "xyz:ANTH",
                        "date_utc": "2026-10-29",
                        "reason": "UNSUPPORTED_HIP3_DEX",
                    },
                ],
            }
        )
    )
    return target


def test_no_external_provider_on_unapproved_market_day(tmp_path):
    evidence = _audit(tmp_path)
    with pytest.raises(ValueError, match="TARGET_NOT_IN_SHA_AUDITED"):
        fetch_days(tmp_path, evidence, (("AVAX", "2026-10-29"),))


def test_no_unsupported_hip3_dex_silently_mapped(tmp_path):
    evidence = _audit(tmp_path)
    with pytest.raises(ValueError, match="TARGET_NOT_IN_SHA_AUDITED"):
        fetch_days(tmp_path, evidence, (("xyz:ANTH", "2026-10-29"),))


def test_bounded_deduplicated_requests_and_provider_mode(tmp_path):
    evidence = _audit(tmp_path)
    with pytest.raises(ValueError, match="CHD_REQUEST_BUDGET"):
        fetch_days(tmp_path, evidence, (("ALGO", "2026-10-29"), ("POL", "2026-10-28")), 1)
    with pytest.raises(ValueError, match="TARGET_NOT_IN_SHA_AUDITED"):
        fetch_days(tmp_path, evidence, (("ALGO", "2026-10-29"),) * 2)
    with pytest.raises(ValueError, match="UNRECOGNIZED_CHD_DATA_MODE"):
        fetch_days(tmp_path, evidence, (("ALGO", "2026-10-29"),), 1, "future")


def test_wrong_or_unpinned_audit_rejected(tmp_path):
    evidence = _audit(tmp_path)
    evidence.write_text(
        json.dumps(
            {
                "schema": "bogus",
                "missing_or_invalid_instrument_day_queue": [],
            }
        )
    )
    with pytest.raises(ValueError, match="NOT_VERIFIED_PM01"):
        fetch_days(tmp_path, evidence, (("ALGO", "2026-10-29"),))
