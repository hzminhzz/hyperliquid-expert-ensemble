"""PM-01 HyperX-only authenticated source recovery & immutable overlay tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import gzip
import json
from pathlib import Path

import pytest

from ensemble.pm_fill_replay import _source_fills, _same_position
from ensemble.pm_hyperx_gap_recovery import _post_audit
from ensemble.pm_hyperx_overlay_audit import _read_pages, _legacy, _source_tuple
from ensemble.pm_sources import CohortEntry

BASE = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
ADDRESS = "0x" + "1" * 40
ALIAS = "W001"
COIN = "BTC"


def _row(tid, timestamp, before, size, side):
    return {
        "user": ADDRESS,
        "coin": COIN,
        "tid": tid,
        "time": int(timestamp.timestamp() * 1000),
        "start_position": str(before),
        "sz": str(size),
        "side": side,
        "px": "100",
        "closedPnl": "0",
    }


def _save_page(folder: Path, rows, *, schema="original"):
    folder.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        {
            "page": 1,
            "page_size": 2000,
            "has_more": False,
            "fills": sorted(rows, key=lambda r: r["time"], reverse=True),
        },
        sort_keys=True,
    ).encode()
    (folder / "page-00001.json.gz").write_bytes(gzip.compress(raw, mtime=0))
    meta = {"alias": ALIAS, "coin": COIN, "page": 1, "sha256": sha256(raw).hexdigest()}
    if schema == "generation":
        meta["schema"] = "pm01_hyperx_gap_generation_page_v1"
    (folder / "page-00001.meta.json").write_text(json.dumps(meta))
    return folder


def _case(root: Path):
    old = [
        _row(1, BASE, 0, 1, "B"),
        _row(3, BASE + timedelta(minutes=2), 2, 2, "A"),
    ]
    new = _row(2, BASE + timedelta(minutes=1), 1, 1, "B")
    cache = root / "slow_pod_data_v1/hyperx_fills_cache_v1/pages/W001/BTC"
    _save_page(cache, old)
    entry = CohortEntry(
        ALIAS,
        "original",
        ADDRESS,
        sha256(ADDRESS.encode()).hexdigest(),
        "VENDOR_ROSTER_CACHED_HISTORICAL_ONLY",
        1,
        2,
    )
    return entry, old, new


def test_hyperx_missing_fill_repairs_genuine_position_gap(tmp_path):
    entry, old, new = _case(tmp_path)
    baseline, errors = _source_fills(tmp_path, ALIAS, ADDRESS, [COIN])
    assert any("POSITION_DISCONTINUITY" in e for e in errors)
    extra = _source_tuple(new, ALIAS, COIN, ADDRESS)
    assert extra[3:5] == (Decimal(1), Decimal(2))
    merged, fixed_errors = _source_fills(
        tmp_path, ALIAS, ADDRESS, [COIN], additional_events=[extra]
    )
    assert fixed_errors == []
    assert len(merged) == 3
    assert all(_same_position(a[4], b[3]) for a, b in zip(merged, merged[1:]))


def test_authenticated_generation_must_match_exact_original_rows(tmp_path):
    entry, old, new = _case(tmp_path)
    generation = _save_page(
        tmp_path / "private_generation/W001/BTC", [*old, new], schema="generation"
    )
    pages = _read_pages(generation, ALIAS, COIN)
    assert len(pages) == 1
    audit = _post_audit(tmp_path, entry, COIN, pages)
    assert audit["overlap_count"] == 2
    assert audit["new_tids"] == 1
    assert audit["new_fills_before_cutoff"] == 1
    assert audit["overlay_eligible"] is True
    changed = dict(old[0], px="101")
    conflicting = json.loads(json.dumps(pages[0]))
    conflicting["fills"] = [changed, old[1], new]
    audit2 = _post_audit(tmp_path, entry, COIN, [conflicting])
    assert audit2["conflicting_identical_tid_payloads"] == 1
    assert audit2["overlay_eligible"] is False


def test_modified_source_sidecar_or_gzip_blocks_reuse(tmp_path):
    _, old, new = _case(tmp_path)
    generation = _save_page(
        tmp_path / "private_generation/W001/BTC", [*old, new], schema="generation"
    )
    gz = generation / "page-00001.json.gz"
    gz.write_bytes(gzip.compress(b'{"altered":true}', mtime=0))
    with pytest.raises(ValueError, match="SOURCE_GENERATION_HASH"):
        _read_pages(generation, ALIAS, COIN)


def test_unknown_coin_address_wrong_and_duplicate_supplement_rejected(tmp_path):
    _case(tmp_path)
    wrong = _row(4, BASE, 0, 1, "B")
    wrong["user"] = "0x" + "2" * 40
    with pytest.raises(ValueError, match="WALLET_OR_INSTRUMENT"):
        _source_tuple(wrong, ALIAS, COIN, ADDRESS)
    extra = _source_tuple(_row(2, BASE + timedelta(minutes=1), 1, 1, "B"), ALIAS, COIN, ADDRESS)
    with pytest.raises(ValueError, match="DUPLICATE_SUPPLEMENTAL"):
        _source_fills(tmp_path, ALIAS, ADDRESS, [COIN], additional_events=[extra, extra])
    with pytest.raises(ValueError, match="OUTSIDE_SOURCE_ROSTER"):
        _source_fills(
            tmp_path,
            ALIAS,
            ADDRESS,
            [COIN],
            additional_events=[(extra[0], extra[1], "ETH", *extra[3:])],
        )


def test_legacy_immutable_cache_remainder_read_only(tmp_path):
    _case(tmp_path)
    before = _legacy(tmp_path, ALIAS, COIN)
    assert set(before) == {1, 3}
    assert before[1]["start_position"] == "0"
