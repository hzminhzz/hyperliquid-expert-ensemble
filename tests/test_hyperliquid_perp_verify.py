"""Regression tests for the official perpetuals-only realized PnL proof."""
from __future__ import annotations

from decimal import Decimal

from ensemble.hyperliquid_perp_verify import perp_pnl, spot_fill, verify_official


def test_spot_and_perp_instrument_identity() -> None:
    assert spot_fill({"coin": "@126"})
    assert spot_fill({"coin": "PURR/USDC"})
    assert not spot_fill({"coin": "BTC"})
    assert not spot_fill({"coin": "xyz:KNTQ"})


def test_mixed_spot_fees_excluded_from_perp_realized_pnl() -> None:
    fills = [
        {"coin": "BTC", "closedPnl": "1884.712164", "fee": "49.028930", "feeToken": "USDC"},
        {"coin": "@126", "closedPnl": "91.10699984", "fee": "0.0275639", "feeToken": "CAT"},
    ]
    result = perp_pnl(fills)
    assert result["perp_fills"] == 1
    assert result["spot_fills_excluded"] == 1
    assert Decimal(result["net"]) == Decimal("1835.683234")


def test_non_usdc_perp_fee_abstains() -> None:
    result = perp_pnl([{"coin": "BTC", "closedPnl": "50", "feeToken": "HYPE", "fee": "1"}])
    assert result["net"] is None
    assert result["status"] == "NON_USDC_PERP_FEE_NOT_CONVERTIBLE"


def test_official_match_requires_cent_level_agreement() -> None:
    assert verify_official("1835.683234", "1835.6832339999996")[0] == "MATCH"
    assert verify_official("1835.683234", "1835.66")[0] == "DIFFERS"
    assert verify_official(None, "1835")[0] == "NOT_VERIFIABLE"
