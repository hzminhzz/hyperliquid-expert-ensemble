"""Bounded CHD 1-minute materialization for verified non-xyz Hyperliquid HIP-3 DEXes.

Uses the existing ml4t-data trade-to-OHLCV builder (NOT a duplicate
resampler). Writes only previously absent, provenance-hashed canonical
namespace-specific partitions; it never overwrites unrelated or dirty data.

Authorized scope: native io:/para: futures market trades, research only.
No wallet fills, orders, account keys, or production service changes.
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

from .pm_market_audit import _audit_partition
from .pm_research import _write_private_evidence
from .pm_shadow import MarketDataBlocked, VerifiedHyperliquidMinuteSource


CHD_EXCHANGE = "hyperliquid_futures"
NAMESPACES = frozenset({"io", "para"})
MAX_DAYS = 5


def _parse_target(value: str) -> tuple[str, date]:
    if "@" not in value:
        raise ValueError("EXPECTED_HIP3_SYMBOL_AT_UTC_DATE")
    symbol, raw_day = value.rsplit("@", 1)
    if symbol.count(":") != 1:
        raise ValueError("INVALID_HIP3_SYMBOL_NAMESPACE")
    dex, ticker = symbol.split(":")
    if dex not in NAMESPACES or not ticker or not ticker.isascii():
        raise ValueError("UNAUTHORIZED_OR_INVALID_HIP3_SYMBOL")
    # This validates both namespace and ticker against our strict path contract.
    try:
        VerifiedHyperliquidMinuteSource._family(f"hyperliquid:mainnet:perp:{dex}:{ticker}")
    except MarketDataBlocked as exc:
        raise ValueError("UNSAFE_HIP3_TICKER") from exc
    return symbol, date.fromisoformat(raw_day)


def _market_path(root: Path, symbol: str, day: date) -> Path:
    dex, ticker = symbol.split(":")
    family = VerifiedHyperliquidMinuteSource._family(f"hyperliquid:mainnet:perp:{dex}:{ticker}")
    return (
        root
        / family
        / f"year={day.year:04d}"
        / f"month={day.month:02d}"
        / f"day={day.day:02d}"
        / "data.parquet"
    )


def _publish_missing_only(path: Path, data: bytes, metadata: bytes) -> None:
    """Two independent no-overwrite file publishes; fail closed on partial publish.

    The SHA in metadata cryptographically binds the data. Readers reject
    incomplete pairs, and this writer refuses to repair them silently.
    """
    sidecar = path.with_name("source.json")
    if path.exists() or sidecar.exists():
        raise FileExistsError("MARKET_PARTITION_ALREADY_EXISTS")
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".pm01-hip3-", suffix=".parquet", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() or sidecar.exists():
            raise FileExistsError("MARKET_PARTITION_PUBLISHED_CONCURRENTLY")
        os.link(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    fd, temp_meta = tempfile.mkstemp(prefix=".pm01-hip3-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(metadata)
            handle.flush()
            os.fsync(handle.fileno())
        if sidecar.exists():
            raise FileExistsError("MARKET_SIDECAR_PUBLISHED_CONCURRENTLY")
        os.link(temp_meta, sidecar)
    finally:
        if os.path.exists(temp_meta):
            os.unlink(temp_meta)


def _validate_requested(audit: dict, targets: tuple[tuple[str, date], ...]) -> None:
    if (
        audit.get("schema") != "pm01_native_1m_36_wallet_coverage_v1"
        or audit.get("wallet_count") != 36
    ):
        raise ValueError("UNVERIFIED_36_WALLET_MARKET_AUDIT")
    allowlist = {
        (row["provider_symbol"], date.fromisoformat(row["date_utc"]))
        for row in audit["missing_or_invalid_instrument_day_queue"]
        if row["reason"] == "MISSING_NATIVE_MARKET_DAY"
    }
    if len(set(targets)) != len(targets) or any(t not in allowlist for t in targets):
        raise ValueError("TARGET_OUTSIDE_FROZEN_MARKET_ACQUISITION_QUEUE")


def _materialize_trade_day(
    *,
    market_root: Path,
    symbol: str,
    day: date,
    raw: Any,
    builder: Any,
) -> dict:
    path = _market_path(market_root, symbol, day)
    if path.exists() or path.with_name("source.json").exists():
        raise FileExistsError("EXISTING_MARKET_PARTITION_NOT_OVERWRITABLE")
    if raw is None or raw.empty:
        return {"status": "NO_CHD_TRADES", "source": symbol, "day": str(day)}
    if "symbol" not in raw.columns or set(raw["symbol"].dropna().unique()) != {symbol}:
        raise ValueError("SOURCE_CHD_NAMESPACE_IDENTITY_MISMATCH")
    ticker = symbol.split(":", 1)[1]
    bars, stats = builder(raw, ticker, day)
    import polars as pl

    if bars.height != 1440:
        raise ValueError("NOT_COMPLETE_1440_MINUTE_GRID")
    bars = bars.with_columns(pl.col("timestamp").cast(pl.Datetime("ms", "UTC"))).select(
        "timestamp", "symbol", "open", "high", "low", "close", "volume"
    )
    if bars.get_column("symbol").unique().to_list() != [ticker]:
        raise ValueError("WRONG_1M_NAMESPACE_ROW_ID")
    # Build the Parquet in memory so no partial file exists before validation.
    from io import BytesIO

    stream = BytesIO()
    bars.write_parquet(stream, compression="zstd")
    data = stream.getvalue()
    digest = sha256(data).hexdigest()
    meta = {
        "schema": "pm01_namespaced_hip3_chd_trades_1m_v1",
        "status": "fetched_from_chd_trades",
        "provider": "CryptoHFTData",
        "exchange": CHD_EXCHANGE,
        "source_type": "trades",
        "source_symbol": symbol,
        "date_utc": str(day),
        "output_type": "1m_OHLCV",
        "columns": bars.columns,
        "bars": bars.height,
        "sha256": digest,
        "zero_volume_bar_rule": (
            "prior same-day trade close forward-filled; zero volume never deemed executable"
        ),
        "account_fills_retrieved": False,
        "source_fill_first_known_at": "UNKNOWN_NOT_A_WALLET_SOURCE",
        **stats,
    }
    payload = (json.dumps(meta, sort_keys=True, indent=2) + "\n").encode()
    _publish_missing_only(path, data, payload)
    check = _audit_partition(
        market_root,
        f"hyperliquid:mainnet:perp:{symbol}",
        datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC),
    )
    if check != "SOURCE_DAY_SHA_VERIFIED":
        raise ValueError("POST_PUBLISH_HIP3_MARKET_INTEGRITY_FAILED:" + check)
    return {
        "status": "PUBLISHED_VERIFIED_NATIVE_HIP3_1M",
        "source_symbol": symbol,
        "date_utc": str(day),
        "bars": bars.height,
        "sha256": digest,
        "trades": stats["trades"],
        "zero_volume_minutes": stats["zero_volume_minutes"],
    }


def fetch(
    *,
    ml4t_root: Path,
    market_audit: Path,
    targets: tuple[tuple[str, date], ...],
    max_days: int = 3,
) -> dict:
    if not 1 <= max_days <= MAX_DAYS or not targets or len(targets) > max_days:
        raise ValueError("CHD_HIP3_REQUEST_LIMIT_EXCEEDED")
    frozen = json.loads(market_audit.read_text(encoding="utf-8"))
    _validate_requested(frozen, targets)
    market_root = ml4t_root / ".data/store"
    data_repo = str(ml4t_root.resolve())
    previous_cwd = Path.cwd()
    sys.path.insert(0, data_repo)
    records: list[dict] = []
    try:
        os.chdir(data_repo)
        import cryptohftdata as chd

        key_loader = importlib.import_module("scripts.fetch_hyperliquid_multi_hftcryptodata")
        builder = importlib.import_module("scripts.build_hyperliquid_rwa_1m_from_chd").build
        token = key_loader.load_api_key()
        client = chd.CryptoHFTDataClient(api_key=token, timeout=90, max_retries=2)
        for symbol, day in targets:
            dest = _market_path(market_root, symbol, day)
            if dest.exists() or dest.with_name("source.json").exists():
                records.append(
                    {
                        "source_symbol": symbol,
                        "date_utc": str(day),
                        "status": (
                            "REUSED_EXISTING_SHA_VERIFIED"
                            if _audit_partition(
                                market_root,
                                "hyperliquid:mainnet:perp:" + symbol,
                                datetime(day.year, day.month, day.day, tzinfo=UTC),
                            )
                            == "SOURCE_DAY_SHA_VERIFIED"
                            else "BLOCKED_EXISTING_INVALID_PARTITION"
                        ),
                        "canonical_partition_modified": False,
                    }
                )
                continue
            try:
                raw = client.get_trades(
                    symbol=symbol,
                    exchange=chd.exchanges.HYPERLIQUID_FUTURES,
                    start_date=str(day),
                    end_date=str(day),
                    max_workers=2,
                )
                entry = _materialize_trade_day(
                    market_root=market_root, symbol=symbol, day=day, raw=raw, builder=builder
                )
                records.append(entry)
            except Exception as exc:
                # No credentials, request headers or client payloads in logs.
                records.append(
                    {
                        "source_symbol": symbol,
                        "date_utc": str(day),
                        "status": "BLOCKED_CHD_HIP3_SOURCE_OR_WRITER",
                        "error_type": type(exc).__name__,
                    }
                )
            time.sleep(1)
    finally:
        os.chdir(previous_cwd)
        sys.path.remove(data_repo)
    return {
        "schema": "pm01_chd_namespaced_hip3_1m_acquisition_v1",
        "source": "CryptoHFTData SDK hyperliquid_futures CommonTrade",
        "builder": "ml4t-data/scripts/build_hyperliquid_rwa_1m_from_chd.py",
        "source_market_audit_sha256": sha256(market_audit.read_bytes()).hexdigest(),
        "requests_bounded_at": max_days,
        "wallet_fills_untouched": True,
        "canonical_storage": "ml4t-data/.data/store",
        "results": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ml4t-root", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--target", action="append", required=True)
    parser.add_argument("--max-days", type=int, default=3)
    args = parser.parse_args()
    items = tuple(_parse_target(x) for x in args.target)
    report = fetch(
        ml4t_root=args.ml4t_root,
        market_audit=args.audit,
        targets=items,
        max_days=args.max_days,
    )
    out, digest = _write_private_evidence(args.out, report)
    print(
        json.dumps(
            {
                "results": report["results"],
                "evidence_sha256": digest,
                "evidence_file": str(out),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
