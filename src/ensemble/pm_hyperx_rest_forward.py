"""Bounded, read-only HyperX REST fill receipts with local first-observed clocks.

One response is not an account snapshot, and a vendor fill event timestamp
is never the first-known timestamp. REST received_at is the upper bound on
when *this* observer first saw the event. This can accumulate future research
evidence without requiring the vendor's multi-wallet WebSocket tier, but it
cannot recover genuine prospective knowledge for past events.

No trading API, live orders, production Telegram, Rust or provider override.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import time

from .pm_fill_replay import _coins
from .pm_hyperx_gap_recovery import _adapter, _atomic, _private_dir
from .pm_hyperx_overlay_audit import _source_tuple
from .pm_hyperx_forward import store_first_local_receipt
from .pm_sources import load_cohort
from .pm_research import _write_private_evidence

MAX_REQUESTS = 5
MIN_INTERVAL_SECONDS = 12.0
RECENT_WINDOW = timedelta(minutes=60)


def parse_pairs(root: Path, pairs: tuple[str, ...]) -> tuple[list, dict]:
    """Reject any unknown identity, market, duplicate or oversized batch."""
    if not 1 <= len(pairs) <= MAX_REQUESTS or len(set(pairs)) != len(pairs):
        raise ValueError("UNBOUNDED_OR_DUPLICATED_REST_FILL_POLL")
    entries, fingerprints = load_cohort(root)
    names = {entry.alias: entry for entry in entries}
    checked = []
    for spec in pairs:
        alias, sep, coin = spec.partition("/")
        if not sep or alias not in names or coin not in _coins(root, alias):
            raise ValueError("REST_SOURCE_PAIR_NOT_IN_FROZEN_VENDOR_ROSTER")
        checked.append((names[alias], coin))
    return checked, fingerprints


def _recent_observation(
    row: dict,
    entry,
    coin: str,
    *,
    requested_at: datetime,
    received_at: datetime,
) -> dict | None:
    """Return only *newly observed* recent fills, never retroactive signals."""
    event = _source_tuple(row, entry.alias, coin, entry.address)
    when, tid, _, before, _, signed_qty, px = event
    # Source clock skew larger than 5 seconds is not a causal event.
    if when > received_at + timedelta(seconds=5):
        raise ValueError("HYPERX_FILL_EVENT_FUTURE_OF_OBSERVER")
    if when < requested_at - RECENT_WINDOW:
        return None
    age = received_at - when
    status = (
        "SOURCE_CLOCK_SKEW_OR_NONCAUSAL"
        if age < timedelta(seconds=-5)
        else "REST_RECENT_FILL_REQUIRES_BOOK_AND_EXECUTION_RECONCILIATION"
    )
    return {
        "schema": "pm01_hyperx_first_local_receipt_v1",
        "wallet": entry.alias,
        "source_identity_sha256": sha256(entry.address.encode()).hexdigest(),
        "source_market": coin,
        "source_trade_id": str(tid),
        "source_event_at": when.isoformat(),
        "locally_received_at": received_at.isoformat(),
        "rest_request_started_at": requested_at.isoformat(),
        "stream_source": "HYPERX_DATA_API_REST_PAGED_NOT_WEBSOCKET",
        "source_fill_price": str(px),
        "source_fill_quantity": str(abs(signed_qty)),
        "source_position_before": str(before),
        "source_side": "B" if signed_qty > 0 else "A",
        "source_snapshot": True,
        "source_age_seconds": round(age.total_seconds(), 3),
        "coverage": status,
        "source_first_known_at": None,
        "whole_account_reconciled": False,
        "source_instrument_roster_verified_complete": False,
        "source_collateral_and_funding_verified": False,
        "actual_follower_fills": None,
        "real_follower_order_sent": False,
        "auto_promotion_authorized": False,
    }


def ingest_batch(
    root: Path,
    destination: Path,
    requests: tuple[str, ...],
    *,
    min_interval_seconds: float = MIN_INTERVAL_SECONDS,
) -> dict:
    if min_interval_seconds < MIN_INTERVAL_SECONDS:
        raise ValueError("HYPERX_SOURCE_RATE_LIMIT_UNSAFE")
    chosen, fingerprints = parse_pairs(root, requests)
    _private_dir(destination)
    provider = _adapter(root)
    token = provider.key_from_existing_tracker_env()
    observations = destination / "first_local_receipts"
    _private_dir(observations)
    records = []
    previous_call: float | None = None
    calls = 0

    for entry, coin in chosen:
        if previous_call is not None:
            time.sleep(max(0, min_interval_seconds - (time.monotonic() - previous_call)))
        requested_at = datetime.now(UTC)
        previous_call = time.monotonic()
        calls += 1
        try:
            data, raw, rate = provider.provider_request(entry.alias, entry.address, coin, 1, token)
            received_at = datetime.now(UTC)
            checked = provider.validate(data, entry.alias, coin, 1, entry.address)
            if not isinstance(data.get("has_more"), bool):
                raise ValueError("VENDOR_PAGINATION_SHAPE_NOT_ATTESTED")
            # One immutable private response generation per observed pair.
            # A second run uses a different destination, not an overwrite.
            from urllib.parse import quote

            folder = destination / "vendor_raw" / entry.alias / quote(coin, safe="")
            _private_dir(folder)
            sha = sha256(raw).hexdigest()
            _atomic(folder / f"page1-{sha[:20]}.json", raw)
            stored = 0
            for fill in data["fills"]:
                obs = _recent_observation(
                    fill, entry, coin, requested_at=requested_at, received_at=received_at
                )
                if obs and store_first_local_receipt(observations, obs):
                    stored += 1
            records.append(
                {
                    "wallet": entry.alias,
                    "coin": coin,
                    "checked_page": 1,
                    "raw_vendor_page_sha256": sha,
                    "vendor_rows": checked["rows"],
                    "vendor_has_more": data["has_more"],
                    "request_started_at": requested_at.isoformat(),
                    "first_local_response_received_at": received_at.isoformat(),
                    "recent_fill_receipts_new": stored,
                    "recent_receipt_not_prospective_qualification": True,
                    "total_vendor_pages_complete": not data["has_more"],
                    "whole_exchange_account_state_verified": False,
                    "source_first_known_at_attested": False,
                    "rate_budget_remaining": (
                        int(rate["x-ratelimit-remaining"])
                        if rate.get("x-ratelimit-remaining", "").isdigit()
                        else None
                    ),
                    "status": "OBSERVED_BOUNDED_VENDOR_PAGE_NO_TRADING",
                }
            )
            remaining = rate.get("x-ratelimit-remaining")
            if remaining is not None and str(remaining).isdigit() and int(remaining) < 10:
                break
        except Exception as exc:
            # Never persist raw URL or vendor exception text; both may contain
            # the wallet address or private transport request metadata.
            records.append(
                {
                    "wallet": entry.alias,
                    "coin": coin,
                    "status": "BLOCKED_VENDOR_OR_PAGINATION",
                    "error_type": type(exc).__name__,
                    "request_started_at": requested_at.isoformat(),
                }
            )
            break
    return {
        "schema": "pm01_bounded_hyperx_rest_first_observed_v1",
        "source": "HYPERX_AUTHENTICATED_DATA_API_REST_ONLY",
        "cohort_manifest_shas": fingerprints,
        "total_cohort_wallets": 36,
        "attempted_requests": calls,
        "max_request_weight": calls * 5,
        "no_broker_or_trading_api": True,
        "never_promote_hindsight_to_prospective": True,
        "all_wallets_prospective_qualified": False,
        "no_complete_account_snapshot_claim": True,
        "results": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-root", type=Path, required=True)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pairs", nargs="+", required=True)
    parser.add_argument("--min-interval", type=float, default=MIN_INTERVAL_SECONDS)
    args = parser.parse_args()
    if not re.fullmatch(r"g[a-zA-Z0-9_-]{8,64}", args.generation.name):
        raise ValueError("INVALID_PRIVATE_REST_POLL_GENERATION")
    result = ingest_batch(
        args.research_root,
        args.generation,
        tuple(args.pairs),
        min_interval_seconds=args.min_interval,
    )
    evidence, digest = _write_private_evidence(args.out, result)
    print(
        json.dumps(
            {
                "requests": result["attempted_requests"],
                "cohort_wallets": result["total_cohort_wallets"],
                "receipts_new": sum(
                    x.get("recent_fill_receipts_new", 0) for x in result["results"]
                ),
                "statuses": [
                    {k: x[k] for k in ("wallet", "coin", "status")} for x in result["results"]
                ],
                "prospective_qualified": False,
                "sha256": digest,
                "private_evidence": str(evidence),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
