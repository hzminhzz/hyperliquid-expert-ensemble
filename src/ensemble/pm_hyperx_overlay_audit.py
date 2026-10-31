"""Cryptographic HyperX source overlay and gap requalification for PM-01.

Reads independently fetched owner-private page generations. A new fill may
enter historical replay only when its raw wallet, coin, trade ID, clocks,
position and overlap with original immutable pages verify. No invented fills,
no market prints substituted, no original source overwrite, no Rust.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import quote

from .pm_fill_replay import CUT, STUDY_START, _coins, _source_fills, _same_position
from .pm_sources import load_cohort
from .pm_research import _write_private_evidence

SOURCE_PREFIX = Path("slow_pod_data_v1/hyperx_fills_cache_v1/pages")


def _read_pages(directory: Path, alias: str, coin: str) -> list[dict]:
    pages = []
    for n in range(1, 5):
        archive = directory / f"page-{n:05d}.json.gz"
        meta = directory / f"page-{n:05d}.meta.json"
        if not archive.exists() and not meta.exists():
            break
        if not archive.is_file() or not meta.is_file():
            raise ValueError("PARTIAL_IMMUTABLE_SOURCE_PAIR")
        raw = gzip.decompress(archive.read_bytes())
        sidecar = json.loads(meta.read_text())
        if (
            sha256(raw).hexdigest() != sidecar.get("sha256")
            or sidecar.get("alias") != alias
            or sidecar.get("coin") != coin
            or sidecar.get("page") != n
        ):
            raise ValueError("SOURCE_GENERATION_HASH_OR_IDENTITY_DRIFT")
        obj = json.loads(raw)
        if obj.get("page") != n or obj.get("page_size") != 2000:
            raise ValueError("UNVERIFIED_SOURCE_PAGINATION")
        pages.append(obj)
    if (directory / "page-00005.json.gz").exists():
        raise ValueError("UNBOUNDED_SOURCE_PAGES_NOT_SUPPORTED")
    return pages


def _source_tuple(record: dict, alias: str, coin: str, address: str) -> tuple:
    if record.get("user", "").lower() != address or record.get("coin") != coin:
        raise ValueError("FILL_WALLET_OR_INSTRUMENT_MISMATCH")
    tid = record.get("tid")
    tm = record.get("time")
    if type(tid) is not int or type(tm) is not int:
        raise ValueError("MISSING_FILL_ID_OR_TIME")
    side = record.get("side")
    if side not in {"A", "B"}:
        raise ValueError("INVALID_FILL_SIDE")
    size = Decimal(str(record["sz"]))
    before = Decimal(str(record["start_position"]))
    price = Decimal(str(record["px"]))
    if not all(x.is_finite() for x in (size, before, price)) or size <= 0 or price <= 0:
        raise ValueError("INVALID_SOURCE_ECONOMIC_VALUES")
    delta = size if side == "B" else -size
    return datetime.fromtimestamp(tm / 1000, UTC), tid, coin, before, before + delta, delta, price


def _legacy(root: Path, alias: str, coin: str) -> dict[int, dict]:
    directory = root / SOURCE_PREFIX / alias / quote(coin, safe="")
    records = {}
    page = 1
    while True:
        gz = directory / f"page-{page:05d}.json.gz"
        meta = directory / f"page-{page:05d}.meta.json"
        if not gz.is_file() and not meta.is_file():
            break
        if not gz.is_file() or not meta.is_file():
            raise ValueError("PARTIAL_ORIGINAL_CACHE_PAGE")
        raw = gzip.decompress(gz.read_bytes())
        if sha256(raw).hexdigest() != json.loads(meta.read_text()).get("sha256"):
            raise ValueError("ORIGINAL_CACHE_SHA_DRIFT")
        obj = json.loads(raw)
        if obj.get("page") != page or obj.get("page_size") != 2000:
            raise ValueError("ORIGINAL_CACHE_PAGE_DRIFT")
        for r in obj.get("fills", []):
            tid = r["tid"]
            if tid in records and records[tid] != r:
                raise ValueError("ORIGINAL_REUSED_TID_DIFFERENT_PAYLOAD")
            records[tid] = r
        page += 1
    if not records:
        raise ValueError("MISSING_ORIGINAL_SOURCE")
    return records


def _gap_count(rows: list[tuple]) -> dict[str, int]:
    bycoin = defaultdict(list)
    for row in rows:
        bycoin[row[2]].append(row)
    result = {}
    for coin, records in bycoin.items():
        bad = sum(not _same_position(a[4], b[3]) for a, b in zip(records, records[1:]))
        if bad:
            result[coin] = bad
    return result


def _gap_details(rows: list[tuple]) -> list[dict]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row[2]].append(row)
    gaps = []
    for coin, samples in sorted(grouped.items()):
        for earlier, later in zip(samples, samples[1:]):
            if not _same_position(earlier[4], later[3]):
                gaps.append(
                    {
                        "coin": coin,
                        "last_verified_before_utc": earlier[0].isoformat(),
                        "first_unreconciled_after_utc": later[0].isoformat(),
                        "prev_tid": earlier[1],
                        "next_tid": later[1],
                        "prev_after_quantity": str(earlier[4]),
                        "next_before_quantity": str(later[3]),
                        "unexplained_position_delta": str(later[3] - earlier[4]),
                        "same_exchange_millisecond": earlier[0] == later[0],
                    }
                )
    return gaps


def examine(root: Path, generations: tuple[Path, ...]) -> tuple[dict, dict[str, list[tuple]]]:
    entries, root_fingerprints = load_cohort(root)
    indexed = {e.alias: e for e in entries}
    all_extra: dict[str, list[tuple]] = defaultdict(list)
    extra_identifiers = defaultdict(set)
    source_results = []
    source_generations = []
    for path in generations:
        if not path.is_dir():
            raise ValueError("SOURCE_GENERATION_NOT_DIRECTORY")
        summary = path / "generation-summary.json"
        if not summary.is_file():
            raise ValueError("MISSING_AUTHENTICATED_GENERATION_SUMMARY")
        source_generations.append(
            {
                "basename": path.name,
                "generation_summary_sha256": sha256(summary.read_bytes()).hexdigest(),
            }
        )
        for alias_dir in sorted(path.iterdir()):
            if not alias_dir.is_dir():
                continue
            alias = alias_dir.name
            if alias not in indexed:
                raise ValueError("UNKNOWN_SOURCE_GENERATION_ALIAS")
            for coin_dir in sorted(alias_dir.iterdir()):
                if not coin_dir.is_dir():
                    continue
                # Name of coin encoded in percent escaped path, never identity.
                from urllib.parse import unquote

                coin = unquote(coin_dir.name)
                entry = indexed[alias]
                pages = _read_pages(coin_dir, alias, coin)
                if not pages:
                    raise ValueError("EMPTY_GENERATION_PAIR")
                if pages[-1].get("has_more") is not False:
                    raise ValueError("INCOMPLETE_AUTHENTICATED_SOURCE_PAGINATION")
                existing = _legacy(root, alias, coin)
                fresh = {}
                for doc in pages:
                    for fill in doc.get("fills", []):
                        row = _source_tuple(fill, alias, coin, entry.address)
                        tid = row[1]
                        if tid in fresh and fresh[tid] != fill:
                            raise ValueError("CONFLICTING_GENERATION_FILL_TID")
                        fresh[tid] = fill
                overlap = set(existing) & set(fresh)
                if any(existing[tid] != fresh[tid] for tid in overlap):
                    raise ValueError("ORIGINAL_GENERATION_TID_PAYLOAD_CONFLICT")
                new_ids = set(fresh) - set(existing)
                for tid in sorted(new_ids):
                    row = _source_tuple(fresh[tid], alias, coin, entry.address)
                    if row[0] > CUT:
                        continue
                    if tid in extra_identifiers[(alias, coin)]:
                        raise ValueError("DUPLICATE_SUPPLEMENTAL_FILL_TID")
                    extra_identifiers[(alias, coin)].add(tid)
                    all_extra[alias].append(row)
                source_results.append(
                    {
                        "alias": alias,
                        "coin": coin,
                        "new_unique_ids": len(new_ids),
                        "new_frozen_ids": sum(
                            fresh[tid]["time"] <= int(CUT.timestamp() * 1000) for tid in new_ids
                        ),
                        "matched_original_ids": len(overlap),
                        "original_tids_not_in_generation": len(set(existing) - set(fresh)),
                        "generation_pages": len(pages),
                        "generation_page_raw_sha256": [
                            json.loads((coin_dir / f"page-{n:05d}.meta.json").read_text())["sha256"]
                            for n in range(1, len(pages) + 1)
                        ],
                        "generation_exhausted": pages[-1].get("has_more") is False,
                        "original_source_unchanged": True,
                    }
                )
    comparison = []
    for entry in entries:
        alias = entry.alias
        coins = _coins(root, alias)
        original, errs_old = _source_fills(root, alias, entry.address, coins)
        merged, errs_new = _source_fills(
            root, alias, entry.address, coins, additional_events=all_extra[alias]
        )
        errors = [e for e in errs_new if not e.endswith(":POSITION_DISCONTINUITY")]
        if errors:
            raise ValueError("UNEXPECTED_SOURCE_NORMALIZATION_ERRORS")
        comparison.append(
            {
                "wallet": alias,
                "cohort": entry.cohort,
                "baseline_fills": len(original),
                "verified_hyperx_additions": len(all_extra[alias]),
                "merged_fills": len(merged),
                "baseline_chain_gaps": sum(_gap_count(original).values()),
                "merged_chain_gaps": sum(_gap_count(merged).values()),
                "baseline_gap_pairs": _gap_count(original),
                "merged_gap_pairs": _gap_count(merged),
                "remaining_gap_details": _gap_details(merged),
                "resolved_gaps": sum(_gap_count(original).values())
                - sum(_gap_count(merged).values()),
                "no_unverified_exchange_state_inferred": True,
            }
        )
    report = {
        "schema": "pm01_hyperx_verified_overlay_report_v1",
        "research_only": True,
        "input": "HYPERX_AUTHENTICATED_DATA_API_ONLY",
        "source_frozen_cutoff": CUT.isoformat(),
        "original_vendor_cache_intact": True,
        "source_manifests": root_fingerprints,
        "private_generations": source_generations,
        "raw_page_overlaps_and_additions": source_results,
        "wallets": comparison,
        "wallet_count": len(comparison),
        "new_verified_pre_cutoff_fills": sum(len(x) for x in all_extra.values()),
        "new_verified_fills_within_study": sum(
            STUDY_START <= row[0] <= CUT for es in all_extra.values() for row in es
        ),
        "chain_gaps_before": sum(x["baseline_chain_gaps"] for x in comparison),
        "chain_gaps_after": sum(x["merged_chain_gaps"] for x in comparison),
        "fully_contiguous_wallets_after": sum(x["merged_chain_gaps"] == 0 for x in comparison),
    }
    return report, dict(all_extra)


def main():
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--research-root", type=Path, required=True)
    ap.add_argument("--generations", type=Path, nargs="+", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    report, _ = examine(args.research_root, tuple(args.generations))
    file, digest = _write_private_evidence(args.out, report)
    print(
        json.dumps(
            {
                "new_pre_cutoff_fills": report["new_verified_pre_cutoff_fills"],
                "new_within_study": report["new_verified_fills_within_study"],
                "wallets": report["wallet_count"],
                "gaps_before": report["chain_gaps_before"],
                "gaps_after": report["chain_gaps_after"],
                "fully_contiguous_after": report["fully_contiguous_wallets_after"],
                "changed_wallets": [
                    {
                        k: v
                        for k, v in x.items()
                        if k
                        in (
                            "wallet",
                            "verified_hyperx_additions",
                            "baseline_chain_gaps",
                            "merged_chain_gaps",
                            "resolved_gaps",
                            "merged_gap_pairs",
                        )
                    }
                    for x in report["wallets"]
                    if x["baseline_chain_gaps"] != x["merged_chain_gaps"]
                    or x["verified_hyperx_additions"] > 0
                ],
                "private_evidence": str(file),
                "sha256": digest,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
