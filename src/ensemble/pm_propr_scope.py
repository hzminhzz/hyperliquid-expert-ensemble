"""PM-01 Propr-scoped market readiness, bounded to owner-held public universe.

This *does not* retroactively change which instruments were tradable.
The 2026-10-30 Propr displayed-asset list is an ex-post snapshot after
the frozen 2026-10-29 study cutoff. Only matching native crypto tickers
are strict display matches. xyz: HIP-3 matches remain tentative because
Propr market/DEX/order identities are not independently attested.

90% coverage is a *data-readiness* screen on market instrument-days.
It never overrides genuine point-in-time, whole-book, source continuity,
market execution, account, or external prop-rule acceptance.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal
from itertools import groupby
from hashlib import sha256
import json
from pathlib import Path

from .pm_fill_replay import (
    CUT,
    STUDY_START,
    _asof_full_book_anchor,
    _coins,
    _market_instrument,
    _source_fills,
)
from .pm_market_audit import _audit_clocks
from .pm_shadow import VerifiedHyperliquidMinuteSource
from .pm_research import _write_private_evidence
from .pm_sources import load_cohort

TARGET_NUM = 9
TARGET_DEN = 10
SCOPE_STRICT = "DISPLAY_NATIVE_CRYPTO_MATCH"
SCOPE_TENTATIVE = "TENTATIVE_XYZ_HIP3_DISPLAY_MATCH"
SCOPE_OUTSIDE = "OUTSIDE_PROPR_DISPLAY_UNIVERSE"
SCOPE_OTHER_DEX = "UNVERIFIED_DEX_SCOPE"
SCOPE_OPAQUE = "OPAQUE_HYPERLIQUID_INSTRUMENT"


def classify_coin(coin: str, snapshot: dict) -> str:
    markets = snapshot["market_symbols"]
    crypto = set(markets["crypto"])
    other = set(markets["equities"] + markets["indices"] + markets["commodities"])
    if coin.startswith("#"):
        return SCOPE_OPAQUE
    if ":" not in coin:
        return SCOPE_STRICT if coin in crypto else SCOPE_OUTSIDE
    scope, ticker = coin.split(":", 1)
    if scope == "xyz" and ticker in other:
        return SCOPE_TENTATIVE
    if scope not in {"xyz", "default"}:
        return SCOPE_OTHER_DEX
    return SCOPE_OUTSIDE


def _coin_from_market(instrument: str) -> str:
    parts = instrument.split(":")
    if len(parts) != 5 or parts[:3] != ["hyperliquid", "mainnet", "perp"]:
        raise ValueError("UNVERIFIED_HYPERLIQUID_MARKET_ID")
    return parts[4] if parts[3] == "default" else parts[3] + ":" + parts[4]


def _coverage(rows: list[dict]) -> dict:
    total = len(rows)
    verified = sum(x["status"] == "SOURCE_DAY_SHA_VERIFIED" for x in rows)
    needed = max(0, (TARGET_NUM * total + TARGET_DEN - 1) // TARGET_DEN - verified)
    return {
        "required_instrument_utc_days": total,
        "sha_verified_days": verified,
        "unavailable_days": total - verified,
        "coverage_fraction": round(verified / total, 6) if total else None,
        "target": 0.90,
        "target_reached": total > 0 and needed == 0,
        "additional_exact_days_required_for_90pct": needed,
        "not_actual_executable_minute_coverage": True,
        "not_proof_of_historical_propr_listing": True,
    }


def build_scope(
    public: dict,
    compatibility: dict,
    market_audit: dict,
    expected_cohort_aliases: tuple[str, ...],
) -> dict:
    """Fail closed on unsupported or incomplete private/public source manifests."""
    if public.get("schema") != "propr_public_universe_browser_snapshot_v1":
        raise ValueError("UNVERIFIED_PROPR_UNIVERSE_SNAPSHOT")
    if compatibility.get("schema") != "propr_wallet_roster_candidate_match_v1":
        raise ValueError("UNVERIFIED_PROPR_VENDOR_ROSTER_COMPATIBILITY")
    if market_audit.get("schema") != "pm01_native_1m_36_wallet_coverage_v1":
        raise ValueError("UNVERIFIED_36_WALLET_NATIVE_1M_AUDIT")
    if public.get("retrieved_on_utc_date") != compatibility.get("snapshot_date"):
        raise ValueError("PROPR_SNAPSHOT_DATE_DRIFT")
    if (
        market_audit.get("wallet_count") != 36
        or len(expected_cohort_aliases) != 36
        or len(set(expected_cohort_aliases)) != 36
    ):
        raise ValueError("MISSING_36_WALLET_COHORT")
    if public.get("not_proof_of_historical_availability") is not True:
        raise ValueError("PROPR_HISTORY_FALSIFIED")
    if (
        compatibility.get("not_pit") is not True
        or compatibility.get("not_verified_live_order_api_acceptance") is not True
    ):
        raise ValueError("UNSUPPORTED_HISTORICAL_OR_ORDER_VERIFICATION_CLAIM")
    if sum(public["category_counts"].values()) != public["snapshot_market_count"]:
        raise ValueError("PROPR_UNIVERSE_CATEGORY_DRIFT")
    all_symbols = public["market_symbols"]
    if sum(len(x) for x in all_symbols.values()) != public["snapshot_market_count"]:
        raise ValueError("PROPR_MARKET_SYMBOL_ROSTER_DRIFT")
    by_wallet: dict[str, dict] = {}
    for group in ("original_six", "wave_a", "wave_b"):
        for row in compatibility[group]:
            alias = row["alias"]
            if alias in by_wallet:
                raise ValueError("DUPLICATED_VENDOR_WALLET_ALIAS")
            by_wallet[alias] = row
    if set(by_wallet) != set(expected_cohort_aliases):
        raise ValueError("PROPR_COMPATIBILITY_WALLET_IDENTITY_MISMATCH")
    trades = sum(row["vendor_completed_trade_roster_total"] for row in by_wallet.values())
    strict = sum(row["exact_native_crypto_completed_trade_count"] for row in by_wallet.values())
    tentative = sum(row["tentative_hip3_completed_trade_count"] for row in by_wallet.values())
    if trades <= 0 or strict < 0 or tentative < 0 or strict + tentative > trades:
        raise ValueError("INVALID_VENDOR_COMPLETED_TRADE_COUNTS")

    requirements = market_audit.get("instrument_day_inventory")
    if not isinstance(requirements, list) or len(requirements) != market_audit.get(
        "unique_instrument_days_audited"
    ):
        raise ValueError("MISSING_COMPLETE_MARKET_DAY_DENOMINATOR")
    if len({(x["instrument"], x["date_utc"]) for x in requirements}) != len(requirements):
        raise ValueError("DUPLICATE_MARKET_DAY_IN_AUDIT")
    if sum(x["status"] == "SOURCE_DAY_SHA_VERIFIED" for x in requirements) != market_audit.get(
        "sha_verified_instrument_days"
    ):
        raise ValueError("MARKET_DAY_SOURCE_SHA_COUNTS_DRIFT")
    if len(requirements) != sum(
        (x["status"] == "SOURCE_DAY_SHA_VERIFIED") for x in requirements
    ) + market_audit.get("missing_or_invalid_instrument_days"):
        raise ValueError("MARKET_DAY_SOURCE_GAP_COUNTS_DRIFT")

    categorized = defaultdict(list)
    for row in requirements:
        categorized[classify_coin(_coin_from_market(row["instrument"]), public)].append(row)
    strict_days = categorized[SCOPE_STRICT]
    tentative_days = categorized[SCOPE_TENTATIVE]
    out_days = categorized[SCOPE_OUTSIDE] + categorized[SCOPE_OTHER_DEX] + categorized[SCOPE_OPAQUE]
    strict_data = _coverage(strict_days)
    candidate_data = _coverage(strict_days + tentative_days)
    excluded_data = _coverage(out_days)

    priority_wallet_counts: dict[str, int] = {}
    priority_samples: dict[str, int] = {}
    for row in market_audit.get("gaps_by_market", []):
        instrument = row.get("instrument")
        if isinstance(instrument, str):
            priority_wallet_counts[instrument] = max(
                priority_wallet_counts.get(instrument, 0), row.get("wallet_count", 0)
            )
            priority_samples[instrument] = max(
                priority_samples.get(instrument, 0), row.get("source_book_samples", 0)
            )
    queue = []
    excluded = Counter()
    for row in requirements:
        if row["status"] == "SOURCE_DAY_SHA_VERIFIED":
            continue
        classification = classify_coin(_coin_from_market(row["instrument"]), public)
        if classification not in (SCOPE_STRICT, SCOPE_TENTATIVE):
            excluded[classification] += 1
            continue
        queue.append(
            {
                "instrument": row["instrument"],
                "provider_symbol": _coin_from_market(row["instrument"]),
                "date_utc": row["date_utc"],
                "status": row["status"],
                "scope": classification,
                "affected_wallet_count": priority_wallet_counts.get(row["instrument"], 0),
                "book_samples_for_market_gap": priority_samples.get(row["instrument"], 0),
                "allowed_for_market_data_acquisition": row["status"] == "MISSING_NATIVE_MARKET_DAY",
                "tradability_confirmed": False,
            }
        )
    queue.sort(
        key=lambda r: (
            0 if r["scope"] == SCOPE_STRICT else 1,
            0 if r["allowed_for_market_data_acquisition"] else 1,
            -r["affected_wallet_count"],
            -r["book_samples_for_market_gap"],
            r["instrument"],
            r["date_utc"],
        )
    )
    eligible_market_days = candidate_data["required_instrument_utc_days"]
    total_market_days = market_audit["unique_instrument_days_audited"]
    if eligible_market_days + len(out_days) != total_market_days:
        raise ValueError("PROPR_DAY_SCOPE_PARTITION_NOT_EXHAUSTIVE")
    return {
        "schema": "pm01_propr_scoped_90pct_market_readiness_v1",
        "source_cohort_wallet_count": 36,
        "propr_snapshot_date": public["retrieved_on_utc_date"],
        "propr_displayed_market_count": public["snapshot_market_count"],
        "snapshot_after_frozen_cutoff_not_pit": True,
        "verified_historical_listings": False,
        "verified_account_order_acceptance": False,
        "no_venue_or_dex_equivalence_inferred": True,
        "trade_roster_coverage": {
            "vendor_completed_trade_count_all_36": trades,
            "strict_native_display_count": strict,
            "strict_native_display_fraction": round(strict / trades, 6),
            "tentative_xyz_display_count": tentative,
            "tentative_xyz_display_fraction": round(tentative / trades, 6),
            "strict_plus_tentative_count": strict + tentative,
            "strict_plus_tentative_upper_bound_fraction": round((strict + tentative) / trades, 6),
            "remaining_unmapped_completed_trades": trades - strict - tentative,
            "count_basis": "VENDOR_90D_COMPLETED_TRADES_NOT_FILL_NOTIONAL",
            "candidate_not_verified_propr_order_eligibility": True,
        },
        "market_day_coverage": {
            "original_all_market_days": _coverage(requirements),
            "strict_native_crypto_display": strict_data,
            "strict_plus_tentative_xyz_display": candidate_data,
            "excluded_or_unverified_market_scope": excluded_data,
        },
        "out_of_scope_gap_days_by_reason": dict(sorted(excluded.items())),
        "propr_only_priority_market_acquisition_queue": queue,
        "all_36_wallet_vendor_roster_market_matches": [
            {
                "wallet": alias,
                "completed_trades": by_wallet[alias]["vendor_completed_trade_roster_total"],
                "strict_native_fraction": by_wallet[alias]["exact_crypto_coverage_fraction"],
                "max_tentative_fraction": by_wallet[alias]["mapping_max_coverage_fraction"],
                "unknown_unverified_symbols": by_wallet[alias].get("unmapped_top5", [])[:5],
            }
            for alias in expected_cohort_aliases
        ],
        "pm01_scientific_qualified": False,
        "pm03_account_executable": False,
        "excluded_source_hedges_are_not_flat": True,
        "propr_90pct_data_gate_not_alpha_gate": True,
    }


def _wallet_propr_day_coverage(
    fills: list, coins: list[str], public: dict, market_statuses: dict
) -> dict:
    """Study-anchored whole-book native-day needs, per-wallet and per-scope."""
    anchor = _asof_full_book_anchor(fills, coins)
    if anchor is None:
        return {
            "status": "NO_AUTHORITATIVE_VENDOR_BOOK_ANCHOR",
            "strict": _coverage([]),
            "candidate": _coverage([]),
        }
    start, state, skip, _, _ = anchor
    state = dict(state)
    observed: set[tuple[str, str, str]] = set()

    def record(clock, terminal=False):
        times = (CUT.replace(second=0, microsecond=0),) if terminal else _audit_clocks(clock, CUT)
        for tm in times:
            for coin, qty in state.items():
                if qty == 0:
                    continue
                scope = classify_coin(coin, public)
                if scope not in (SCOPE_STRICT, SCOPE_TENTATIVE):
                    continue
                observed.add((_market_instrument(coin), tm.date().isoformat(), scope))

    record(start)
    for when, batch in groupby(fills[skip:], lambda row: row[0]):
        if VerifiedHyperliquidMinuteSource._minute(when + timedelta(minutes=5)) > CUT:
            continue
        for row in batch:
            state[row[2]] = row[4]
        record(when)
    record(CUT, terminal=True)

    matched = [
        {
            "status": market_statuses.get((instrument, day), "MISSING_GLOBAL_DAY_REQUIREMENT"),
            "instrument": instrument,
            "date_utc": day,
            "scope": scope,
        }
        for instrument, day, scope in sorted(observed)
    ]
    if any(x["status"] == "MISSING_GLOBAL_DAY_REQUIREMENT" for x in matched):
        raise ValueError("PER_WALLET_SOURCE_CLOCKS_AND_GLOBAL_AUDIT_DISAGREE")
    return {
        "status": "SOURCE_ANCHORED_MARKET_RESEARCH_ONLY",
        "strict": _coverage([x for x in matched if x["scope"] == SCOPE_STRICT]),
        "candidate": _coverage(matched),
        "strict_missing_days": [
            {"instrument": x["instrument"], "date_utc": x["date_utc"]}
            for x in matched
            if x["scope"] == SCOPE_STRICT and x["status"] != "SOURCE_DAY_SHA_VERIFIED"
        ],
        "candidate_missing_days": [
            {"instrument": x["instrument"], "date_utc": x["date_utc"]}
            for x in matched
            if x["status"] != "SOURCE_DAY_SHA_VERIFIED"
        ],
        "observed_unmapped_hedges_may_change_net_book": True,
    }


def add_fill_scope_summary(
    report: dict,
    root: Path,
    expected_aliases: tuple[str, ...],
    public: dict,
    market_audit: dict | None = None,
) -> None:
    """Compare the independent 90d vendor completed-trade proxy to real cached fill rows."""
    entries, _ = load_cohort(root)
    if tuple(e.alias for e in entries) != expected_aliases:
        raise ValueError("SOURCE_COHORT_CHANGED_DURING_SCOPE_AUDIT")
    window_count = Counter()
    notional = defaultdict(lambda: Decimal("0"))
    no_positive_fill_prices = Counter()
    by_wallet = []
    wallet_days = []
    statuses = (
        {
            (x["instrument"], x["date_utc"]): x["status"]
            for x in market_audit["instrument_day_inventory"]
        }
        if market_audit is not None
        else None
    )
    for entry in entries:
        coins = _coins(root, entry.alias)
        rows, errors = _source_fills(root, entry.alias, entry.address, coins)
        counter = Counter()
        seen_nonzero_outside = set()
        seen_nonzero_tentative = set()
        anchor = _asof_full_book_anchor(rows, coins)
        if statuses is not None:
            wallet_days.append(
                {
                    "wallet": entry.alias,
                    "cohort": entry.cohort,
                    **_wallet_propr_day_coverage(rows, coins, public, statuses),
                }
            )
        if anchor is not None:
            for coin, quantity in anchor[1].items():
                if quantity != 0:
                    scope = classify_coin(coin, public)
                    if scope == SCOPE_TENTATIVE:
                        seen_nonzero_tentative.add(coin)
                    elif scope != SCOPE_STRICT:
                        seen_nonzero_outside.add(coin)
        for row in rows:
            when, _, coin, _, after, signed_qty, price = row
            scope = classify_coin(coin, public)
            if STUDY_START <= when <= CUT:
                if after != 0:
                    if scope == SCOPE_TENTATIVE:
                        seen_nonzero_tentative.add(coin)
                    elif scope != SCOPE_STRICT:
                        seen_nonzero_outside.add(coin)
                window_count[scope] += 1
                counter[scope] += 1
                if price > 0:
                    notional[scope] += abs(signed_qty) * price
                else:
                    no_positive_fill_prices[scope] += 1
        by_wallet.append(
            {
                "wallet": entry.alias,
                "cohort": entry.cohort,
                "window_source_fill_counts": dict(sorted(counter.items())),
                "unresolved_source_errors": errors,
                "nonzero_unmatched_coin_count_at_anchor_or_study_events": len(seen_nonzero_outside),
                "nonzero_tentative_xyz_coin_count_at_anchor_or_study_events": len(
                    seen_nonzero_tentative
                ),
                "source_anchor_verified_vendor_listed_only": anchor is not None,
                "excluded_active_hedge_risk_cannot_be_assumed_zero": bool(seen_nonzero_outside),
            }
        )
    total = sum(window_count.values())
    denom_notional = sum(notional.values())
    strict = window_count[SCOPE_STRICT]
    tentative = window_count[SCOPE_TENTATIVE]
    report["actual_5month_fill_scope"] = {
        "total_source_fill_events": total,
        "strict_display_source_fills": strict,
        "strict_plus_tentative_source_fills": strict + tentative,
        "strict_fill_fraction": round(strict / total, 6) if total else None,
        "candidate_upper_fill_fraction": round((strict + tentative) / total, 6) if total else None,
        "source_quoted_positive_price_notional_sum": str(denom_notional),
        "strict_source_notional_fraction": (
            round(float(notional[SCOPE_STRICT] / denom_notional), 6) if denom_notional else None
        ),
        "candidate_upper_notional_fraction": (
            round(float((notional[SCOPE_STRICT] + notional[SCOPE_TENTATIVE]) / denom_notional), 6)
            if denom_notional
            else None
        ),
        "unpriced_source_fill_counts": dict(sorted(no_positive_fill_prices.items())),
        "source_fill_notional_not_propr_follower_notional": True,
        "future_or_latest_propr_market_snapshot_not_historical_pit": True,
        "source_gap_wallets_not_silently_repaired": True,
        "wallets_with_any_nonzero_excluded_leg": sum(
            row["excluded_active_hedge_risk_cannot_be_assumed_zero"] for row in by_wallet
        ),
    }
    report["source_wallet_study_scope"] = by_wallet
    if market_audit is not None:
        report["per_wallet_propr_data_readiness"] = wallet_days
        report["unhedged_gap_free_research_shortlist"] = [
            r["wallet"]
            for r in by_wallet
            if not r["excluded_active_hedge_risk_cannot_be_assumed_zero"]
            and not r["unresolved_source_errors"]
        ]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--research-root", required=True, type=Path)
    p.add_argument("--market-audit", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()
    entries, source_fingerprints = load_cohort(args.research_root)
    base = args.research_root
    universe_path = base / "PROPR_PUBLIC_ASSET_UNIVERSE_20261030.json"
    compatibility_path = base / "PROPR_WALLET_MARKET_COMPATIBILITY_20261030.json"
    public = json.loads(universe_path.read_text(encoding="utf-8"))
    compat = json.loads(compatibility_path.read_text(encoding="utf-8"))
    market = json.loads(args.market_audit.read_text(encoding="utf-8"))
    result = build_scope(public, compat, market, tuple(e.alias for e in entries))
    result["input_source_manifest_sha256"] = source_fingerprints
    result["input_files_sha256"] = {
        "propr_public_universe": sha256(universe_path.read_bytes()).hexdigest(),
        "propr_wallet_roster_overlap": sha256(compatibility_path.read_bytes()).hexdigest(),
        "market_1m_audit": sha256(args.market_audit.read_bytes()).hexdigest(),
    }
    add_fill_scope_summary(result, base, tuple(e.alias for e in entries), public, market)
    path, digest = _write_private_evidence(args.out, result)
    print(
        json.dumps(
            {
                "wallet_count": 36,
                "propr_snapshot_date": result["propr_snapshot_date"],
                "completed_trades_proxy": result["trade_roster_coverage"],
                "native_market_days": result["market_day_coverage"]["strict_native_crypto_display"],
                "native_plus_tentative_market_days": result["market_day_coverage"][
                    "strict_plus_tentative_xyz_display"
                ],
                "outside_scope_market_days": result["market_day_coverage"][
                    "excluded_or_unverified_market_scope"
                ],
                "actual_source_fills_in_window": result["actual_5month_fill_scope"],
                "propr_acquisition_queue_days": len(
                    result["propr_only_priority_market_acquisition_queue"]
                ),
                "first_priority": result["propr_only_priority_market_acquisition_queue"][:10],
                "qualified": False,
                "private_evidence_sha256": digest,
                "private_evidence_path": str(path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
