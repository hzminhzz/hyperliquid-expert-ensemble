"""Bounded CHD research acquisition contracts — synthetic, no provider calls."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from ensemble.pm_chd_target_1m import fetch_days, _day_grid_quality, _propr_allowlist
from ensemble.pm_propr_scope import SCOPE_STRICT, SCOPE_TENTATIVE


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


def _propr_scope(tmp_path: Path, audit: Path):
    source = {
        "schema": "pm01_propr_scoped_90pct_market_readiness_v1",
        "source_cohort_wallet_count": 36,
        "input_files_sha256": {"market_1m_audit": sha256(audit.read_bytes()).hexdigest()},
        "market_day_coverage": {
            "strict_native_crypto_display": {"target": 0.90, "target_reached": False},
            "strict_plus_tentative_xyz_display": {"target": 0.90, "target_reached": False},
        },
        "propr_only_priority_market_acquisition_queue": [
            {
                "provider_symbol": "ALGO",
                "date_utc": "2026-10-29",
                "status": "MISSING_NATIVE_MARKET_DAY",
                "scope": SCOPE_STRICT,
                "allowed_for_market_data_acquisition": True,
            },
            {
                "provider_symbol": "xyz:AMD",
                "date_utc": "2026-10-28",
                "status": "MISSING_NATIVE_MARKET_DAY",
                "scope": SCOPE_TENTATIVE,
                "allowed_for_market_data_acquisition": True,
            },
        ],
    }
    raw = (json.dumps(source, sort_keys=True) + "\n").encode()
    path = tmp_path / f"pm01-36-wallets-{sha256(raw).hexdigest()[:20]}.json"
    path.write_bytes(raw)
    return path, source


def test_propr_limit_blocks_wasted_out_of_market_day_fetch_before_sdk(tmp_path):
    audit = _audit(tmp_path)
    path, scope = _propr_scope(tmp_path, audit)
    with pytest.raises(ValueError, match="OUT_OF_SCOPE_FOR_PROPR_DATA_ACQUISITION"):
        fetch_days(tmp_path, audit, (("POL", "2026-10-28"),), 1, "trades", path, "strict")
    with pytest.raises(ValueError, match="PROPR_SCOPE_AUDIT_SHA_MISMATCH"):
        _propr_allowlist(scope, "f" * 64, "strict")
    strict_allowed = _propr_allowlist(scope, sha256(audit.read_bytes()).hexdigest(), "strict")
    assert strict_allowed == {("ALGO", "2026-10-29")}
    candidate_allowed = _propr_allowlist(scope, sha256(audit.read_bytes()).hexdigest(), "candidate")
    assert ("xyz:AMD", "2026-10-28") in candidate_allowed
    scope["market_day_coverage"]["strict_native_crypto_display"]["target_reached"] = True
    with pytest.raises(ValueError, match="PROPR_90_PERCENT_DATA_TARGET_ALREADY_MET"):
        _propr_allowlist(scope, sha256(audit.read_bytes()).hexdigest(), "strict")


def test_unsealed_propr_scope_cannot_authorize_any_provider_call(tmp_path):
    audit = _audit(tmp_path)
    path, _ = _propr_scope(tmp_path, audit)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="UNSEALED_PROPR_SCOPE_EVIDENCE"):
        fetch_days(tmp_path, audit, (("ALGO", "2026-10-29"),), 1, "trades", path, "strict")


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
