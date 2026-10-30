"""Verify spot-base fee conversion uses instrument identity and fill price."""
from __future__ import annotations

from decimal import Decimal

from ensemble.hyperliquid_spot_fee_reconcile import fee_in_usdc, pnl_from_fills, spot_pairs


def test_spot_meta_maps_pair_indices_to_base_usdc() -> None:
    meta = {
        "tokens": [
            {"name": "USDC", "index": 0},
            {"name": "HYPE", "index": 150},
            {"name": "CAT", "index": 48},
        ],
        "universe": [
            {"name": "@107", "tokens": [150, 0]},
            {"name": "@126", "tokens": [48, 0]},
        ],
    }
    pairs = spot_pairs(meta)
    assert pairs["@107"] == ("HYPE", "USDC")
    assert pairs["@126"] == ("CAT", "USDC")


def test_base_token_fee_converts_using_execution_px() -> None:
    pairs = {"@107": ("HYPE", "USDC")}
    charge, source = fee_in_usdc(
        {"feeToken": "HYPE", "fee": "0.00153299", "px": "91.313", "coin": "@107"}, pairs,
    )
    assert charge == Decimal("0.00153299") * Decimal("91.313")
    assert source == "SPOT_BASE_AT_EXECUTION_PRICE"


def test_fee_in_wrong_base_or_non_usdc_pair_not_converted() -> None:
    pairs = {"@107": ("HYPE", "USDC"), "@900": ("CAT", "HYPE")}
    assert fee_in_usdc({"feeToken": "CAT", "fee": "1", "px": "4", "coin": "@107"}, pairs)[0] is None
    assert fee_in_usdc({"feeToken": "CAT", "fee": "1", "px": "4", "coin": "@900"}, pairs)[0] is None


def test_combined_perp_and_spot_pnl_net_no_double_count() -> None:
    result = pnl_from_fills([
        {"coin": "BTC", "closedPnl": "10.00", "fee": "0.40", "feeToken": "USDC"},
        {"coin": "@107", "closedPnl": "0.00", "fee": "0.10", "feeToken": "HYPE", "px": "90.00"},
    ], {"@107": ("HYPE", "USDC")})
    assert Decimal(result["gross"]) == Decimal("10.00")
    assert Decimal(result["fee_usdc"]) == Decimal("9.4000")
    assert Decimal(result["net"]) == Decimal("0.6000")
    assert result["converted_spot_base_fees"] == 1
    assert not result["unconverted"]


def test_unknown_fee_conversion_censors_net_but_preserves_gross() -> None:
    result = pnl_from_fills([
        {"coin": "@321", "closedPnl": "2.00", "fee": "0.20", "feeToken": "UNKNOWN", "px": "3.00"}
    ], {})
    assert result["gross"] == "2.00"
    assert result["fee_usdc"] is None
    assert result["net"] is None
    assert result["unconverted"] == ["UNKNOWN:@321"]
