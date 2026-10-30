"""PM-01 historical HyperX fill-to-native-1m replay, independent of Rust.

Vendor fill timestamps are *oracle historical actions*, not prospective signals.
Failures in source continuity, missing price days or initial positions block a
wallet's entire performance estimate; no missing legs are silently dropped.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import gzip
import json
from pathlib import Path
from urllib.parse import quote

from .pm_sources import load_cohort
from .pm_shadow import MarketDataBlocked, VerifiedHyperliquidMinuteSource, Scenario, _VirtualBook
from .pm_research import _write_private_evidence

ZERO = Decimal(0)
CUT = datetime(2026, 10, 29, 17, 22, 16, 142000, tzinfo=UTC)


def _read_pair(root: Path, alias: str, address: str, coin: str, page_limit: int = 150):
    path = root / "slow_pod_data_v1/hyperx_fills_cache_v1/pages" / alias / quote(coin, safe="")
    all_fills = []
    seen = set()
    complete = False
    for page in range(1, page_limit + 1):
        file = path / f"page-{page:05d}.json.gz"
        meta_path = path / f"page-{page:05d}.meta.json"
        if not file.is_file() or not meta_path.is_file():
            if page == 1:
                raise ValueError("missing cached pair first page")
            break
        raw = gzip.decompress(file.read_bytes())
        meta = json.loads(meta_path.read_text())
        if sha256(raw).hexdigest() != meta.get("sha256"):
            raise ValueError("immutable page SHA mismatch")
        document = json.loads(raw)
        if document.get("page") != page or document.get("page_size") != 2000:
            raise ValueError("source page index mismatch")
        for f in document["fills"]:
            if f["coin"] != coin or f["user"].lower() != address:
                raise ValueError("source wallet/coin mismatch")
            tid = f["tid"]
            if tid in seen:
                raise ValueError("duplicate pair trade id")
            seen.add(tid)
            if datetime.fromtimestamp(f["time"] / 1000, UTC) <= CUT:
                side = f.get("side")
                if side not in ("A", "B"):
                    raise ValueError("source side missing")
                size = Decimal(str(f["sz"]))
                start = Decimal(str(f["start_position"]))
                price = Decimal(str(f["px"]))
                if size <= ZERO or price <= ZERO:
                    raise ValueError("invalid source fill")
                delta = size if side == "B" else -size
                all_fills.append(
                    (
                        datetime.fromtimestamp(f["time"] / 1000, UTC),
                        int(tid),
                        coin,
                        start,
                        start + delta,
                        delta,
                        price,
                    )
                )
        complete = document.get("has_more") is False
        if complete:
            break
    if not complete:
        raise ValueError("source pagination not exhausted")
    return all_fills


def _source_fills(root: Path, alias: str, address: str, coins: list[str]):
    events = []
    blockers = []
    for coin in coins:
        try:
            events.extend(_read_pair(root, alias, address, coin))
        except (ValueError, OSError, KeyError, TypeError) as ex:
            blockers.append(f"{coin}:{type(ex).__name__}")
    # Same-ms order is not always TID order. Reject ambiguous continuity.
    by_coin = defaultdict(list)
    for e in events:
        by_coin[e[2]].append(e)
    results = []
    for coin, rows in by_coin.items():
        rows.sort(key=lambda e: (e[0], e[1]))
        prev = None
        for _, group in __import__("itertools").groupby(rows, key=lambda e: e[0]):
            remaining = list(group)
            while remaining:
                linked = [
                    x
                    for x in remaining
                    if prev is not None
                    and abs(x[3] - prev) <= Decimal("0.0000001") * max(Decimal(1), abs(prev))
                ]
                if linked:
                    pick = min(linked, key=lambda x: x[1])
                else:
                    start_rows = [
                        x
                        for x in remaining
                        if not any(
                            abs(y[4] - x[3]) < Decimal("0.0000001") for y in remaining if y is not x
                        )
                    ]
                    pick = min(start_rows or remaining, key=lambda x: x[1])
                remaining.remove(pick)
                if prev is not None and abs(pick[3] - prev) > Decimal("0.0000001") * max(
                    Decimal(1), abs(prev)
                ):
                    blockers.append(f"{coin}:POSITION_DISCONTINUITY")
                prev = pick[4]
                results.append(pick)
    results.sort(key=lambda x: (x[0], x[1]))
    return results, sorted(set(blockers))


def _coins(root: Path, alias: str):
    path = root / "slow_pod_data_v1/hyperx_fills_cache_v1/pages" / alias
    if not path.exists():
        raise ValueError("wallet source pages missing")
    found = []
    for f in path.rglob("page-00001.meta.json"):
        meta = json.loads(f.read_text())
        if meta.get("alias") != alias:
            raise ValueError("source alias mismatch")
        found.append(meta["coin"])
    if len(found) != len(set(found)) or not found:
        raise ValueError("missing or duplicate source coin")
    return sorted(found)


def replay_wallet(
    root: Path,
    entry,
    source: VerifiedHyperliquidMinuteSource,
    latency: int,
    capital: Decimal = Decimal("100000"),
    risk_budget: Decimal = Decimal("0.10"),
):
    coins = _coins(root, entry.alias)
    fills, issues = _source_fills(root, entry.alias, entry.address, coins)
    history = {
        "wallet": entry.alias,
        "cohort": entry.cohort,
        "instruments": len(coins),
        "source_fills": len(fills),
        "known_at": "UNAVAILABLE_VENDOR_HISTORICAL",
        "latency_minutes": latency,
        "scope": "VENDOR_REPORTED_INSTRUMENTS_ONLY",
        "oracle_hindsight": True,
        "blocked_reasons": issues,
    }
    if not fills or issues:
        return {**history, "status": "BLOCKED_SOURCE_INCOMPLETE", "pnl": None}
    # A vendor-listed instrument may be left-censored. Reconstruct its
    # signed quantity from the first observed start_position and only begin
    # the follower after *every listed instrument* has been observed flat
    # simultaneously. This excludes the unknown prehistory without
    # pretending the source started with zero positions.
    first = {}
    for event in fills:
        first.setdefault(event[2], event[3])
    seed = dict(first)
    trusted_start = None
    for ix, event in enumerate(fills):
        coin, before, after = event[2], event[3], event[4]
        if abs(seed[coin] - before) > Decimal("0.0000001") * max(Decimal(1), abs(before)):
            return {**history, "status": "BLOCKED_SOURCE_CONTINUITY", "pnl": None}
        seed[coin] = after
        if all(q == ZERO for q in seed.values()):
            trusted_start = ix + 1
            break
    if trusted_start is None or trusted_start >= len(fills):
        return {
            **history,
            "status": "BLOCKED_NO_OBSERVED_FLAT_RESET",
            "pnl": None,
            "nonflat_initial_instruments": sum(v != ZERO for v in first.values()),
        }
    history["warmup_fills_excluded"] = trusted_start
    book = _VirtualBook()
    control = _VirtualBook()
    source_book = {coin: ZERO for coin in coins}
    last_time = None
    last_marks = {}
    first_timestamp = fills[trusted_start][0]
    model = Scenario("HYPERX_HISTORICAL_FILL_ORACLE", latency, capital, risk_budget)
    for when, tid, coin, before, after, delta, source_px in fills[trusted_start:]:
        if source_book[coin] != before:
            return {**history, "status": "BLOCKED_SOURCE_CONTINUITY", "pnl": None}
        source_book[coin] = after
        action_at = when + timedelta(minutes=latency)
        if action_at > CUT:
            return {**history, "status": "BLOCKED_POST_CUTOFF_ACTION", "pnl": None}
        # Price and liquidity checks across every open leg at every action.
        instrument_names = {
            c: (
                "hyperliquid:mainnet:perp:default:" + c
                if ":" not in c and not c.startswith("#")
                else "hyperliquid:mainnet:perp:" + c
            )
            for c in coins
        }
        active = {instrument_names[c]: q for c, q in source_book.items() if q != ZERO}
        needed = sorted(set(active) | set(book.positions) | set(control.positions))
        try:
            quotes = {name: source.get(name, action_at) for name in needed}
            marks = {name: v.open for name, v in quotes.items()}
            # A *whole-book* gross risk normalization; no per-leg independent
            # sizing, and no source account equity hallucinated.
            gross = sum((abs(q * marks[name]) for name, q in active.items()), ZERO)
            scale = model.initial_cash * model.portfolio_multiplier / gross if gross else ZERO
            target = {name: q * scale for name, q in active.items()}
            long_control = {name: abs(q) for name, q in target.items()}
            if last_time is not None:
                elapsed = source._minute(action_at) - last_time
                if elapsed < timedelta():
                    raise ValueError("nonmonotonic follower event")
                book.accrue_funding(marks, elapsed, model)
                control.accrue_funding(marks, elapsed, model)
            for b, tgt in ((book, target), (control, long_control)):
                for name in needed:
                    d = tgt.get(name, ZERO) - b.positions.get(name, ZERO)
                    if d and (
                        quotes[name].quote_volume <= ZERO
                        or abs(d * marks[name])
                        > quotes[name].quote_volume * model.max_minute_participation
                    ):
                        raise MarketDataBlocked("VOLUME_PARTICIPATION_LIMIT")
            book.rebalance(target, quotes, model)
            control.rebalance(long_control, quotes, model)
            last_time = source._minute(action_at)
            last_marks = marks
        except (MarketDataBlocked, ValueError) as exc:
            return {
                **history,
                "status": "BLOCKED_MARKET_OR_EXECUTION",
                "reason": str(exc),
                "pnl": None,
            }
    attributed = book.pnl_by_instrument(last_marks)
    pnl = sum(attributed.values(), ZERO)
    marked = sum((q * last_marks[name] for name, q in book.positions.items()), ZERO)
    cash = capital + sum(book.flows.values(), ZERO)
    if cash + marked - capital != pnl:
        raise AssertionError("follower balance does not reconcile")
    return {
        **history,
        "status": "HISTORICAL_HINDSIGHT_SIMULATION_ONLY",
        "pnl": str(pnl),
        "return_on_initial_capital": str(pnl / capital),
        "always_long_control_pnl": str(sum(control.pnl_by_instrument(last_marks).values(), ZERO)),
        "fees": str(book.fees),
        "funding_assumed": str(book.funding),
        "turnover": str(book.turnover),
        "fill_count": book.fills,
        "open_positions": sum(bool(q) for q in book.positions.values()),
        "first_event": first_timestamp.isoformat(),
        "last_event": fills[-1][0].isoformat(),
    }


def run(root: Path, market_root: Path):
    entries, fingerprints = load_cohort(root)
    source = VerifiedHyperliquidMinuteSource(market_root)
    rows = []
    for entry in entries:
        for lag in (5, 15, 60):
            rows.append(replay_wallet(root, entry, source, lag))
    return {
        "schema": "pm01_fill_only_replay_v1",
        "wallet_count": len(entries),
        "source_contract_shas": fingerprints,
        "model": "historical-fill-oracle, not prospective known-at",
        "no_rust_dependency": True,
        "never_claim_prospective_alpha": True,
        "rows": rows,
        "market_day_shas": dict(sorted(source.source_days.items())),
        "liquidity_proxy_days": sorted(source.liquidity_proxy_days),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--research-root", type=Path, required=True)
    p.add_argument("--market-root", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    evidence = run(args.research_root, args.market_root)
    file, digest = _write_private_evidence(args.out, evidence)
    counts = Counter(x["status"] for x in evidence["rows"])
    print(
        json.dumps(
            {
                "wallets": evidence["wallet_count"],
                "scenarios": dict(counts),
                "sha256": digest,
                "file": str(file),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
