"""Bounded CHD 1-minute Hyperliquid acquisition using ml4t-data's own writer.

This is NOT wallet fills. The authoritative canonical writer lives in
ml4t-data; no duplicate portfolio-owned market store is created.
Never overwrites preexisting partitions. Fail-close on missing CHD coverage.
"""

from __future__ import annotations

import argparse
from datetime import date
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
import time

from .pm_research import _write_private_evidence


def _day_grid_quality(meta: dict) -> dict:
    """Parquet SHA verifies bytes, not a complete/executable 1m day."""
    bars = meta.get("bars")
    if type(bars) is not int or not 0 < bars <= 1440:
        return {
            "bar_count": bars if type(bars) is int else None,
            "full_utc_minute_grid": None,
            "missing_utc_minutes": None,
            "market_capacity_verified": False,
        }
    empty = meta.get("zero_volume_minutes")
    return {
        "bar_count": bars,
        "full_utc_minute_grid": bars == 1440,
        "missing_utc_minutes": 1440 - bars,
        "zero_volume_minutes": empty if type(empty) is int and empty >= 0 else None,
        "market_capacity_verified": False,
    }


def fetch_days(
    ml4t_root: Path,
    audit: Path,
    targets: tuple[tuple[str, str], ...],
    max_days: int = 3,
    mode: str = "candles",
) -> dict:
    if not 1 <= max_days <= 5 or not targets or len(targets) > max_days:
        raise ValueError("CHD_REQUEST_BUDGET_EXCEEDED")
    if mode not in {"candles", "trades"}:
        raise ValueError("UNRECOGNIZED_CHD_DATA_MODE")
    ml4t_root = ml4t_root.resolve()
    market_root = ml4t_root / ".data/store"
    report = json.loads(audit.read_text())
    if report.get("schema") != "pm01_native_1m_36_wallet_coverage_v1":
        raise ValueError("NOT_VERIFIED_PM01_36_WALLET_MARKET_AUDIT")
    permitted = {
        (row["provider_symbol"], row["date_utc"])
        for row in report["missing_or_invalid_instrument_day_queue"]
        if row["reason"] == "MISSING_NATIVE_MARKET_DAY"
    }
    if len(set(targets)) != len(targets) or any(x not in permitted for x in targets):
        raise ValueError("TARGET_NOT_IN_SHA_AUDITED_MISSING_DAY_QUEUE")
    previous = Path.cwd()
    sys.path.insert(0, str(ml4t_root))
    try:
        os.chdir(ml4t_root)
        import cryptohftdata as chd

        writer = importlib.import_module("scripts.fetch_hyperliquid_ohlcv_candles_from_chd")
        trade_writer = (
            importlib.import_module("scripts.build_hyperliquid_1m_perps_from_chd")
            if mode == "trades"
            else None
        )
        creds = importlib.import_module("scripts.fetch_hyperliquid_multi_hftcryptodata")
        key = creds.load_api_key()
        client = chd.CryptoHFTDataClient(api_key=key, timeout=45, max_retries=2)
        verified = []
        for symbol, day_text in targets:
            day = date.fromisoformat(day_text)
            dest = writer.partition(market_root, symbol, day)
            source = dest.with_name("source.json")
            if dest.exists() or source.exists():
                # No rewrite or repair of any pre-existing canonical partition.
                status = "BLOCKED_ALREADY_PRESENT"
                quality = {}
                if dest.is_file() and source.is_file():
                    meta = json.loads(source.read_text())
                    if (
                        meta.get("sha256") == sha256(dest.read_bytes()).hexdigest()
                        and meta.get("exchange") == "hyperliquid_futures"
                        and meta.get("source_symbol") == symbol
                    ):
                        status = "REUSED_SHA_VERIFIED_EXISTING"
                        quality = _day_grid_quality(meta)
                verified.append(
                    {
                        "symbol": symbol,
                        "day": day_text,
                        "status": status,
                        "canonical_partition_modified": False,
                        **quality,
                    }
                )
                continue
            try:
                result = (
                    writer.fetch_symbol(client, symbol, day, day, market_root)
                    if mode == "candles"
                    else trade_writer.build_day(symbol, day, False, key, api_workers=2)
                )
                if result["status"] not in {
                    "saved",
                    "reused",
                    "fetched_from_chd_trades",
                    "rebuilt_from_local_chd_trades",
                }:
                    verified.append(
                        {
                            "symbol": symbol,
                            "day": day_text,
                            "status": "CHD_" + result["status"].upper(),
                            "canonical_partition_modified": False,
                        }
                    )
                    continue
                if not dest.is_file() or not source.is_file():
                    raise ValueError("CHD_WRITER_FAILED_TO_PUBLISH_COMPLETE_PAIR")
                meta = json.loads(source.read_text())
                digest = sha256(dest.read_bytes()).hexdigest()
                if (
                    meta.get("sha256") != digest
                    or meta.get("exchange") != "hyperliquid_futures"
                    or meta.get("source_symbol") != symbol
                    or meta.get("source_type") != mode
                    or meta.get("provider") != "CryptoHFTData"
                ):
                    raise ValueError("CHD_SOURCE_PROVENANCE_CONFLICT")
                quality = _day_grid_quality(meta)
                verified.append(
                    {
                        "symbol": symbol,
                        "day": day_text,
                        "status": (
                            "WRITER_PUBLISHED_SHA_VERIFIED"
                            if quality["full_utc_minute_grid"] is True
                            else "WRITER_PUBLISHED_SHA_VERIFIED_PARTIAL_OR_UNKNOWN_GRID"
                        ),
                        "sha256": digest,
                        "source_type": meta["source_type"],
                        "canonical_partition_modified": True,
                        **quality,
                    }
                )
            except Exception as ex:
                # The SDK exception may contain request/transport detail.
                # Never persist credentials, authorization URLs, or headers.
                verified.append(
                    {
                        "symbol": symbol,
                        "day": day_text,
                        "status": "CHD_SOURCE_BLOCKED",
                        "error_type": type(ex).__name__,
                        "canonical_partition_modified": dest.exists(),
                    }
                )
            time.sleep(1.5)
    finally:
        os.chdir(previous)
        sys.path.remove(str(ml4t_root))
    return {
        "schema": "pm01_chd_targeted_native_1m_fetch_v1",
        "writer": (
            "ml4t-data/scripts/build_hyperliquid_1m_perps_from_chd.py"
            if mode == "trades"
            else "ml4t-data/scripts/fetch_hyperliquid_ohlcv_candles_from_chd.py"
        ),
        "mode": mode,
        "source": "CryptoHFTData SDK 0.8.0",
        "wallet_source_fills_untouched": True,
        "owner": "ml4t-data canonical storage",
        "planned_max_day_requests": max_days,
        "targets": verified,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ml4t-root", type=Path, required=True)
    p.add_argument("--audit", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--target", action="append", required=True)
    p.add_argument("--max-days", type=int, default=3)
    p.add_argument("--mode", choices=["candles", "trades"], default="candles")
    args = p.parse_args()
    pairs = []
    for record in args.target:
        if "@" not in record:
            raise ValueError("EXPECTED_SYMBOL_AT_UTC_DATE")
        sym, day = record.split("@", 1)
        if not sym or not day:
            raise ValueError("INVALID_SOURCE_TARGET")
        pairs.append((sym, day))
    report = fetch_days(args.ml4t_root, args.audit, tuple(pairs), args.max_days, args.mode)
    evidence, hash_value = _write_private_evidence(args.out, report)
    print(
        json.dumps(
            {
                "results": report["targets"],
                "sha256": hash_value,
                "private_evidence": str(evidence),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
