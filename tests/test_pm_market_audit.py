"""Read-only 1m native-source coverage contracts; no provider network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json

from ensemble.pm_market_audit import _audit_partition, _audit_clocks, LATENCY_MINUTES
from ensemble.pm_shadow import VerifiedHyperliquidMinuteSource

BTC = "hyperliquid:mainnet:perp:default:BTC"
NVDA = "hyperliquid:mainnet:perp:xyz:NVDA"
AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _day(tmp_path, instrument):
    folder = (
        tmp_path / VerifiedHyperliquidMinuteSource._family(instrument) / "year=2026/month=10/day=20"
    )
    folder.mkdir(parents=True)
    payload = b"verified native parquet bytes for independent partition audit"
    (folder / "data.parquet").write_bytes(payload)
    dex, coin = instrument.split(":")[3:5]
    meta = {
        "exchange": "hyperliquid_futures",
        "source_type": "trades",
        "source_symbol": dex + ":" + coin if dex != "default" else coin,
        "sha256": sha256(payload).hexdigest(),
    }
    (folder / "source.json").write_text(json.dumps(meta))
    return folder


def test_market_audit_includes_5m_15m_60m_and_terminal_boundary():
    assert LATENCY_MINUTES == (5, 15, 60)
    cut = AT + timedelta(minutes=100)
    assert _audit_clocks(AT, cut) == (
        AT + timedelta(minutes=5),
        AT + timedelta(minutes=15),
        AT + timedelta(minutes=60),
    )
    near_cut = cut - timedelta(minutes=10)
    assert _audit_clocks(near_cut, cut) == (near_cut + timedelta(minutes=5),)


def test_chd_negative_coverage_metadata_is_not_silent_market_data(tmp_path):
    family = VerifiedHyperliquidMinuteSource._family(BTC)
    folder = tmp_path / family / "year=2026/month=10/day=20"
    folder.mkdir(parents=True)
    (folder / "source.json").write_text(
        json.dumps(
            {
                "status": "no_source_data",
                "provider": "CryptoHFTData",
                "exchange": "hyperliquid_futures",
                "source_symbol": "BTC",
            }
        )
    )
    assert _audit_partition(tmp_path, BTC, AT) == "CHD_CONFIRMED_NO_TRADE_SOURCE"
    (folder / "source.json").write_text(
        json.dumps(
            {
                "status": "no_source_data",
                "provider": "OtherVenue",
                "exchange": "binance",
                "source_symbol": "BTC",
            }
        )
    )
    assert _audit_partition(tmp_path, BTC, AT) == "INCOMPLETE_MARKET_DAY_SOURCE_PAIR"


def test_native_market_day_missing_and_sha_verified(tmp_path):
    assert _audit_partition(tmp_path, BTC, AT) == "MISSING_NATIVE_MARKET_DAY"
    _day(tmp_path, BTC)
    assert _audit_partition(tmp_path, BTC, AT) == "SOURCE_DAY_SHA_VERIFIED"


def test_no_silent_repair_or_cross_market_substitution(tmp_path):
    folder = _day(tmp_path, BTC)
    (folder / "data.parquet").write_bytes(b"modified bytes")
    assert _audit_partition(tmp_path, BTC, AT) == "SOURCE_SHA_MISMATCH"
    (folder / "source.json").write_text(
        json.dumps(
            {
                "exchange": "binance",
                "source_symbol": "BTC",
                "sha256": sha256(b"modified bytes").hexdigest(),
            }
        )
    )
    assert _audit_partition(tmp_path, BTC, AT) == "WRONG_NATIVE_MARKET_VENUE"


def test_native_para_and_io_sources_use_distinct_partitions(tmp_path):
    io = "hyperliquid:mainnet:perp:io:ANTH"
    para = "hyperliquid:mainnet:perp:para:ANTH"
    io_folder = _day(tmp_path, io)
    para_folder = _day(tmp_path, para)
    assert io_folder != para_folder
    assert _audit_partition(tmp_path, io, AT) == "SOURCE_DAY_SHA_VERIFIED"
    assert _audit_partition(tmp_path, para, AT) == "SOURCE_DAY_SHA_VERIFIED"
    meta = json.loads((io_folder / "source.json").read_text())
    meta["source_symbol"] = "xyz:ANTH"
    (io_folder / "source.json").write_text(json.dumps(meta))
    assert _audit_partition(tmp_path, io, AT) == "WRONG_NATIVE_MARKET_SYMBOL"
    assert _audit_partition(tmp_path, para, AT) == "SOURCE_DAY_SHA_VERIFIED"


def test_native_hip3_requires_exact_namespace(tmp_path):
    folder = _day(tmp_path, NVDA)
    assert _audit_partition(tmp_path, NVDA, AT) == "SOURCE_DAY_SHA_VERIFIED"
    metadata = json.loads((folder / "source.json").read_text())
    metadata["source_symbol"] = "NVDA"
    (folder / "source.json").write_text(json.dumps(metadata))
    assert _audit_partition(tmp_path, NVDA, AT) == "WRONG_NATIVE_MARKET_SYMBOL"
