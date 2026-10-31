"""Bounded research-only HyperX Data API WS receipt probe for forward PM-01.

Never a copy-trading or order client. Verifies event identity, records the
first *locally observed* receipt in owner-private immutable files and preserves
disconnection/snapshot uncertainty. A receipt alone is NOT a whole-account
snapshot or proof that a follower could execute.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
from urllib.parse import urlencode

from .pm_hyperx_gap_recovery import _adapter
from .pm_sources import load_cohort

ENDPOINT = "wss://data-api.hyperx.trade/fills/ws"
MAX_SECONDS = 60
MAX_MESSAGES = 5
MAX_WALLETS = 36
EVENT_FIELDS = ("coin", "time", "tid", "px", "sz", "side")


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_mode & 0o077:
        raise ValueError("NONPRIVATE_FORWARD_RECEIPT_DIRECTORY")


def extract_observations(
    raw: str,
    received_at: datetime,
    roster: dict[str, str],
    *,
    connected_at: datetime,
) -> list[dict]:
    """Strictly accept vendor-addressed fill events, not anonymous updates."""
    if received_at.tzinfo is None or connected_at.tzinfo is None or received_at < connected_at:
        raise ValueError("INVALID_OBSERVATION_CLOCK")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return []
    body = payload.get("data", payload)
    if not isinstance(body, dict):
        return []
    values = body.get("fills")
    if values is None and all(k in body for k in EVENT_FIELDS):
        values = [body]
    if not isinstance(values, list) or len(values) > 250:
        return []
    address = body.get("user") or body.get("address")
    snapshot = bool(body.get("isSnapshot") or payload.get("isSnapshot"))
    by_address = {addr.lower(): alias for alias, addr in roster.items()}
    result = []
    for event in values:
        if not isinstance(event, dict) or not all(k in event for k in EVENT_FIELDS):
            continue
        user = event.get("user") or event.get("address") or address
        if not isinstance(user, str) or user.lower() not in by_address:
            # A trade without a verified source wallet is never eligible.
            continue
        try:
            source_ms = int(event["time"])
            if source_ms < 0 or isinstance(event["time"], bool):
                continue
            event_at = datetime.fromtimestamp(source_ms / 1000, UTC)
            quantity = Decimal(str(event["sz"]))
            price = Decimal(str(event["px"]))
            start_position = event.get("start_position", event.get("startPosition"))
            before = Decimal(str(start_position)) if start_position is not None else None
            if (
                any(not number.is_finite() for number in (price, quantity))
                or quantity <= 0
                or price <= 0
                or (before is not None and not before.is_finite())
            ):
                continue
            tid = event["tid"]
            if isinstance(tid, bool) or not str(tid).isdigit():
                continue
            if event["side"] not in ("A", "B") or not isinstance(event["coin"], str):
                continue
            if event_at > received_at + timedelta(seconds=5):
                continue
        except (ValueError, TypeError, OverflowError, InvalidOperation):  # fmt: skip
            continue
        age = received_at - event_at
        status = (
            "RECOVERED_OR_STALE_NOT_FOR_FORWARD_QUALIFICATION"
            if snapshot or age > timedelta(minutes=1)
            else "PROSPECTIVE_RECEIPT_OBSERVED_BOOK_NOT_ATTESTED"
        )
        result.append(
            {
                "schema": "pm01_hyperx_first_local_receipt_v1",
                "wallet": by_address[user.lower()],
                "source_identity_sha256": sha256(user.lower().encode()).hexdigest(),
                "source_market": event["coin"],
                "source_trade_id": str(tid),
                "source_event_at": event_at.isoformat(),
                "locally_received_at": received_at.astimezone(UTC).isoformat(),
                "stream_connection_at": connected_at.astimezone(UTC).isoformat(),
                "stream_source": "HYPERX_DATA_API_WS_NOT_TRADING",
                "source_fill_price": str(price),
                "source_fill_quantity": str(quantity),
                "source_position_before": str(before) if before is not None else None,
                "source_side": event["side"],
                "source_snapshot": snapshot,
                "source_age_seconds": round(age.total_seconds(), 3),
                "coverage": status,
                "real_follower_order_sent": False,
                "whole_account_reconciled": False,
                "auto_promotion_authorized": False,
            }
        )
    return result


def store_first_local_receipt(directory: Path, observation: dict) -> bool:
    """O_EXCL on the event identity: never change its original receipt clock."""
    _private(directory)
    identity = (
        observation["source_identity_sha256"],
        observation["source_market"],
        observation["source_trade_id"],
        observation["source_event_at"],
    )
    slug = sha256(_canonical({"id": identity})).hexdigest()
    path = directory / f"hyperx-observation-{slug}.json"
    payload = _canonical(observation) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        # Existing original first local observation wins, even if new receipt
        # differs after a reconnect. Retain the original exact bytes.
        saved = json.loads(path.read_bytes())
        if any(
            saved.get(field) != observation.get(field)
            for field in (
                "source_identity_sha256",
                "source_market",
                "source_trade_id",
                "source_event_at",
                "source_fill_quantity",
                "source_fill_price",
                "source_side",
                "source_position_before",
            )
        ):
            raise ValueError("HYPERX_REPLAYED_TID_PAYLOAD_CONFLICT")
        return False
    with os.fdopen(fd, "wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    return True


async def probe(
    *,
    root: Path,
    output: Path,
    aliases: tuple[str, ...],
    duration: int,
    max_messages: int,
) -> dict:
    if (
        not 1 <= duration <= MAX_SECONDS
        or not 1 <= max_messages <= MAX_MESSAGES
        or not 1 <= len(aliases) <= MAX_WALLETS
        or len(set(aliases)) != len(aliases)
    ):
        raise ValueError("UNBOUNDED_OR_DUPLICATE_WS_SUBSCRIPTION")
    entries, _ = load_cohort(root)
    by_alias = {x.alias: x.address for x in entries}
    if any(alias not in by_alias for alias in aliases):
        raise ValueError("UNVERIFIED_COHORT_WALLET")
    _private(output)
    provider = _adapter(root)
    key = provider.key_from_existing_tracker_env()
    import websockets

    requested = {alias: by_alias[alias] for alias in aliases}
    query = urlencode({"addresses": ",".join(requested.values())})
    # The *read-only* data token is passed only in an authenticated header.
    # Neither the WebSocket URI nor any log embeds the token.
    uri = ENDPOINT + "?" + query
    results = {
        "schema": "pm01_hyperx_forward_probe_v1",
        "selected_count": len(aliases),
        "aliases": sorted(aliases),
        "read_only_data_api": True,
        "no_official_hyperliquid_api": True,
        "no_trading_token_loaded": True,
        "connected": False,
        "subscription_ack_attested": False,
        "observed_frames": 0,
        "first_local_receipts_persisted": 0,
        "server_error_categories": [],
        "errors": [],
    }
    try:
        async with websockets.connect(
            uri,
            additional_headers={"X-API-Key": key},
            open_timeout=12,
            close_timeout=5,
            max_size=1_000_000,
            ping_interval=15,
            ping_timeout=10,
        ) as ws:
            connected = datetime.now(UTC)
            results["connected"] = True
            results["connected_at"] = connected.isoformat()
            expiry = asyncio.get_running_loop().time() + duration
            while results["observed_frames"] < max_messages:
                remaining = expiry - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                receipt = datetime.now(UTC)
                results["observed_frames"] += 1
                if not isinstance(raw, str):
                    continue
                try:
                    body = json.loads(raw)
                    if isinstance(body, dict):
                        message_type = str(body.get("type", "")).lower()
                        if message_type in (
                            "subscribed",
                            "subscription",
                            "subscriptionresponse",
                            "ack",
                        ):
                            results["subscription_ack_attested"] = True
                        if message_type in ("error", "rejected", "failure"):
                            # Vendor error text may contain private addresses or
                            # headers. Only report the class, never raw messages.
                            server_text = str(body.get("message", body.get("error", ""))).lower()
                            results["server_error_categories"].append(
                                "SUBSCRIPTION_CAPACITY_OR_TIER_LIMIT"
                                if (
                                    "limit" in server_text
                                    or "too many" in server_text
                                    or "maximum" in server_text
                                )
                                else "UNCLASSIFIED_SUBSCRIPTION_REJECTION"
                            )
                    parsed = extract_observations(raw, receipt, requested, connected_at=connected)
                    for observation in parsed:
                        if store_first_local_receipt(output, observation):
                            results["first_local_receipts_persisted"] += 1
                except (ValueError, TypeError, KeyError):  # fmt: skip
                    results["errors"].append("INVALID_OR_UNATTESTED_WS_SOURCE_MESSAGE")
    except Exception as exc:
        # No raw exception/URI: URLs can contain private wallet addresses and
        # exception text can contain transport headers.
        results["errors"].append("WS_SOURCE_CONNECT_OR_RECV_FAILED:" + type(exc).__name__)
    results["status"] = (
        "RECEIPT_OBSERVED_NOT_ACCOUNT_QUALIFIED"
        if results["first_local_receipts_persisted"]
        else "NO_VERIFIED_NEW_FILL_RECEIPTS"
        if results["connected"]
        else "WS_CONNECT_BLOCKED"
    )
    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--research-root", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--aliases", nargs="+", default=["CA-005"])
    p.add_argument("--all-cohort", action="store_true")
    p.add_argument("--duration", type=int, default=20)
    p.add_argument("--max-messages", type=int, default=5)
    args = p.parse_args()
    selected = (
        tuple(entry.alias for entry in load_cohort(args.research_root)[0])
        if args.all_cohort
        else tuple(args.aliases)
    )
    result = asyncio.run(
        probe(
            root=args.research_root,
            output=args.out,
            aliases=selected,
            duration=args.duration,
            max_messages=args.max_messages,
        )
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
