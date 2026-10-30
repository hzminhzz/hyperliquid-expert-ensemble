"""PM-01 bounded private 36-wallet research run (no network, no broker).

Usage:
 PYTHONPATH=src /path/to/python -m ensemble.pm_research \
     --research-root /private/five-pod-20261029-research \
     --ledger /private/observation.db \
     --market-root /private/ml4t-data/.data/store \
     --out /private/reports/pm01-shadow

Output is a content-addressed, owner-only artifact. Alias/address maps never
leave memory; historical HyperX fills do NOT supply first-known timestamps.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from .pm_portfolio import AccountEvent, reconstruct_timeline
from .pm_shadow import Scenario, VerifiedHyperliquidMinuteSource, replay_shadow
from .pm_sources import CohortEntry, load_cohort


FROZEN_CUT = "2026-10-29T17:22:16.142+00:00"
SELECT = """
SELECT seq, kind, wallet_id, ledger_id, instrument_id, event_time,
       received_at, known_at, before_revision, after_revision, payload, quality
FROM outbox_events
WHERE (wallet_id IN ({wallet_placeholders}) OR kind = 'COVERAGE_CHANGE')
  AND known_at <= ?
ORDER BY seq ASC
LIMIT ?
"""


def _asof(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.utcoffset() is None:
        raise ValueError("study cutoff must be offset-aware")
    return dt.astimezone(UTC)


def _account_event(row: sqlite3.Row, alias: str) -> AccountEvent:
    raw = json.loads(row["payload"])
    if not isinstance(raw, dict):
        raise ValueError("non-object Rust payload")
    kwargs: dict[str, Any] = {
        "kind": row["kind"],
        "wallet_id": alias,
        "ledger_id": row["ledger_id"],
        "seq": row["seq"],
        "event_at": _asof(row["event_time"]),
        "received_at": _asof(row["received_at"]),
        "known_at": _asof(row["known_at"]),
        "quality": row["quality"],
        "coverage": str(raw.get("coverage", "UNKNOWN")),
        "partial_history": bool(raw.get("partial_history", True)),
        "before_revision": row["before_revision"],
        "after_revision": row["after_revision"],
    }
    if row["kind"] == "ACCOUNT_SNAPSHOT":
        raw_positions = raw.get("positions")
        if not isinstance(raw_positions, list) or not isinstance(raw.get("equity"), dict):
            raise ValueError("invalid atomic Rust snapshot contract")
        positions: dict[str, Decimal] = {}
        for pos in raw_positions:
            name = pos["instrument_id"]
            if name in positions:
                raise ValueError("duplicate source snapshot instrument")
            positions[name] = Decimal(str(pos["quantity_after"]))
        kwargs["positions"] = positions
        kwargs["equity"] = Decimal(str(raw["equity"]["account_value"]))
    elif row["kind"] == "POSITION_CHANGE":
        kwargs["instrument"] = row["instrument_id"]
        kwargs["quantity_after"] = Decimal(str(raw["quantity_after"]))
        kwargs["delta"] = Decimal(str(raw["delta"]))
    else:
        kwargs["gap_status"] = str(raw.get("status", "COVERAGE_UNVERIFIED"))
    return AccountEvent(**kwargs)


def _load_rust(
    ledger: Path,
    entries: tuple[CohortEntry, ...],
    cutoff: datetime,
    max_events: int,
) -> tuple[dict[str, list[AccountEvent]], dict[str, dict[str, Any]], str, int, int]:
    if max_events < 36:
        raise ValueError("max events cannot be smaller than cohort")
    by_address = {e.address: e.alias for e in entries}
    events: dict[str, list[AccountEvent]] = defaultdict(list)
    stats: dict[str, dict[str, Any]] = {
        e.alias: {
            "snapshots": 0,
            "position_changes": 0,
            "malformed": 0,
            "first_seen": None,
            "last_seen": None,
            "instruments_seen": set(),
        }
        for e in entries
    }
    fingerprint = sha256()
    shared: list[AccountEvent] = []
    total = 0
    conn = sqlite3.connect(f"file:{ledger.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        placeholders = ",".join("?" for _ in by_address)
        cur = conn.execute(
            SELECT.format(wallet_placeholders=placeholders),
            (*sorted(by_address), cutoff.isoformat(), max_events + 1),
        )
        for row in cur:
            total += 1
            if total > max_events:
                raise ValueError("OBSERVATION_LIMIT_EXCEEDED; refuse truncated cohort")
            # Hash exact pinned producer facts before parsing, including malformed
            # rows; do not include wallet addresses in the emitted report.
            fingerprint.update(
                json.dumps(
                    [row[key] for key in row.keys()], separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
            )
            fingerprint.update(b"\n")
            if row["kind"] == "COVERAGE_CHANGE":
                shared.append(_account_event(row, "__all_wallets__"))
                continue
            alias = by_address.get(row["wallet_id"].lower())
            if alias is None:
                raise ValueError("unknown identity in filtered Rust source")
            entry = stats[alias]
            kind = row["kind"]
            if kind not in {"ACCOUNT_SNAPSHOT", "POSITION_CHANGE"}:
                raise ValueError("unrecognized Rust state event kind")
            entry["snapshots" if kind == "ACCOUNT_SNAPSHOT" else "position_changes"] += 1
            entry["first_seen"] = min(entry["first_seen"] or row["known_at"], row["known_at"])
            entry["last_seen"] = max(entry["last_seen"] or row["known_at"], row["known_at"])
            try:
                event = _account_event(row, alias)
                events[alias].append(event)
                if event.kind == "POSITION_CHANGE":
                    entry["instruments_seen"].add(event.instrument)
                else:
                    assert event.positions is not None
                    entry["instruments_seen"].update(event.positions)
            except (ValueError, TypeError, KeyError, ArithmeticError):  # fmt: skip
                # Do not salvage a partial wallet after a bad producer row.
                entry["malformed"] += 1
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    for alias in events:
        events[alias].extend(shared)
    for alias in stats:
        stats[alias]["instruments_seen"] = len(stats[alias]["instruments_seen"])
    return events, stats, fingerprint.hexdigest(), total, len(shared)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(v) for v in value]
    raise TypeError(f"unsuitable evidence field type {type(value).__name__}")


def study(
    root: Path,
    ledger: Path,
    market_root: Path,
    cutoff: datetime,
    samples: int = 12,
    max_events: int = 1_000_000,
) -> dict[str, Any]:
    if not 2 <= samples <= 30:
        raise ValueError("samples must be 2..30 for a bounded study")
    entries, upstream_hashes = load_cohort(root)
    events, counts, ledger_hash, total, feed_gaps = _load_rust(ledger, entries, cutoff, max_events)
    # 60m model must have matured entirely before the pinned input cutoff.
    last_cut = cutoff - timedelta(minutes=70)
    cuts = tuple(last_cut - timedelta(days=i) for i in reversed(range(samples)))
    market = VerifiedHyperliquidMinuteSource(market_root)
    by_wallet: list[dict[str, Any]] = []
    for entry in entries:
        obs = events.get(entry.alias, [])
        cuts_result = reconstruct_timeline(entry.wallet, obs, cuts)
        history = counts[entry.alias]
        eligible_books = sum(d.book_known for d in cuts_result)
        source_status = (
            "MALFORMED_RUST_INPUT"
            if history["malformed"]
            else "NO_RUST_OBSERVATIONS"
            if not (history["snapshots"] or history["position_changes"])
            else "AS_KNOWN_PARTIAL_HISTORY"
        )
        if entry.historical_state == "FIVE_MONTH_VENDOR_GAP_HISTORICAL_ONLY":
            source_status += "_PLUS_HISTORICAL_VENDOR_GAP"
        scenarios = []
        for scenario in (
            Scenario("LATENCY_5M_COST_STRESS", 5),
            Scenario("LATENCY_60M_HEAVY_COST", 60, slippage_bps=Decimal("25")),
        ):
            if history["malformed"]:
                outcome = {
                    "status": "BLOCKED",
                    "reason": "MALFORMED_RUST_SOURCE_ROW",
                    "hypothetical_fill_count": 0,
                    "follower_pnl": None,
                    "market_only_pnl": None,
                }
            else:
                result = replay_shadow(cuts_result, market, scenario)
                outcome = {
                    "status": result.status,
                    "reason": result.reason,
                    "decision_count": result.decision_count,
                    "hypothetical_fill_count": result.hypothetical_fill_count,
                    "last_hypothetical_fill_at": result.last_hypothetical_fill_at,
                    "follower_pnl": result.follower_pnl,
                    "market_only_pnl": result.market_only_pnl,
                    "per_instrument_pnl": result.per_instrument_pnl,
                    "turnover": result.turnover,
                    "fees": result.fees,
                    "spread_slippage": result.spread_slippage,
                    "funding_assumed": result.funding_assumed,
                    "open_positions": result.open_positions,
                    "accounting_reconciled": result.accounting_reconciled,
                    "source_account_pnl": None,
                    "actual_broker_pnl": None,
                }
            scenarios.append({"name": scenario.name, **_json_safe(outcome)})
        by_wallet.append(
            {
                "alias": entry.alias,
                "cohort": entry.cohort,
                "identity_sha256": entry.address_sha256,
                "source_identity_verified": True,
                "hyperx_source_state": entry.historical_state,
                "vendor_roster_page_count": entry.vendor_page_count,
                "vendor_raw_fill_rows": entry.vendor_fill_count,
                "rust_account_snapshot_count": history["snapshots"],
                "rust_position_change_count": history["position_changes"],
                "rust_malformed_rows": history["malformed"],
                "rust_first_known_at": history["first_seen"],
                "rust_last_known_at": history["last_seen"],
                "rust_instruments_observed": history["instruments_seen"],
                "first_known_sample_books": eligible_books,
                "sample_cut_count": len(cuts_result),
                "portfolio_coverage": source_status,
                "sample_states": [
                    {
                        "cut": d.decided_at.isoformat(),
                        "book_known": d.book_known,
                        "instrument_count": len(d.source_positions),
                        "reason": d.reason,
                        "source_cursor": d.source_cursor,
                        "historical_history_partial": d.history_partial,
                    }
                    for d in cuts_result
                ],
                "scenarios": scenarios,
                "qualified_source_pnl": None,
                "qualified_prospective_follower_pnl": None,
            }
        )
    assert len(by_wallet) == 36 and len({x["identity_sha256"] for x in by_wallet}) == 36
    cards = {}
    for alias, rationale in (
        (
            "CA-005",
            "Diversified default-perp/short-leaning candidate; no first-known HyperX history.",
        ),
        (
            "W042",
            "Actual Rust-known observations exist; historical latency sensitivity and missing legs.",
        ),
        (
            "VS066",
            "Independent HIP-3 short/maker-heavy hypothesis; source maker fills not copyable.",
        ),
    ):
        card = next(x for x in by_wallet if x["alias"] == alias)
        cards[alias] = {
            "source_state": card["portfolio_coverage"],
            "first_known_book_count": card["first_known_sample_books"],
            "scenario_verdicts": [
                {
                    k: s.get(k)
                    for k in ("name", "status", "reason", "follower_pnl", "market_only_pnl")
                }
                for s in card["scenarios"]
            ],
            "research_rationale": rationale,
            "promotion": "DENIED_REQUIRES_REAL_FORWARD_EVIDENCE",
        }
    return _json_safe(
        {
            "schema": "pm01_36_wallet_shadow_research_v1",
            "issue": "hzminhzz/hyperliquid-expert-ensemble#3",
            "authority": "RESEARCH_SHADOW_ONLY_NO_LIVE_ORDERS",
            "source_event_cutoff_utc": cutoff.isoformat(),
            "source_event_rows_verified_in_read_only_sql_snapshot": total,
            "source_shared_feed_gap_events": feed_gaps,
            "source_rust_canonical_rows_sha256": ledger_hash,
            "source_cohort_manifest_sha256": upstream_hashes,
            "source_market_1m_day_shas": dict(sorted(market.source_days.items())),
            "liquidity_derived_base_volume_proxy_days": sorted(market.liquidity_proxy_days),
            "source_known_at": "RUST_DURABLE_OUTBOX",
            "hyperx_fills_known_at": "UNKNOWN_HISTORICAL_ONLY",
            "price_source": "VENUE_AWARE_SHA_VALIDATED_HYPERLIQUID_1M_OPEN_PROXY",
            "market_original_hip3_sha_defect": (
                "HISTORICALLY_REPORTED_58_BAD_SOURCE_DAYS;"
                "INDEPENDENT_LIVE_LAKE_AUDIT_REQUIRED_FOR_CURRENT_VERDICT"
            ),
            "funding": "POSITIVE_ABSOLUTE_STRESS_ASSUMPTION_NOT_OBSERVED_SETTLEMENT",
            "execution": "HYPOTHETICAL_TAKER_PLUS_SPREAD_SLIPPAGE_PARTICIPATION_NO_QUEUE_MODEL",
            "source_accounting": "INCOMPLETE_ORIGINAL_FILL_ACCOUNT_FUNDING_AND_COLLATERAL_NOT_RECONCILED",
            "first_known_sample_decision_cuts": [x.isoformat() for x in cuts],
            "wallet_count": 36,
            "cohort_counts": {"original": 6, "wave1": 10, "wave2": 20},
            "qualified_pods": 0,
            "prospective_frozen_policy_follower_evidence": 0,
            "research_candidates": cards,
            "wallets": by_wallet,
        }
    )


def _write_private_evidence(out: Path, result: dict[str, Any]) -> tuple[Path, str]:
    """Content addressed and deterministic; never overwrite divergent evidence."""
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    if out.stat().st_mode & 0o077:
        raise ValueError("evidence directory is not private")
    raw = (json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
    digest = sha256(raw).hexdigest()
    file = out / f"pm01-36-wallets-{digest[:20]}.json"
    if file.exists():
        if sha256(file.read_bytes()).hexdigest() != digest:
            raise ValueError("existing research evidence hash conflict")
        return file, digest
    fd, temp = tempfile.mkstemp(prefix=".pending-pm01-", dir=out)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, file)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return file, digest


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only PM-01 36-wallet shadow replay")
    parser.add_argument("--research-root", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--market-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--cutoff", default=FROZEN_CUT)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--max-events", type=int, default=1_000_000)
    args = parser.parse_args()
    report = study(
        args.research_root,
        args.ledger,
        args.market_root,
        _asof(args.cutoff),
        args.samples,
        args.max_events,
    )
    path, digest = _write_private_evidence(args.out, report)
    by_status = defaultdict(int)
    for row in report["wallets"]:
        for s in row["scenarios"]:
            by_status[s["status"]] += 1
    print(
        json.dumps(
            {
                "wallets": report["wallet_count"],
                "identity_unique": len({w["identity_sha256"] for w in report["wallets"]}),
                "rust_events": report["source_event_rows_verified_in_read_only_sql_snapshot"],
                "shared_gap_events": report["source_shared_feed_gap_events"],
                "scenarios": dict(sorted(by_status.items())),
                "qualified_pods": report["qualified_pods"],
                "evidence_sha256": digest,
                "evidence_file": str(path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
