"""Read-only 1m native-source coverage contracts; no provider network."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json

from ensemble.pm_market_audit import _audit_partition
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
        "source_symbol": "xyz:" + coin if dex == "xyz" else coin,
        "sha256": sha256(payload).hexdigest(),
    }
    (folder / "source.json").write_text(json.dumps(meta))
    return folder


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


def test_native_hip3_requires_exact_namespace(tmp_path):
    folder = _day(tmp_path, NVDA)
    assert _audit_partition(tmp_path, NVDA, AT) == "SOURCE_DAY_SHA_VERIFIED"
    metadata = json.loads((folder / "source.json").read_text())
    metadata["source_symbol"] = "NVDA"
    (folder / "source.json").write_text(json.dumps(metadata))
    assert _audit_partition(tmp_path, NVDA, AT) == "WRONG_NATIVE_MARKET_SYMBOL"
