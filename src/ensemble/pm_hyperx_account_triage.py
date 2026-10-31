"""PM-01 HyperX account-state triangulation: bounded, private and non-causal.

The Data API wallet analysis, trader funding and margin timeline are separate
sources from authenticated fill history. This does NOT repair a missing fill,
manufacture an account position, or give a historical first-known timestamp.
The snapshots are vendor data acquired NOW, not an old exchange-account
journal. No trading credentials or live orders.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .pm_hyperx_gap_recovery import _adapter, _private_dir
from .pm_sources import load_cohort
from .pm_research import _write_private_evidence

BASE = "https://data-api.hyperx.trade"
ENDPOINTS = (
    ("wallet_analysis", "POST", "/wallet_analysis/"),
    ("trader_funding", "GET", "/market/trader-funding/"),
    ("trader_margin", "GET", "/market/trader-margin-timeline/"),
)
MAX_WALLETS = 2


def _verify_response(kind: str, body: dict, address: str) -> dict:
    if not isinstance(body, dict) or body.get("address", "").lower() != address.lower():
        raise ValueError("HYPERX_ACCOUNT_IDENTITY_MISMATCH")
    if kind == "wallet_analysis":
        leverage = body.get("users_historical_leverage")
        metrics = body.get("wallet_metrics")
        if not isinstance(leverage, list) or not isinstance(metrics, dict):
            raise ValueError("HYPERX_ACCOUNT_ANALYSIS_CONTRACT")
        if not isinstance(body.get("user_event_stats"), dict):
            raise ValueError("MISSING_ACCOUNT_ASSET_FLOW_HISTORY")
        return {
            "time_extent_claim": "VENDOR_ANALYSIS_GLOBAL_DATE_RANGE_NOT_HISTORY_ATTESTATION",
            "analysis_start_timestamp_ms": body.get("start_timestamp_ms"),
            "analysis_end_timestamp_ms": body.get("end_timestamp_ms"),
            "leverage_points": len(leverage),
            "positions_verified_at_cutoff": False,
            "historic_account_equity_reconciled": False,
            "available_metric_periods": sorted(metrics),
            "asset_flow_event_count": body["user_event_stats"].get("asset_flow_events_count"),
        }
    if kind == "trader_funding":
        records = body.get("records")
        if not isinstance(records, list) or not isinstance(body.get("has_more"), bool):
            raise ValueError("HYPERX_FUNDING_CONTRACT")
        observed = []
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("time"), str):
                raise ValueError("UNVERIFIED_FUNDING_RECORD_CLOCK")
            try:
                at = datetime.fromisoformat(record["time"].replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("UNVERIFIED_FUNDING_RECORD_CLOCK") from exc
            if at.tzinfo is None or at.utcoffset() is None:
                raise ValueError("UNVERIFIED_FUNDING_RECORD_CLOCK")
            observed.append(at.astimezone(UTC))
        return {
            "reported_days": body.get("days"),
            "rows_first_page": len(records),
            "first_page_oldest_utc": min(observed).isoformat() if observed else None,
            "first_page_newest_utc": max(observed).isoformat() if observed else None,
            "has_more_funding_pages": body["has_more"],
            "funding_complete_study": False,
            "funding_not_broker_executed": True,
        }
    if kind == "trader_margin":
        points = body.get("points")
        if not isinstance(points, list) or not isinstance(body.get("data_window_days"), int):
            raise ValueError("HYPERX_MARGIN_CONTRACT")
        return {
            "reported_days": body["data_window_days"],
            "points": len(points),
            "whole_study_margin_complete": False,
            "historical_first_known": False,
        }
    raise ValueError("UNSUPPORTED_ACCOUNT_DATA_ENDPOINT")


def _write_immutable_private(path: Path, payload: bytes) -> str:
    """Preserve original HyperX reply in a new O_EXCL private generation."""
    _private_dir(path.parent)
    if path.exists():
        raise ValueError("GENERATION_REUSE_NOT_AUTHORIZED")
    fd, tmp = tempfile.mkstemp(prefix=".pm01-account-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            raise ValueError("GENERATION_REUSE_NOT_AUTHORIZED")
        os.link(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return sha256(payload).hexdigest()


def acquire(root: Path, generation: Path, aliases: tuple[str, ...]) -> dict:
    if not 1 <= len(aliases) <= MAX_WALLETS or len(set(aliases)) != len(aliases):
        raise ValueError("BOUNDED_ACCOUNT_QUERY_MAX_TWO_WALLETS")
    if not re.fullmatch(r"g[a-zA-Z0-9_-]{8,50}", generation.name):
        raise ValueError("INVALID_ACCOUNT_GENERATION")
    entries, fingerprints = load_cohort(root)
    index = {e.alias: e for e in entries}
    if any(a not in index for a in aliases):
        raise ValueError("UNVERIFIED_SOURCE_WALLET")
    _private_dir(generation)
    import requests

    provider = _adapter(root)
    token = provider.key_from_existing_tracker_env()
    # At most 2 * (5 + 1 + 1) documented HyperX weight per invocation:
    # 14 total < free API's advertised 30/minute budget.
    report: dict[str, Any] = {
        "schema": "pm01_hyperx_account_triage_v1",
        "source": "HYPERX_DATA_API_ONLY",
        "rows": [],
        "authorized_wallet_count": len(aliases),
        "rate_weight_upper_bound": 14,
        "source_cohort_manifests_sha256": fingerprints,
        "wallet_fill_cache_mutated": False,
        "point_in_time_follower_qualified": False,
        "source_account_fully_reconciled": False,
    }
    session = requests.Session()
    session.headers.update({"X-API-Key": token})
    try:
        for alias in aliases:
            entry = index[alias]
            row = {
                "alias": alias,
                "identity_sha256": entry.address_sha256,
                "status": "PARTIAL_NONCAUSAL_DATA",
                "sources": {},
            }
            for kind, method, path in ENDPOINTS:
                response = session.request(
                    method,
                    BASE + path + entry.address,
                    json={} if method == "POST" else None,
                    timeout=(10, 30),
                )
                if response.status_code != 200:
                    row["status"] = "BLOCKED_VENDOR_ACCOUNT_SOURCE"
                    row["sources"][kind] = {
                        "http_status": response.status_code,
                        "sha256": None,
                    }
                    # Fail closed for this wallet; no more calls for it.
                    break
                raw = response.content
                if len(raw) > 5_000_000:
                    raise ValueError("OVERSIZED_HYPERX_ACCOUNT_RESPONSE")
                document = response.json()
                metadata = _verify_response(kind, document, entry.address)
                retrieved = datetime.now(UTC).isoformat()
                source_name = generation / alias / f"{kind}.json"
                signature = _write_immutable_private(source_name, raw)
                row["sources"][kind] = {
                    "source_sha256": signature,
                    "source_path_private": str(source_name),
                    "retrieved_at_utc": retrieved,
                    "metadata": metadata,
                    "first_known_historical_economic_fact": None,
                }
            report["rows"].append(row)
    finally:
        session.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-root", type=Path, required=True)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--aliases", nargs="+", required=True)
    args = parser.parse_args()
    report = acquire(
        args.research_root,
        args.generation,
        tuple(args.aliases),
    )
    private_path, digest = _write_private_evidence(args.out, report)
    print(
        json.dumps(
            {
                "wallets": len(report["rows"]),
                "account_fully_reconciled": report["source_account_fully_reconciled"],
                "rows": [
                    {
                        "alias": row["alias"],
                        "status": row["status"],
                        "sources": {k: v.get("metadata", {}) for k, v in row["sources"].items()},
                    }
                    for row in report["rows"]
                ],
                "evidence_sha256": digest,
                "evidence_file": str(private_path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
