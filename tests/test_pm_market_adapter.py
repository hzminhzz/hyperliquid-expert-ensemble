"""Native 1-minute Hyperliquid market adapter integrity tests.

These tests need Polars; skip only when the lightweight Python environment
does not ship the optional market-data engine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

import pytest

from ensemble.pm_shadow import MarketDataBlocked, VerifiedHyperliquidMinuteSource

pl = pytest.importorskip("polars")


START = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
BTC = "hyperliquid:mainnet:perp:default:BTC"
NVDA = "hyperliquid:mainnet:perp:xyz:NVDA"


def _fixture(root: Path, instrument: str, *, invalid=False, duplicates=False):
    family = VerifiedHyperliquidMinuteSource._family(instrument)
    folder = root / family / "year=2026/month=10/day=20"
    folder.mkdir(parents=True, exist_ok=True)
    sym = instrument.rsplit(":", 1)[-1]
    rows = [
        {
            "timestamp": START + timedelta(minutes=i),
            "symbol": sym,
            "open": 100.0 + i,
            "high": 102.0 + i,
            "low": 99.0 + i,
            "close": 101.0 + i,
            "volume": 10.0,
        }
        for i in range(3)
    ]
    if invalid:
        rows[1]["low"] = 200.0
    if duplicates:
        rows[-1]["timestamp"] = rows[-2]["timestamp"]
    data = folder / "data.parquet"
    pl.DataFrame(rows).write_parquet(data)
    source = {
        "sha256": sha256(data.read_bytes()).hexdigest(),
        "exchange": "hyperliquid_futures",
        "source_type": "trades",
        "source_symbol": (
            instrument.split(":")[3] + ":" + sym if instrument.split(":")[3] != "default" else sym
        ),
    }
    (folder / "source.json").write_text(json.dumps(source))
    return folder


def test_verified_native_open_at_next_minute_after_signal(tmp_path):
    _fixture(tmp_path, BTC)
    market = VerifiedHyperliquidMinuteSource(tmp_path)
    bar = market.get(BTC, START + timedelta(seconds=35))
    assert bar.timestamp == START + timedelta(minutes=1)
    assert bar.open == 101
    assert bar.quote_volume == 1010
    assert len(market.source_days) == 1
    assert market.liquidity_proxy_days == {BTC + "@2026-10-20"}
    with pytest.raises(MarketDataBlocked, match="MISSING_EXACT_1M_BAR"):
        market.get(BTC, START + timedelta(minutes=5))


def test_verified_hip3_meta_requires_xyz_symbol_namespace(tmp_path):
    folder = _fixture(tmp_path, NVDA)
    market = VerifiedHyperliquidMinuteSource(tmp_path)
    assert market.get(NVDA, START).open == 100
    source = json.loads((folder / "source.json").read_text())
    source["source_symbol"] = "NVDA"  # wrong native market namespace
    (folder / "source.json").write_text(json.dumps(source))
    with pytest.raises(MarketDataBlocked, match="WRONG_MARKET_SYMBOL"):
        VerifiedHyperliquidMinuteSource(tmp_path).get(NVDA, START)


def test_invalid_ohlc_and_duplicate_bars_fail_closed(tmp_path):
    _fixture(tmp_path, BTC, invalid=True)
    with pytest.raises(MarketDataBlocked, match="INVALID_NATIVE_1M_OHLCV"):
        VerifiedHyperliquidMinuteSource(tmp_path).get(BTC, START)
    _fixture(tmp_path, BTC, duplicates=True)
    with pytest.raises(MarketDataBlocked, match="DUPLICATE_OR_MALFORMED_1M_BARS"):
        VerifiedHyperliquidMinuteSource(tmp_path).get(BTC, START)


def test_corrupted_native_parquet_never_used(tmp_path):
    folder = _fixture(tmp_path, BTC)
    with (folder / "data.parquet").open("ab") as f:
        f.write(b"changed source")
    with pytest.raises(MarketDataBlocked, match="MARKET_PARQUET_HASH_MISMATCH"):
        VerifiedHyperliquidMinuteSource(tmp_path).get(BTC, START)


def test_valid_case_sensitive_symbol_and_isolated_hip3_dex():
    assert VerifiedHyperliquidMinuteSource._family(
        "hyperliquid:mainnet:perp:default:kPEPE"
    ) == Path("crypto_futures_ohlcv_1m_HYPERLIQUID_kPEPE_PERP")
    assert VerifiedHyperliquidMinuteSource._family("hyperliquid:mainnet:perp:io:ANTH") == Path(
        "crypto_futures_ohlcv_1m_HYPERLIQUID_IO_PERP/symbol=ANTH"
    )
    assert VerifiedHyperliquidMinuteSource._family("hyperliquid:mainnet:perp:para:VST") == Path(
        "crypto_futures_ohlcv_1m_HYPERLIQUID_PARA_PERP/symbol=VST"
    )
    assert VerifiedHyperliquidMinuteSource._family(
        "hyperliquid:mainnet:perp:xyz:ANTH"
    ) != VerifiedHyperliquidMinuteSource._family("hyperliquid:mainnet:perp:io:ANTH")
    with pytest.raises(MarketDataBlocked, match="UNSUPPORTED_HIP3_DEX"):
        VerifiedHyperliquidMinuteSource._family("hyperliquid:mainnet:perp:other:ANTH")


def test_native_io_venue_identity_attested(tmp_path):
    instrument = "hyperliquid:mainnet:perp:io:ANTH"
    folder = _fixture(tmp_path, instrument)
    assert VerifiedHyperliquidMinuteSource(tmp_path).get(instrument, START).open == 100
    meta = json.loads((folder / "source.json").read_text())
    meta["source_symbol"] = "para:ANTH"
    (folder / "source.json").write_text(json.dumps(meta))
    with pytest.raises(MarketDataBlocked, match="WRONG_MARKET_SYMBOL"):
        VerifiedHyperliquidMinuteSource(tmp_path).get(instrument, START)
