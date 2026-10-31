"""PM-01 source-exact compact whole-observed-book tape for PM-03 research.

Historical HyperX fill timestamps NEVER imply receipt/first-known timestamps.
This is a non-tradable, immutable diagnostic tape, never a broker command.
Missing source legs and position-chain breaks remain explicitly UNKNOWN.
"""

from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
from itertools import groupby
import json
from pathlib import Path
from typing import Any

from .pm_fill_replay import (
    CUT,
    STUDY_START,
    _asof_full_book_anchor,
    _coins,
    _market_instrument,
    _same_position,
    _source_fills,
)
from .pm_portfolio import classify_transition
from .pm_research import _write_private_evidence
from .pm_shadow import MarketDataBlocked
from .pm_sources import CohortEntry, load_cohort

SCHEMA = "pm01_observed_book_tape_v1"
ZERO = Decimal(0)


def _instrument_label(coin: str) -> str | None:
    try:
        return _market_instrument(coin)
    except MarketDataBlocked:
        return None


def _position_map(book: dict[str, Decimal]) -> list[dict[str, str | None]]:
    return [
        {
            "source_coin": coin,
            "instrument": _instrument_label(coin),
            "signed_quantity": str(qty),
            "instrument_resolution": "EXACT" if _instrument_label(coin) else "UNKNOWN",
        }
        for coin, qty in sorted(book.items())
        if qty != ZERO
    ]


def _transition(before: Decimal, after: Decimal) -> str:
    # Pure transition classification only; never use independent Rust observations.
    return classify_transition(before, after)


def build_wallet_tape(
    entry: CohortEntry,
    coins: list[str],
    fills: list[tuple],
    errors: list[str],
) -> dict[str, Any]:
    """Exactly one immutable wallet tape. No future or source-less zero book."""
    if not coins or len(coins) != len(set(coins)) or any(e[2] not in coins for e in fills):
        raise ValueError("INVALID_VENDOR_LISTED_BOOK")
    if any(e[0] > CUT or e[0].tzinfo is None for e in fills):
        raise ValueError("FILL_NOT_WITHIN_FROZEN_CUTOFF")
    anchor = _asof_full_book_anchor(fills, coins)
    head: dict[str, Any] = {
        "wallet": entry.alias,
        "cohort": entry.cohort,
        "wallet_identity_sha256": entry.address_sha256,
        "listed_instruments": len(coins),
        "vendor_source_scope": "VENDOR_LISTED_BOOK_NOT_EXCHANGE_ACCOUNT",
        "source_equity_status": "UNKNOWN",
        "source_funding_status": "UNKNOWN",
        "source_collateral_status": "UNKNOWN",
        "source_first_known_at": None,
        "source_receipt_clock_attested": False,
        "causal_follower_actions_allowed": False,
        "historical_oracle_only": True,
        "retrospective_source_errors": errors,
        "observed_raw_fills": len(fills),
        "initial_signed_book": None,
        "anchor_source_event_at": None,
        "events": [],
    }
    fatal = [x for x in errors if not x.endswith(":POSITION_DISCONTINUITY")]
    if anchor is None or not fills or fatal:
        return {
            **head,
            "book_status": (
                "BLOCKED_UNVERIFIED_SOURCE" if fatal else "BLOCKED_NO_COMPLETE_VENDOR_LISTED_ANCHOR"
            ),
            "economic_replay_allowed": False,
        }
    when, state, consumed, warmup_gaps, first_nonflat = anchor
    unsupported = sorted(coin for coin in coins if _instrument_label(coin) is None)
    head.update(
        {
            "anchor_source_event_at": when.isoformat(),
            "initial_signed_book": _position_map(state),
            "warmup_fills_excluded": consumed,
            "left_censored_initial_nonflat_instruments": first_nonflat,
            "warmup_position_gaps": warmup_gaps,
            "unsupported_vendor_instruments": unsupported,
            "book_status": (
                "UNKNOWN_UNSUPPORTED_INSTRUMENT"
                if unsupported
                else "HISTORICAL_OBSERVED_VENDOR_LISTED_ONLY"
            ),
            "economic_replay_allowed": False,
        }
    )
    bad = bool(unsupported)
    events = []
    gap_count = 0
    for event_time, group in groupby(fills[consumed:], key=lambda e: e[0]):
        changes = []
        for _, tid, coin, before, after, signed_change, price in group:
            expected = state.get(coin)
            contiguous = expected is not None and _same_position(expected, before)
            if not contiguous:
                bad = True
                gap_count += 1
            # Save vendor-reported before/after independently of any assumed
            # continuous state, so an audit can identify the exact contradiction.
            state[coin] = after
            changes.append(
                {
                    "source_coin": coin,
                    "instrument": _instrument_label(coin),
                    "trade_id": str(tid),
                    "observed_before": str(before),
                    "observed_after": str(after),
                    "signed_fill_quantity": str(signed_change),
                    "source_fill_price": str(price),
                    "transition": _transition(before, after),
                    "source_continuous": contiguous,
                }
            )
        events.append(
            {
                "source_event_at": event_time.isoformat(),
                "first_known_at": None,
                "received_at": None,
                "decision_at": None,
                "hypothetical_fill_at": None,
                "observation_type": "VENDOR_HISTORICAL_NOT_LIVE",
                "changes": changes,
                "status": (
                    "UNKNOWN_AFTER_SOURCE_GAP" if bad else "HISTORICAL_VENDOR_BOOK_DIAGNOSTIC_ONLY"
                ),
                "can_feed_pm03_actionable_account": False,
            }
        )
    head["post_anchor_source_gaps"] = gap_count
    if gap_count:
        head["book_status"] = "UNKNOWN_AFTER_SOURCE_GAP"
    head["events"] = events
    return head


