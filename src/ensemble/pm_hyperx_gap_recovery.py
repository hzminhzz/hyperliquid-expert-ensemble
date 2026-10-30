"""Bounded authenticated HyperX resnapshot for PM-01 fill-chain gaps.

Uses the existing VPS HyperX Data API credential, never the trading API.
Fetches complete new source page generations with immutable gzip+sidecar SHA.
Does not overwrite, patch, or silently extend original immutable caches.
Raw addresses, keys and fill payloads remain only in owner-private disk.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import UTC, datetime
import gzip
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import time

from .pm_fill_replay import _coins, _source_fills, _same_position
from .pm_sources import load_cohort


DEFAULT_ALIASES = (
    "W035",
    "W041",
    "W028",
    "EQ-001",
    "CO-001",
    "W033",
    "W019",
    "CA-008",
    "VS083",
)
SOURCE_ADAPTER = "slow_pod_hyperx_cache.py"
SOURCE_SHA = "e03d5ec1fc5cc626021d599a6cd8fee8dd7c79b826f2c925120b9cbf15cdf042"
FROZEN_CUTOFF_MS = 1793294536142
MAX_REQUESTS = 30
MAX_PAGES_PER_PAIR = 4
MIN_INTERVAL = 12.0


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_mode & 0o077:
        raise ValueError("PRIVATE_OUTPUT_DIRECTORY_REQUIRED")


def _atomic(path: Path, data: bytes) -> None:
    _private_dir(path.parent)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("IMMUTABLE_GENERATION_CONFLICT")
        return
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Source page generation is private; never overwrite existing pages.
        if path.exists():
            raise ValueError("SOURCE_PAGE_ALREADY_PRESENT")
        os.link(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _adapter(root: Path):
    source = root / SOURCE_ADAPTER
    if sha256(source.read_bytes()).hexdigest() != SOURCE_SHA:
        raise ValueError("HYPERX_ADAPTER_SHA_DRIFT")
    spec = importlib.util.spec_from_file_location("pm01_pinned_hyperx_provider", source)
    if spec is None or spec.loader is None:
        raise ValueError("MISSING_AUTHORIZED_HYPERX_ADAPTER")
    from importlib.util import module_from_spec

    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.source_cutoff_ms() != FROZEN_CUTOFF_MS:
        raise ValueError("HYPERX_FROZEN_CUTOFF_DRIFT")
    return module


def _gaps(root: Path, entry) -> dict[str, list[dict]]:
    fills, errors = _source_fills(root, entry.alias, entry.address, _coins(root, entry.alias))
    if any(not e.endswith(":POSITION_DISCONTINUITY") for e in errors):
        raise ValueError("PREEXISTING_SOURCE_ERRORS_REQUIRE_INVESTIGATION")
    rows = defaultdict(list)
    for fill in fills:
        rows[fill[2]].append(fill)
    out = {}
    for coin, events in rows.items():
        issues = []
        for a, b in zip(events, events[1:]):
            if not _same_position(a[4], b[3]):
                issues.append(
                    {
                        "previous_tid": a[1],
                        "next_tid": b[1],
                        "previous_event_ms": int(a[0].timestamp() * 1000),
                        "next_event_ms": int(b[0].timestamp() * 1000),
                        "unexplained_quantity_delta": str(b[3] - a[4]),
                    }
                )
        if issues:
            out[coin] = issues
    return out


def _generation_pages(dirpath: Path, alias: str, coin: str) -> list[dict]:
    all_rows = []
    page = 1
    while True:
        archive = dirpath / f"page-{page:05d}.json.gz"
        sidecar = dirpath / f"page-{page:05d}.meta.json"
        if not archive.exists() and not sidecar.exists():
            break
        if not archive.is_file() or not sidecar.is_file():
            raise ValueError("BROKEN_GENERATION_PAGE_PAIR")
        raw = gzip.decompress(archive.read_bytes())
        metadata = json.loads(sidecar.read_text())
        if (
            metadata.get("sha256") != sha256(raw).hexdigest()
            or metadata.get("alias") != alias
            or metadata.get("coin") != coin
            or metadata.get("page") != page
        ):
            raise ValueError("GENERATION_PAGE_IDENTITY_MISMATCH")
        obj = json.loads(raw)
        all_rows.append(obj)
        page += 1
    return all_rows


def _post_audit(root: Path, entry, coin: str, new_pages: list[dict]) -> dict:
    """Only overlapping source-record equality and additions; no inferred fixes."""
    original = root / "slow_pod_data_v1/hyperx_fills_cache_v1/pages" / entry.alias
    from urllib.parse import quote

    folder = original / quote(coin, safe="")
    old_rows = {}
    for p in sorted(folder.glob("page-*.json.gz")):
        obj = json.loads(gzip.decompress(p.read_bytes()))
        for row in obj["fills"]:
            tid = row["tid"]
            existing = old_rows.get(tid)
            if existing is not None and existing != row:
                raise ValueError("CONFLICTING_ORIGINAL_TID")
            old_rows[tid] = row
    fresh_rows = {}
    for obj in new_pages:
        for row in obj.get("fills", []):
            if row.get("user", "").lower() != entry.address or row.get("coin") != coin:
                raise ValueError("FRESH_WALLET_OR_COIN_MISMATCH")
            tid = row["tid"]
            if tid in fresh_rows and fresh_rows[tid] != row:
                raise ValueError("CONFLICTING_FRESH_TID")
            fresh_rows[tid] = row
    overlap = set(old_rows) & set(fresh_rows)
    conflicts = sum(old_rows[t] != fresh_rows[t] for t in overlap)
    new = set(fresh_rows) - set(old_rows)
    old_only = set(old_rows) - set(fresh_rows)
    result = {
        "alias": entry.alias,
        "coin": coin,
        "original_unique_tids": len(old_rows),
        "fresh_unique_tids": len(fresh_rows),
        "overlap_count": len(overlap),
        "conflicting_identical_tid_payloads": conflicts,
        "new_tids": len(new),
        "original_tids_not_in_fresh_pages": len(old_only),
        "new_fills_before_cutoff": sum(fresh_rows[t]["time"] <= FROZEN_CUTOFF_MS for t in new),
        "fresh_pages": len(new_pages),
        "newest_fresh_event_ms": max((r["time"] for r in fresh_rows.values()), default=None),
        "oldest_fresh_event_ms": min((r["time"] for r in fresh_rows.values()), default=None),
        "fresh_pagination_exhausted": bool(new_pages and new_pages[-1].get("has_more") is False),
    }
    # Important: when source changed overlapping TIDs, do not automatically
    # interpret an alleged recovery as history improvement.
    result["overlay_eligible"] = conflicts == 0 and result["fresh_pagination_exhausted"]
    return result


def recover(
    root: Path,
    output: Path,
    *,
    aliases: tuple[str, ...],
    pairs: tuple[str, ...] | None = None,
    max_calls: int = 18,
    max_pages: int = 3,
    min_interval: float = MIN_INTERVAL,
) -> dict:
    if not 1 <= max_calls <= MAX_REQUESTS:
        raise ValueError("MAX_HYPERX_CALLS_EXCEEDED")
    if not 1 <= max_pages <= MAX_PAGES_PER_PAIR:
        raise ValueError("MAX_HYPERX_PAGES_EXCEEDED")
    if min_interval < MIN_INTERVAL:
        raise ValueError("HYPERX_MIN_INTERVAL_TOO_FAST")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", output.name):
        raise ValueError("INVALID_GENERATION_DIRECTORY_NAME")
    roster, _ = load_cohort(root)
    by_alias = {e.alias: e for e in roster}
    if len(set(aliases)) != len(aliases) or any(a not in by_alias for a in aliases):
        raise ValueError("DUPLICATED_OR_UNKNOWN_ALIAS")
    _private_dir(output)
    provider = _adapter(root)
    token = provider.key_from_existing_tracker_env()
    selected = [
        (a, coin, gaps)
        for a in aliases
        for coin, gaps in _gaps(root, by_alias[a]).items()
        if pairs is None or f"{a}/{coin}" in pairs
    ]
    if pairs is not None and set(pairs) != {f"{a}/{coin}" for a, coin, _ in selected}:
        raise ValueError("SELECTED_PAIR_NOT_PRESENT_IN_ORIGINAL_GAPS")
    # Prioritize 2026 gaps; carry 2025 sources only if API budget remains.
    selected.sort(key=lambda x: (-max(g["next_event_ms"] for g in x[2]), x[0], x[1]))
    statuses = []
    calls = 0
    last = 0.0
    for alias, coin, gaps in selected:
        entry = by_alias[alias]
        target_dir = output / alias / provider.safe_coin(coin)
        _private_dir(target_dir)
        fresh = _generation_pages(target_dir, alias, coin)
        completed = bool(fresh and fresh[-1].get("has_more") is False)
        error = None
        while not completed and len(fresh) < max_pages and calls < max_calls:
            if last:
                time.sleep(max(0.0, min_interval - (time.monotonic() - last)))
            page = len(fresh) + 1
            last = time.monotonic()
            calls += 1
            try:
                obj, raw, rate = provider.provider_request(alias, entry.address, coin, page, token)
                valid = provider.validate(obj, alias, coin, page, entry.address)
                metadata = {
                    "schema": "pm01_hyperx_gap_generation_page_v1",
                    "alias": alias,
                    "coin": coin,
                    "page": page,
                    "sha256": sha256(raw).hexdigest(),
                    "frozen_research_cutoff_ms": FROZEN_CUTOFF_MS,
                    "retrieved_at_utc": datetime.now(UTC).isoformat(),
                    "validation": valid,
                    "rate_budget_remaining": rate.get("x-ratelimit-remaining"),
                }
                _atomic(target_dir / f"page-{page:05d}.json.gz", gzip.compress(raw, mtime=0))
                _atomic(
                    target_dir / f"page-{page:05d}.meta.json",
                    (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode(),
                )
                fresh.append(obj)
                completed = obj.get("has_more") is False
                # Provider budget below a floor => stop after this page.
                remaining = rate.get("x-ratelimit-remaining")
                if remaining is not None and int(remaining) < 15:
                    error = "PROVIDER_RATE_BUDGET_LOW"
                    break
            except Exception as exc:
                error = "HYPERX_REQUEST_FAILED:" + type(exc).__name__ + ":" + str(exc)[:90]
                break
        if fresh:
            audit = _post_audit(root, entry, coin, fresh)
        else:
            audit = {
                "alias": alias,
                "coin": coin,
                "fresh_pages": 0,
                "new_tids": None,
                "overlay_eligible": False,
            }
        audit.update(
            {
                "preexisting_gap_count": len(gaps),
                "preexisting_unexplained_deltas": [g["unexplained_quantity_delta"] for g in gaps],
                "recovery_error": error,
                "requested_page_cap": max_pages,
            }
        )
        statuses.append(audit)
        if calls >= max_calls or error == "PROVIDER_RATE_BUDGET_LOW":
            break
    report = {
        "schema": "pm01_fresh_hyperx_gap_generation_v1",
        "provider": "HyperX authenticated Data API (NOT trading API)",
        "all_source_aliases": list(aliases),
        "planned_gap_pairs": len(selected),
        "retrieved_gap_pairs": len(statuses),
        "api_requests": calls,
        "api_request_limit": max_calls,
        "frozen_cutoff_ms": FROZEN_CUTOFF_MS,
        "original_cache_modified": False,
        "rows": statuses,
    }
    _atomic(
        output / "generation-summary.json",
        (json.dumps(report, sort_keys=True, indent=2) + "\n").encode(),
    )
    return report


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--research-root", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--aliases", nargs="+", default=list(DEFAULT_ALIASES))
    p.add_argument("--max-calls", type=int, default=18)
    p.add_argument("--pairs", nargs="+")
    p.add_argument("--max-pages", type=int, default=3)
    p.add_argument("--min-interval", type=float, default=MIN_INTERVAL)
    args = p.parse_args()
    report = recover(
        args.research_root,
        args.out,
        aliases=tuple(args.aliases),
        pairs=tuple(args.pairs) if args.pairs is not None else None,
        max_calls=args.max_calls,
        max_pages=args.max_pages,
        min_interval=args.min_interval,
    )
    print(
        json.dumps(
            {
                "requests": report["api_requests"],
                "pairs_retrieved": report["retrieved_gap_pairs"],
                "planned_pairs": report["planned_gap_pairs"],
                "result": [
                    {
                        k: r.get(k)
                        for k in (
                            "alias",
                            "coin",
                            "fresh_pages",
                            "fresh_unique_tids",
                            "new_tids",
                            "new_fills_before_cutoff",
                            "conflicting_identical_tid_payloads",
                            "fresh_pagination_exhausted",
                            "preexisting_gap_count",
                            "overlay_eligible",
                            "recovery_error",
                        )
                    }
                    for r in report["rows"]
                ],
                "report_private": str(args.out / "generation-summary.json"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
