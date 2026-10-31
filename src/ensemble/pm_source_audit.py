"""Independent read-only HyperX page and live Hyperliquid minute-source audit.

Run separately from PM research to establish whether source manifests still
match physical cached bytes. Each relevant journal page is authenticated
against sidecar SHA, decompressed payload SHA, identity, instrument and per-page
economic trade IDs. No API tokens, addresses or live trading authority.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .pm_research import _write_private_evidence
from .pm_sources import CohortEntry, load_cohort


PAGE_CACHE = Path("slow_pod_data_v1/hyperx_fills_cache_v1")
PAGE_LIMIT = 2400
FILL_LIMIT = 450000


def audit_pages(root: Path, entries: tuple[CohortEntry, ...]) -> dict[str, Any]:
    """Every cached journal page for the 36 aliases, fail closed on any mismatch."""
    cache = root / PAGE_CACHE
    by_alias = {e.alias: e for e in entries}
    expected_pair = {(e.alias, e.cohort) for e in entries}
    if len(by_alias) != 36 or len(expected_pair) != 36:
        raise ValueError("incomplete or colliding audit cohort")
    if any(e.vendor_page_count <= 0 for e in entries):
        raise ValueError("cannot verify an alias without vendor source pages")
    seen: dict[tuple[str, str, int], str] = {}
    page_count = Counter()
    fill_count = Counter()
    pair_coins = defaultdict(set)
    pair_trade_ids = defaultdict(set)
    after_cutoff = Counter()
    cutoff_ms = 1793294536142
    signature = sha256()
    examined = 0
    total_fills = 0
    journal_path = cache / "append_only_page_journal.jsonl"
    if not journal_path.is_file():
        raise ValueError("required append-only source journal missing")
    with journal_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                raise ValueError("blank line in immutable source journal")
            info = json.loads(line)
            alias = info["alias"]
            if alias not in by_alias:
                continue
            examined += 1
            if examined > PAGE_LIMIT:
                raise ValueError("BOUNDED_PAGE_AUDIT_LIMIT_EXCEEDED")
            coin = info["coin"]
            page = int(info["page"])
            if page < 1 or not isinstance(coin, str) or not coin:
                raise ValueError("invalid source page identity")
            key = (alias, coin, page)
            old = seen.get(key)
            if old is not None:
                if old != info["sha256"]:
                    raise ValueError("source cache changed the same page's bytes")
                raise ValueError("duplicate source journal page record")
            seen[key] = info["sha256"]
            folder = cache / "pages" / alias / quote(coin, safe="")
            sidecar = folder / f"page-{page:05d}.meta.json"
            archive = folder / f"page-{page:05d}.json.gz"
            if not sidecar.is_file() or not archive.is_file():
                raise ValueError(f"missing source page half for {alias}")
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            if meta != info:
                raise ValueError(f"source journal/sidecar mismatch for {alias}")
            raw = gzip.decompress(archive.read_bytes())
            actual = sha256(raw).hexdigest()
            if actual != info["sha256"]:
                raise ValueError(f"source page payload SHA mismatch for {alias}")
            response = json.loads(raw)
            if response.get("page") != page or response.get("page_size") != 2000:
                raise ValueError(f"HyperX pagination contract changed for {alias}")
            rows = response.get("fills")
            if not isinstance(rows, list) or len(rows) > 2000:
                raise ValueError(f"bad vendor fill array for {alias}")
            total_fills += len(rows)
            if total_fills > FILL_LIMIT:
                raise ValueError("BOUNDED_FILL_AUDIT_LIMIT_EXCEEDED")
            tids: set[int] = set()
            previous_time: int | None = None
            for fill in rows:
                if not isinstance(fill, dict):
                    raise ValueError(f"invalid vendor fill row for {alias}")
                if fill.get("user", "").lower() != by_alias[alias].address:
                    raise ValueError(f"source fill address conflict for {alias}")
                if fill.get("coin") != coin:
                    raise ValueError(f"source instrument mismatch for {alias}")
                tid = fill.get("tid")
                clock = fill.get("time")
                if isinstance(tid, bool) or not isinstance(tid, int) or tid in tids:
                    raise ValueError(f"duplicate/invalid source TID for {alias}")
                if isinstance(clock, bool) or not isinstance(clock, int):
                    raise ValueError(f"missing original source event time for {alias}")
                if previous_time is not None and clock > previous_time:
                    raise ValueError(f"source pagination not newest-first for {alias}")
                previous_time = clock
                tids.add(tid)
                if tid in pair_trade_ids[(alias, coin)]:
                    raise ValueError(f"cross-page duplicate TID for {alias}")
                pair_trade_ids[(alias, coin)].add(tid)
                if clock > cutoff_ms:
                    after_cutoff[alias] += 1
            signature.update(
                json.dumps([alias, coin, page, actual], separators=(",", ":")).encode()
            )
            signature.update(b"\n")
            page_count[alias] += 1
            fill_count[alias] += len(rows)
            pair_coins[alias].add(coin)
    if examined == 0:
        raise ValueError("no source pages for requested cohort")
    wallet_rows: list[dict[str, Any]] = []
    for entry in entries:
        if page_count[entry.alias] < entry.vendor_page_count:
            raise ValueError(f"{entry.alias}: incomplete source page cache")
        wallet_rows.append(
            {
                "alias": entry.alias,
                "cohort": entry.cohort,
                "page_count_verified": page_count[entry.alias],
                "active_instruments_verified": len(pair_coins[entry.alias]),
                "raw_fill_rows_verified": fill_count[entry.alias],
                "post_frozen_cutoff_rows_excluded": after_cutoff[entry.alias],
                "historical_known_at": "UNKNOWN_VENDOR_BACKFILL",
                "vendor_roster_state": entry.historical_state,
            }
        )
    return {
        "schema": "pm01_independent_36_source_page_audit_v1",
        "wallet_count": len(entries),
        "source_page_sha_chain": signature.hexdigest(),
        "pages_verified": examined,
        "raw_fill_rows_verified": total_fills,
        "post_frozen_cutoff_rows_excluded": sum(after_cutoff.values()),
        "cohort": wallet_rows,
        "source_account_pnl_reconciled": False,
        "first_known_historical_vendor_fills": False,
        "read_only": True,
        "validation": "PASS_IMMUTABLE_BYTES_AND_IDS_NOT_WHOLE_ACCOUNT_COVERAGE",
    }


def audit_w019_fresh_lit(root: Path, entry: CohortEntry) -> dict[str, Any]:
    """Verify the *new* vendor generation that closed the older W019/LIT gap.

    Original cached page1/page2 must match the new-generation vendor bytes,
    while its page3 is independently verified and never backdated.
    """
    generation = (
        root / "slow_pod_data_v1/wave_b_five_month_v1/w019_lit_new_generation/"
        "generation-20261030T120153628646Z-1551b1a"
    )
    manifest = json.loads((generation / "generation_manifest.json").read_text())
    expected_pages = manifest.get("source_page_sha_list", [])
    if len(expected_pages) != 3 or entry.alias != "W019":
        raise ValueError("W019 addendum manifest/identity mismatch")
    old_cache = root / "slow_pod_data_v1/hyperx_fills_cache_v1/pages/W019/LIT"
    all_ids: set[int] = set()
    overlap_ids: set[int] = set()
    fills_count = 0
    for n, expected_sha, count, oldest_ms in expected_pages:
        path = generation / f"page-{n:05d}.json.gz"
        sidecar = generation / f"page-{n:05d}.meta.json"
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        original = gzip.decompress(path.read_bytes())
        if (
            meta.get("wallet_alias") != "W019"
            or meta.get("coin") != "LIT"
            or meta.get("page") != n
            or meta.get("sha256") != expected_sha
            or sha256(original).hexdigest() != expected_sha
        ):
            raise ValueError("W019 new-generation immutable source SHA/identity mismatch")
        response = json.loads(original)
        rows = response.get("fills")
        if not isinstance(rows, list) or len(rows) != count:
            raise ValueError("W019 new-generation pagination row drift")
        if response.get("page") != n or response.get("page_size") != 2000:
            raise ValueError("W019 new-generation provider page mismatch")
        if bool(response.get("has_more")) != (n < 3):
            raise ValueError("W019 new-generation pagination not exhausted")
        if meta.get("source_known_at") not in {"UNKNOWN", "UNAVAILABLE", None}:
            raise ValueError("W019 vendor historical fills falsely assigned known-at")
        times: list[int] = []
        for fill in rows:
            if fill.get("user", "").lower() != entry.address or fill.get("coin") != "LIT":
                raise ValueError("W019 addendum source wallet/coin conflict")
            tid, tm = fill.get("tid"), fill.get("time")
            if isinstance(tid, bool) or not isinstance(tid, int) or tid in all_ids:
                raise ValueError("W019 repeated TID in fresh source")
            if not isinstance(tm, int) or isinstance(tm, bool):
                raise ValueError("W019 malformed source time")
            all_ids.add(tid)
            times.append(tm)
        if times and (min(times) != oldest_ms or times != sorted(times, reverse=True)):
            raise ValueError("W019 new generation time ordering/source boundary mismatch")
        fills_count += len(rows)
        if n <= 2:
            old_raw = gzip.decompress((old_cache / f"page-{n:05d}.json.gz").read_bytes())
            previous = json.loads(old_raw)
            previous_fills = {x["tid"]: x for x in previous["fills"]}
            for row in rows:
                if previous_fills.get(row["tid"]) != row:
                    raise ValueError("W019 cross-generation vendor source changed")
                overlap_ids.add(row["tid"])
    if fills_count != 5165 or len(overlap_ids) != 4000:
        raise ValueError("W019 fresh 3-page continuity mismatch")
    if manifest.get("cross_generation_original_TID_payload_mismatches") != 0:
        raise ValueError("W019 vendor generation reports source conflict")
    return {
        "alias": "W019",
        "fresh_vendor_pages_verified": 3,
        "fresh_vendor_raw_fill_rows": fills_count,
        "cross_generation_identical_rows": len(overlap_ids),
        "additional_distinct_vendor_fill_rows": fills_count - len(overlap_ids),
        "five_month_vendor_listed_LIT_pagination_exhausted": True,
        "historical_first_known_at": "UNKNOWN",
        "whole_account_coverage_verified": False,
        "generation_manifest_sha256": sha256(
            (generation / "generation_manifest.json").read_bytes()
        ).hexdigest(),
    }


def audit_market_defects(market_root: Path, max_days: int = 3000) -> dict[str, Any]:
    """Recheck previously reported SP500/XYZ100 source.json/Parquet mismatches."""
    prefix = market_root / "crypto_futures_ohlcv_1m_HYPERLIQUID_XYZ_PERP"
    result: dict[str, Any] = {}
    for symbol in ("SP500", "XYZ100"):
        folder = prefix / ("symbol=" + symbol)
        mismatches = []
        checked = 0
        for meta in sorted(folder.rglob("source.json")) if folder.exists() else []:
            checked += 1
            if checked > max_days:
                raise ValueError("BOUNDED_MARKET_DAY_LIMIT_EXCEEDED")
            expected = json.loads(meta.read_text(encoding="utf-8")).get("sha256")
            file = meta.with_name("data.parquet")
            actual = sha256(file.read_bytes()).hexdigest() if file.is_file() else None
            if expected != actual:
                mismatches.append(meta.parent.relative_to(prefix).as_posix())
        result[symbol] = {
            "days_checked": checked,
            "live_lake_SHA_mismatch_count": len(mismatches),
            "mismatched_source_days": mismatches,
            "market_qualified": checked > 0 and not mismatches,
        }
    return {
        "schema": "pm01_hip3_market_source_defect_recheck_v1",
        "venues": "HYPERLIQUID_XYZ_PERP_ONLY",
        "instruments": result,
        "no_unverified_market_day_substitution": True,
    }


def audit(root: Path, market_root: Path) -> dict[str, Any]:
    entries, source_hashes = load_cohort(root)
    return {
        "source_contract_sha256": source_hashes,
        "hyperx_raw_cache": audit_pages(root, entries),
        "w019_new_generation": audit_w019_fresh_lit(
            root, next(e for e in entries if e.alias == "W019")
        ),
        "market_defect_recheck": audit_market_defects(market_root),
        "authority": "RESEARCH_ONLY_NO_ORDER_OR_PRODUCTION_MUTATION",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent immutable 36-wallet source audit")
    parser.add_argument("--research-root", type=Path, required=True)
    parser.add_argument("--market-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    options = parser.parse_args()
    result = audit(options.research_root, options.market_root)
    file, digest = _write_private_evidence(options.out, result)
    print(
        json.dumps(
            {
                "wallets": result["hyperx_raw_cache"]["wallet_count"],
                "pages": result["hyperx_raw_cache"]["pages_verified"],
                "raw_fill_rows": result["hyperx_raw_cache"]["raw_fill_rows_verified"],
                "w019_additional_verified_rows": result["w019_new_generation"][
                    "additional_distinct_vendor_fill_rows"
                ],
                "post_cutoff_rows_excluded": result["hyperx_raw_cache"][
                    "post_frozen_cutoff_rows_excluded"
                ],
                "live_market_source_mismatches": {
                    k: v["live_lake_SHA_mismatch_count"]
                    for k, v in result["market_defect_recheck"]["instruments"].items()
                },
                "evidence_sha256": digest,
                "evidence_file": str(file),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