def build_tape(
    root: Path,
    *,
    hyperx_generations: tuple[Path, ...] = (),
) -> dict[str, Any]:
    entries, fingerprints = load_cohort(root)
    additions: dict[str, list[tuple]] = {}
    overlay = None
    if hyperx_generations:
        from .pm_hyperx_overlay_audit import examine

        overlay, additions = examine(root, hyperx_generations)
        if overlay["wallet_count"] != 36 or not overlay["original_vendor_cache_intact"]:
            raise ValueError("UNVERIFIED_HYPERX_OVERLAY")
    wallets = []
    for entry in entries:
        coins = _coins(root, entry.alias)
        fills, issues = _source_fills(
            root,
            entry.alias,
            entry.address,
            coins,
            additional_events=additions.get(entry.alias),
        )
        wallets.append(build_wallet_tape(entry, coins, fills, issues))
    if Counter(x["cohort"] for x in wallets) != Counter({"original": 6, "wave1": 10, "wave2": 20}):
        raise ValueError("PM01_COHORT_DRIFT")
    return {
        "schema": SCHEMA,
        "source": "AUTHENTICATED_HYPERX_VENDOR_FILLS_ONLY",
        "source_manifests_sha256": fingerprints,
        "overlay_verified": bool(overlay),
        "overlay_generation_identifiers": (overlay["private_generations"] if overlay else []),
        "study_start": STUDY_START.isoformat(),
        "cutoff": CUT.isoformat(),
        "wallet_count": 36,
        "wallets_with_observable_source_anchor": sum(
            card["initial_signed_book"] is not None for card in wallets
        ),
        "wallets_with_post_anchor_source_gaps": sum(
            card.get("post_anchor_source_gaps", 0) > 0 for card in wallets
        ),
        "wallets_with_real_first_known_clocks": 0,
        "pm03_engineering_consumption": "HISTORICAL_DIAGNOSTICS_ONLY",
        "pm03_actionable_targets_available": False,
        "original_source_files_unchanged": True,
        "economic_follower_qualifications": 0,
        "wallets": wallets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-root", type=Path, required=True)
    parser.add_argument("--hyperx-generations", type=Path, nargs="*", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    evidence = build_tape(args.research_root, hyperx_generations=tuple(args.hyperx_generations))
    path, digest = _write_private_evidence(args.out, evidence)
    print(
        json.dumps(
            {
                "schema": evidence["schema"],
                "wallets": evidence["wallet_count"],
                "wallets_with_anchor": evidence["wallets_with_observable_source_anchor"],
                "source_gapped_wallets": evidence["wallets_with_post_anchor_source_gaps"],
                "actionable": evidence["pm03_actionable_targets_available"],
                "sha256": digest,
                "file": str(path),
            },
            sort_keys=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
