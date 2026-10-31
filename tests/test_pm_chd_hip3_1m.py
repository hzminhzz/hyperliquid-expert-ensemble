"""No-overwrite, source-attested namespaced Hyperliquid 1m contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
import json

import pytest

from ensemble.pm_chd_hip3_1m import (
    _market_path,
    _parse_target,
    _materialize_trade_day,
    _publish_missing_only,
    _validate_requested,
)
from ensemble.pm_shadow import VerifiedHyperliquidMinuteSource


def test_hip3_namespace_cannot_collide_with_xyz_or_default(tmp_path):
    day = date(2026, 10, 15)
    io = _market_path(tmp_path, "io:ANTH", day)
    para = _market_path(tmp_path, "para:ANTH", day)
    assert io != para
    assert io != _market_path(tmp_path, "io:SNDK", day)
    assert "HYPERLIQUID_IO_PERP/symbol=ANTH" in io.as_posix()
    assert "HYPERLIQUID_PARA_PERP/symbol=ANTH" in para.as_posix()


@pytest.mark.parametrize(
    "value",
    [
        "xyz:AMD@2026-10-15",
        "io:ANTH/..@2026-10-15",
        "io:../BTC@2026-10-15",
        "foo:ANTH@2026-10-15",
        "io:@2026-10-15",
        "BTC@2026-10-15",
        "io:ANTH:BAD@2026-10-15",
    ],
)
def test_disallow_unapproved_market_or_path(value):
    with pytest.raises(ValueError):
        _parse_target(value)


def test_prior_frozen_audit_only_accepts_missing_exact_venue_day():
    allow = {
        "schema": "pm01_native_1m_36_wallet_coverage_v1",
        "wallet_count": 36,
        "missing_or_invalid_instrument_day_queue": [
            {
                "provider_symbol": "io:ANTH",
                "date_utc": "2026-10-15",
                "reason": "MISSING_NATIVE_MARKET_DAY",
            },
            {
                "provider_symbol": "para:VST",
                "date_utc": "2026-10-15",
                "reason": "MISSING_NATIVE_MARKET_DAY",
            },
            {
                "provider_symbol": "io:ANTH",
                "date_utc": "2026-10-14",
                "reason": "SOURCE_SHA_MISMATCH",
            },
        ],
    }
    _validate_requested(allow, (("io:ANTH", date(2026, 10, 15)),))
    with pytest.raises(ValueError, match="TARGET_OUTSIDE_FROZEN"):
        _validate_requested(allow, (("io:ANTH", date(2026, 10, 14)),))
    with pytest.raises(ValueError, match="TARGET_OUTSIDE_FROZEN"):
        _validate_requested(allow, (("para:COHR", date(2026, 10, 15)),))
    with pytest.raises(ValueError, match="TARGET_OUTSIDE_FROZEN"):
        _validate_requested(allow, (("io:ANTH", date(2026, 10, 15)),) * 2)


def test_atomic_publish_never_overwrites_existing_partition(tmp_path):
    path = tmp_path / "year=2026/month=10/day=15/data.parquet"
    raw = b"valid immutable data"
    metadata = b'{"sha256":"example"}'
    _publish_missing_only(path, raw, metadata)
    assert path.read_bytes() == raw
    assert path.with_name("source.json").read_bytes() == metadata
    with pytest.raises(FileExistsError, match="EXISTS"):
        _publish_missing_only(path, b"modified", b"modified metadata")
    assert path.read_bytes() == raw


def test_synthetic_polars_1m_end_to_end_and_no_cross_dex(tmp_path):
    pl = pytest.importorskip("polars")
    pd = pytest.importorskip("pandas")

    day = date(2026, 10, 15)
    start = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
    values = [start + timedelta(minutes=i) for i in range(1440)]

    def builder(raw, symbol, d):
        assert set(raw["symbol"].unique()) == {"io:ANTH"}
        assert symbol == "ANTH" and d == day
        bars = pl.DataFrame(
            {
                "timestamp": values,
                "symbol": ["ANTH"] * 1440,
                "open": [100.0] * 1440,
                "high": [101.0] * 1440,
                "low": [99.0] * 1440,
                "close": [100.0] * 1440,
                "volume": [8.0] * 1440,
            }
        )
        return bars, {
            "trades": 1440,
            "duplicates_removed": 0,
            "observed_trade_hours": 24,
            "zero_volume_minutes": 0,
        }

    raw = pd.DataFrame({"symbol": ["io:ANTH"]})
    result = _materialize_trade_day(
        market_root=tmp_path, symbol="io:ANTH", day=day, raw=raw, builder=builder
    )
    assert result["status"] == "PUBLISHED_VERIFIED_NATIVE_HIP3_1M"
    path = _market_path(tmp_path, "io:ANTH", day)
    meta = json.loads(path.with_name("source.json").read_text())
    assert sha256(path.read_bytes()).hexdigest() == meta["sha256"]
    assert meta["source_symbol"] == "io:ANTH"
    instrument = "hyperliquid:mainnet:perp:io:ANTH"
    assert VerifiedHyperliquidMinuteSource(tmp_path).get(instrument, start).open == 100
    with pytest.raises(FileExistsError):
        _materialize_trade_day(
            market_root=tmp_path, symbol="io:ANTH", day=day, raw=raw, builder=builder
        )
    wrong = pd.DataFrame({"symbol": ["xyz:ANTH"]})
    with pytest.raises(ValueError, match="SOURCE_CHD_NAMESPACE_IDENTITY"):
        _materialize_trade_day(
            market_root=tmp_path, symbol="para:ANTH", day=day, raw=wrong, builder=builder
        )
