"""Offline regression tests for HyperX historical wallet verification."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from ensemble.hyperx_verify import ApiClient, read_candidates, verify_three_views

W = "0x" + "a" * 40


def sample() -> tuple[dict, dict, dict, dict]:
    prior = {
        "wallet_id": W, "previous_predictive_rank": 1,
        "episodes_1h": "16", "utc_days": "8",
        "unverified_coverage_episodes": "15",
        "primary_equal_day_incremental_bps": "211.9",
        "mean_wallet_net_10bps": "2.1",
        "ex_post_identical_signal_group": None,
    }
    batch = {
        "user_address": W, "period": "90d", "total_realized_pnl": 100.0,
        "total_fee": 5.0, "total_complete_trade_count": 20, "win_rate": 0.7,
        "roi": 0.2, "mtm_coverage": "full", "is_contain_fee": True,
        "performance_by_coin": [
            {"coin": "BTC", "total_realized_pnl": 80.0},
            {"coin": "ETH", "total_realized_pnl": 20.0},
        ],
    }
    get = {
        "address": W, "period": "90d",
        "metrics": {"total_realized_pnl": 100.0, "total_fee": 5.0, "total_complete_trade_count": 20,
                    "is_contain_fee": True},
    }
    a = {
        "address": W,
        "wallet_metrics": {"90d": {"period": "90d", "total_realized_pnl": 100.0,
                                  "total_fee": 5.0, "total_complete_trades_count": 20,
                                  "roi": 0.2, "roi_coverage": "full",
                                  "mtm_coverage": "full", "funding_net": 2.0,
                                  "is_contain_fee": True}},
    }
    return prior, batch, get, a


def test_reconcile_three_consistent_provider_views() -> None:
    row = verify_three_views(*sample())
    assert row["endpoint_consistency_status"] == "CONSISTENT_REALIZED_PNL_AND_COUNTS"
    assert row["hyperx_realized_pnl_90d_usd"] == 100.0
    assert row["trades_90d"] == 20
    assert row["reported_fee_included"] is True
    assert row["top_coin_abs_pnl_concentration"] == 0.8
    assert row["roi_verification"] == "UNVERIFIED_VENDOR_DENOMINATOR"
    assert row["account_trading_return_verification"] == "NOT_VERIFIED_MTM_AND_CASH_FLOWS"


def test_roi_conflict_cannot_silently_be_qualified() -> None:
    p, b, g, a = sample()
    a["wallet_metrics"]["90d"]["roi"] = 3.4
    result = verify_three_views(p, b, g, a)
    assert "ROI_ENDPOINT_CONFLICT" in result["discrepancies"]
    assert result["roi_batch_RAW_UNITS_UNVERIFIED"] == 0.2
    assert result["roi_deep_RAW_UNITS_UNVERIFIED"] == 3.4


def test_fee_and_mtm_require_complete_evidence() -> None:
    p, b, g, a = sample()
    a["wallet_metrics"]["90d"]["mtm_coverage"] = "none"
    g["metrics"]["is_contain_fee"] = False
    b["deposits_and_withdrawals_count"] = 3
    result = verify_three_views(p, b, g, a)
    assert "FEE_INCLUSION_UNVERIFIED" in result["discrepancies"]
    assert "MISSING_FULL_MTM_COVERAGE" in result["discrepancies"]
    assert "TRANSFER_ADJUSTED_ROI_UNQUALIFIED" in result["discrepancies"]


def test_cross_endpoint_pnl_conflict_is_not_disguised() -> None:
    p, b, g, a = sample()
    a["wallet_metrics"]["90d"]["total_realized_pnl"] = 99.0
    a["wallet_metrics"]["90d"]["total_complete_trades_count"] = 21
    result = verify_three_views(p, b, g, a)
    assert result["endpoint_consistency_status"] == "CONFLICT_OR_MISSING"
    assert "REALIZED_PNL_ENDPOINT_CONFLICT" in result["discrepancies"]
    assert "TRADE_COUNT_ENDPOINT_CONFLICT" in result["discrepancies"]


def test_period_mismatch_and_missing_pnl_are_explicit() -> None:
    p, b, g, a = sample()
    g["period"] = "all"
    b.pop("total_realized_pnl")
    result = verify_three_views(p, b, g, a)
    assert "PERIOD_MISMATCH" in result["discrepancies"]
    assert "MISSING_PNL_ENDPOINT" in result["discrepancies"]


def test_private_roster_order_is_preserved(tmp_path: Path) -> None:
    f = tmp_path / "top20.csv"
    with f.open("w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(("wallet_id", "episodes_1h"))
        writer.writerow((W, 16))
        writer.writerow(("0x" + "b" * 40, 11))
    rows = read_candidates(f, n=2)
    assert [x["previous_predictive_rank"] for x in rows] == [1, 2]
    assert rows[0]["wallet_id"] == W


def test_bad_or_duplicate_roster_rejected(tmp_path: Path) -> None:
    f = tmp_path / "top20.csv"
    with f.open("w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(("wallet_id",))
        writer.writerow((W,))
        writer.writerow((W,))
    with pytest.raises(ValueError, match="duplicate"):
        read_candidates(f, n=2)
    with pytest.raises(ValueError, match="candidate count exceeded"):
        read_candidates(f, n=11)


def test_untrusted_base_url_rejected() -> None:
    with pytest.raises(ValueError, match="untrusted"):
        ApiClient(base="https://example.com")
