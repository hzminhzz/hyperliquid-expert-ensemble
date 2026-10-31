"""Adversarial immutable source byte integrity: synthetic private fixtures only."""

from __future__ import annotations

import gzip
from hashlib import sha256
import json
from pathlib import Path

import pytest

from ensemble.pm_source_audit import audit_market_defects, audit_pages
from ensemble.pm_sources import CohortEntry


CUTOFF_MS = 1793294536142


def _make_source(root: Path):
    cache = root / "slow_pod_data_v1/hyperx_fills_cache_v1"
    cache.mkdir(parents=True, exist_ok=True)
    entries = []
    journal = []
    for n in range(1, 37):
        alias = f"W{n:03d}"
        address = "0x" + f"{n:040x}"
        cohort = "original" if n <= 6 else "wave1" if n <= 16 else "wave2"
        entries.append(
            CohortEntry(
                alias,
                cohort,
                address,
                sha256(address.encode()).hexdigest(),
                "VENDOR_ROSTER_CACHED_HISTORICAL_ONLY",
                1,
                1,
            )
        )
        coin = "#12270" if n == 1 else "BTC"
        raw = json.dumps(
            {
                "page": 1,
                "page_size": 2000,
                "fills": [
                    {
                        "user": address,
                        "coin": coin,
                        "tid": n,
                        "time": CUTOFF_MS + n % 3 - 1,
                    }
                ],
            },
            sort_keys=True,
        ).encode()
        data = sha256(raw).hexdigest()
        info = {"alias": alias, "coin": coin, "page": 1, "page_size": 2000, "sha256": data}
        journal.append(json.dumps(info, sort_keys=True))
        folder = cache / "pages" / alias / ("%2312270" if n == 1 else "BTC")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "page-00001.json.gz").write_bytes(gzip.compress(raw, mtime=0))
        (folder / "page-00001.meta.json").write_text(json.dumps(info))
    (cache / "append_only_page_journal.jsonl").write_text("\n".join(journal) + "\n")
    return tuple(entries), cache


def test_36_source_pages_identity_sha_cutoff_and_opaque_coin(tmp_path):
    entries, _ = _make_source(tmp_path)
    result = audit_pages(tmp_path, entries)
    assert result["wallet_count"] == 36
    assert result["pages_verified"] == 36
    assert result["raw_fill_rows_verified"] == 36
    assert result["post_frozen_cutoff_rows_excluded"] == 12
    assert result["validation"].startswith("PASS_IMMUTABLE")
    assert all(x["historical_known_at"] == "UNKNOWN_VENDOR_BACKFILL" for x in result["cohort"])


def test_corrupt_raw_page_sha_blocks_entire_cohort(tmp_path):
    entries, cache = _make_source(tmp_path)
    original = cache / "pages/W002/BTC/page-00001.json.gz"
    obj = json.loads(gzip.decompress(original.read_bytes()))
    obj["fills"][0]["tid"] = 999
    original.write_bytes(gzip.compress(json.dumps(obj).encode(), mtime=0))
    with pytest.raises(ValueError, match="SHA mismatch"):
        audit_pages(tmp_path, entries)


def test_missing_sidecar_never_silently_accepts_vendor_rows(tmp_path):
    entries, cache = _make_source(tmp_path)
    (cache / "pages/W003/BTC/page-00001.meta.json").unlink()
    with pytest.raises(ValueError, match="missing source page half"):
        audit_pages(tmp_path, entries)


def test_changed_source_trade_id_across_pair_pages_is_rejected(tmp_path):
    entries, cache = _make_source(tmp_path)
    # Add a second valid cached source page, but reuses its first page TID.
    path = cache / "pages/W001/%2312270"
    info1 = json.loads((path / "page-00001.meta.json").read_text())
    original = json.loads(gzip.decompress((path / "page-00001.json.gz").read_bytes()))
    original["page"] = 2
    raw = json.dumps(original).encode()
    info2 = dict(info1, page=2, sha256=sha256(raw).hexdigest())
    (path / "page-00002.json.gz").write_bytes(gzip.compress(raw))
    (path / "page-00002.meta.json").write_text(json.dumps(info2))
    with (cache / "append_only_page_journal.jsonl").open("a") as f:
        f.write(json.dumps(info2) + "\n")
    with pytest.raises(ValueError, match="cross-page duplicate TID"):
        audit_pages(tmp_path, entries)


def test_live_hip3_source_metadata_tamper_reported_not_waived(tmp_path):
    folder = (
        tmp_path
        / "crypto_futures_ohlcv_1m_HYPERLIQUID_XYZ_PERP"
        / "symbol=SP500/year=2026/month=10/day=10"
    )
    folder.mkdir(parents=True)
    file = folder / "data.parquet"
    file.write_bytes(b"original source binary")
    (folder / "source.json").write_text(
        json.dumps({"sha256": sha256(b"original source binary").hexdigest()})
    )
    result = audit_market_defects(tmp_path)
    assert result["instruments"]["SP500"]["market_qualified"]
    assert not result["instruments"]["XYZ100"]["market_qualified"]
    file.write_bytes(b"untracked source edit")
    changed = audit_market_defects(tmp_path)
    assert changed["instruments"]["SP500"]["live_lake_SHA_mismatch_count"] == 1
    assert not changed["instruments"]["SP500"]["market_qualified"]
