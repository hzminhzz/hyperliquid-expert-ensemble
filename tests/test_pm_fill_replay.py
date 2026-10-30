"""Fill-only PM-01 source validation without Rust observation dependencies."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from hashlib import sha256
import gzip
import json
import pytest

from ensemble.pm_fill_replay import _read_pair, _source_fills, CUT


def _fixture(tmp_path: Path):
    root = tmp_path / "slow_pod_data_v1/hyperx_fills_cache_v1/pages/W001/BTC"
    root.mkdir(parents=True)
    address = "0x" + "1" * 40
    t0 = int(datetime(2026, 10, 20, tzinfo=UTC).timestamp() * 1000)
    rows = [
        {
            "user": address,
            "coin": "BTC",
            "tid": 1,
            "time": t0,
            "side": "B",
            "sz": "1",
            "px": "100",
            "start_position": "0",
        },
        {
            "user": address,
            "coin": "BTC",
            "tid": 2,
            "time": t0 + 60000,
            "side": "A",
            "sz": "1",
            "px": "110",
            "start_position": "1",
        },
        {
            "user": address,
            "coin": "BTC",
            "tid": 3,
            "time": t0 + 120000,
            "side": "B",
            "sz": "2",
            "px": "120",
            "start_position": "0",
        },
    ]
    raw = json.dumps({"page": 1, "page_size": 2000, "has_more": False, "fills": rows}).encode()
    (root / "page-00001.json.gz").write_bytes(gzip.compress(raw))
    (root / "page-00001.meta.json").write_text(json.dumps({"sha256": sha256(raw).hexdigest()}))
    return address, root, rows


def test_historical_source_filter_and_position_linking(tmp_path):
    address, root, _ = _fixture(tmp_path)
    rows = _read_pair(tmp_path, "W001", address, "BTC")
    assert len(rows) == 3
    assert rows[0][3:5] == (Decimal(0), Decimal(1))
    assert rows[-1][3:5] == (Decimal(0), Decimal(2))
    merged, issues = _source_fills(tmp_path, "W001", address, ["BTC"])
    assert not issues and len(merged) == 3


def test_bad_checksum_rejected(tmp_path):
    address, root, _ = _fixture(tmp_path)
    (root / "page-00001.meta.json").write_text(json.dumps({"sha256": "0" * 64}))
    with pytest.raises(ValueError, match="SHA mismatch"):
        _read_pair(tmp_path, "W001", address, "BTC")


def test_duplicate_trades_rejected(tmp_path):
    address, root, rows = _fixture(tmp_path)
    rows[-1]["tid"] = rows[0]["tid"]
    raw = json.dumps({"page": 1, "page_size": 2000, "has_more": False, "fills": rows}).encode()
    (root / "page-00001.json.gz").write_bytes(gzip.compress(raw))
    (root / "page-00001.meta.json").write_text(json.dumps({"sha256": sha256(raw).hexdigest()}))
    with pytest.raises(ValueError, match="duplicate"):
        _read_pair(tmp_path, "W001", address, "BTC")


def test_vendor_event_after_frozen_cutoff_excluded(tmp_path):
    address, root, rows = _fixture(tmp_path)
    rows[-1]["time"] = int(CUT.timestamp() * 1000) + 1000
    raw = json.dumps({"page": 1, "page_size": 2000, "has_more": False, "fills": rows}).encode()
    (root / "page-00001.json.gz").write_bytes(gzip.compress(raw))
    (root / "page-00001.meta.json").write_text(json.dumps({"sha256": sha256(raw).hexdigest()}))
    assert len(_read_pair(tmp_path, "W001", address, "BTC")) == 2


def test_wrong_identity_rejected(tmp_path):
    address, _, _ = _fixture(tmp_path)
    with pytest.raises(ValueError, match="wallet/coin mismatch"):
        _read_pair(tmp_path, "W001", "0x" + "2" * 40, "BTC")
