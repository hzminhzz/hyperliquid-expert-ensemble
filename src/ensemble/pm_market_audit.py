"""Read-only PM-01 36-wallet native 1m market-data coverage and integrity audit.

Uses only immutable historical HyperX fills and native Hyperliquid partition
metadata. No Rust, no network, no bar substitution and no production writes.
Coverage here is per instrument *and calendar day*; an intact Parquet day alone
does NOT prove every requested minute has a valid/open executable bar.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from hashlib import sha256
from itertools import groupby
import json
from pathlib import Path

from .pm_fill_replay import CUT, _coins, _source_fills, _asof_full_book_anchor, _market_instrument
from .pm_shadow import MarketDataBlocked, VerifiedHyperliquidMinuteSource
from .pm_sources import load_cohort
from .pm_research import _write_private_evidence


LATENCY_MINUTES = (5, 15, 60)


def _audit_clocks(source_when: datetime, cutoff: datetime) -> tuple[datetime, ...]:
    """Future executable minute opens that matured within frozen research."""
    return tuple(
        moment
        for latency in LATENCY_MINUTES
        if (
            moment := VerifiedHyperliquidMinuteSource._minute(
                source_when + timedelta(minutes=latency)
            )
        )
        <= cutoff
    )


def _audit_partition(root: Path, instrument: str, when: datetime) -> str:
    """Verify original 1m day metadata and bytes without loading all bars."""
    try:
        family = VerifiedHyperliquidMinuteSource._family(instrument)
    except MarketDataBlocked as exc:
        return str(exc)
    minute = VerifiedHyperliquidMinuteSource._minute(when)
    path = root / family / f"year={minute.year:04d}/month={minute.month:02d}/day={minute.day:02d}"
    data = path / "data.parquet"
    meta = path / "source.json"
    if not data.is_file() or not meta.is_file():
        if meta.is_file():
            try:
                source = json.loads(meta.read_text(encoding="utf-8"))
                dex, symbol = instrument.split(":")[3:5]
                expected_symbol = dex + ":" + symbol if dex != "default" else symbol
                if (
                    source.get("status") == "no_source_data"
                    and source.get("provider") == "CryptoHFTData"
                    and source.get("exchange") == "hyperliquid_futures"
                    and source.get("source_symbol") == expected_symbol
                ):
                    return "CHD_CONFIRMED_NO_TRADE_SOURCE"
            except (OSError, ValueError, TypeError):  # fmt: skip
                pass
            return "INCOMPLETE_MARKET_DAY_SOURCE_PAIR"
        return "MISSING_NATIVE_MARKET_DAY"
    try:
        source = json.loads(meta.read_text(encoding="utf-8"))
        dex, symbol = instrument.split(":")[3:5]
        expected_symbol = dex + ":" + symbol if dex != "default" else symbol
        if source.get("exchange") != "hyperliquid_futures":
            return "WRONG_NATIVE_MARKET_VENUE"
        if source.get("source_symbol") != expected_symbol:
            return "WRONG_NATIVE_MARKET_SYMBOL"
        expected = source.get("sha256")
        if not isinstance(expected, str) or len(expected) != 64:
            return "SOURCE_SHA_MISSING"
        if sha256(data.read_bytes()).hexdigest() != expected:
            return "SOURCE_SHA_MISMATCH"
        return "SOURCE_DAY_SHA_VERIFIED"
    except (OSError, ValueError, TypeError):  # fmt: skip
        return "SOURCE_DAY_MALFORMED"


def inventory(research_root: Path, market_root: Path) -> dict:
    entries, fingerprints = load_cohort(research_root)
    per_day_cache: dict[tuple[str, str], str] = {}
    required = Counter()
    by_issue = defaultdict(set)
    counts = Counter()
    wallets = []
    for entry in entries:
        coins = _coins(research_root, entry.alias)
        fills, source_errors = _source_fills(research_root, entry.alias, entry.address, coins)
        anchor = _asof_full_book_anchor(fills, coins)
        summary = {
            "wallet": entry.alias,
            "cohort": entry.cohort,
            "source_coin_count": len(coins),
            "source_fills": len(fills),
            "source_errors": source_errors,
            "source_coverage_start": anchor[0].isoformat() if anchor else None,
            "source_before_coverage_gaps": anchor[3] if anchor else None,
        }
        if anchor is None:
            summary["status"] = "BLOCKED_NO_FULL_VENDOR_ROSTER_ANCHOR"
            wallets.append(summary)
            continue
        when, state, skip, _, _ = anchor
        sample_count = 0
        missing_samples = Counter()

        def sample_book(source_when: datetime, *, terminal: bool = False):
            nonlocal sample_count
            times = (
                (CUT.replace(second=0, microsecond=0),)
                if terminal
                else _audit_clocks(source_when, CUT)
            )
            for market_minute in times:
                sample_count += 1
                for coin, qty in state.items():
                    if qty == 0:
                        continue
                    try:
                        instrument = _market_instrument(coin)
                        day = market_minute.date().isoformat()
                        key = (instrument, day)
                        if key not in per_day_cache:
                            per_day_cache[key] = _audit_partition(
                                market_root, instrument, market_minute
                            )
                        state_code = per_day_cache[key]
                    except MarketDataBlocked as exc:
                        instrument = coin
                        day = "UNKNOWN"
                        state_code = str(exc)
                    required[(instrument, day, state_code)] += 1
                    counts[state_code] += 1
                    if state_code != "SOURCE_DAY_SHA_VERIFIED":
                        missing_samples[(instrument, state_code)] += 1
                        by_issue[(instrument, state_code)].add(entry.alias)

        sample_book(when)
        continuity_after = 0
        for source_when, group in groupby(fills[skip:], key=lambda e: e[0]):
            if VerifiedHyperliquidMinuteSource._minute(source_when + timedelta(minutes=5)) > CUT:
                continue
            for _, _, coin, before, after, _, _ in group:
                if state.get(coin) != before:
                    continuity_after += 1
                state[coin] = after
            sample_book(source_when)
        # Open follower exposure requires an independently sourced terminal
        # mark, even for inactive coins whose last fill was days earlier.
        sample_book(CUT, terminal=True)
        summary.update(
            {
                "status": "COVERAGE_AUDITED_NOT_ECONOMICALLY_QUALIFIED",
                "book_sample_count": sample_count,
                "post_anchor_source_gaps": continuity_after,
                "missing_source_day_samples": sum(missing_samples.values()),
                "top_missing_markets": [
                    {"instrument": x[0][0], "reason": x[0][1], "book_samples": x[1]}
                    for x in missing_samples.most_common(10)
                ],
            }
        )
        wallets.append(summary)
    gaps = sorted(
        (
            {
                "instrument": instrument,
                "reason": reason,
                "wallet_count": len(aliases),
                "affected_wallets": sorted(aliases),
                "source_book_samples": sum(
                    count
                    for (i, day, status), count in required.items()
                    if i == instrument and status == reason
                ),
            }
            for (instrument, reason), aliases in by_issue.items()
        ),
        key=lambda g: (-g["wallet_count"], -g["source_book_samples"], g["instrument"]),
    )
    wallet_counts_by_market = {
        (row["instrument"], row["reason"]): row["wallet_count"] for row in gaps
    }
    acquisition_queue = sorted(
        (
            {
                "instrument": instrument,
                "provider_symbol": (
                    ":".join(instrument.split(":")[3:])
                    if instrument.split(":")[3] != "default"
                    else instrument.rsplit(":", 1)[-1]
                ),
                "date_utc": day,
                "reason": status,
                "affected_wallet_count": wallet_counts_by_market.get((instrument, status), 0),
            }
            for (instrument, day), status in per_day_cache.items()
            if status != "SOURCE_DAY_SHA_VERIFIED"
        ),
        key=lambda row: (-row["affected_wallet_count"], row["instrument"], row["date_utc"]),
    )
    local_tick_candidates = []
    for request in acquisition_queue:
        symbol = request["provider_symbol"]
        day = request["date_utc"]
        year, month, date = day.split("-")
        for tick_symbol in (symbol, symbol.upper()):
            tick = (
                market_root
                / f"crypto_futures_tick_HYPERLIQUID_{tick_symbol}_PERP"
                / f"year={year}/month={month}/day={date}/data.parquet"
            )
            if tick.is_file():
                local_tick_candidates.append(request)
                break
    return {
        "schema": "pm01_native_1m_36_wallet_coverage_v1",
        "latency_minutes_covered": list(LATENCY_MINUTES),
        "terminal_position_mark_day_included": True,
        "coverage_is_conservative_day_level_union_not_exact_execution": True,
        "local_unverified_tick_day_materialization_candidates": local_tick_candidates,
        "local_tick_candidate_count": len(local_tick_candidates),
        "acquisition_requests_are_not_provider_availability_claims": True,
        "missing_or_invalid_instrument_day_queue": acquisition_queue,
        "wallet_count": len(entries),
        "source_manifest_shas": fingerprints,
        "input": "HYPERX_FILLS_ONLY_RUST_INDEPENDENT",
        "study_cutoff": CUT.isoformat(),
        "price_scope": "HYPERLIQUID_NATIVE_1M_ONLY",
        "read_only": True,
        "day_integrity_does_not_prove_exact_executable_minute": True,
        "unique_instrument_days_audited": len(per_day_cache),
        "sha_verified_instrument_days": sum(
            v == "SOURCE_DAY_SHA_VERIFIED" for v in per_day_cache.values()
        ),
        "missing_or_invalid_instrument_days": sum(
            v != "SOURCE_DAY_SHA_VERIFIED" for v in per_day_cache.values()
        ),
        "book_sample_status_counts": dict(sorted(counts.items())),
        "gaps_by_market": gaps,
        "wallets": wallets,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--research-root", type=Path, required=True)
    p.add_argument("--market-root", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    report = inventory(args.research_root, args.market_root)
    path, digest = _write_private_evidence(args.out, report)
    print(
        json.dumps(
            {
                "wallets": report["wallet_count"],
                "instrument_days": report["unique_instrument_days_audited"],
                "sha_verified_days": report["sha_verified_instrument_days"],
                "missing_or_invalid_days": report["missing_or_invalid_instrument_days"],
                "top_missing": report["gaps_by_market"][:10],
                "evidence_sha256": digest,
                "evidence_path": str(path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
