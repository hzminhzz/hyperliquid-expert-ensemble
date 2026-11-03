"""Offline Hyperliquid public Info API fill reconstruction tests."""
from __future__ import annotations

from decimal import Decimal

from ensemble.hyperliquid_chain_verify import compare, get_full_fills, summarize


def fill(tid: int, when: int, pnl: str, fee: str = "0.1", token: str = "USDC") -> dict:
    return {
        "tid": tid, "time": when, "closedPnl": pnl, "fee": fee,
        "feeToken": token, "coin": "BTC",
    }


def test_net_realized_from_fill_is_closed_pnl_less_usdc_fee() -> None:
    result = summarize([fill(1, 1000, "33.502981", "0.990446")])
    assert Decimal(result["net"]) == Decimal("32.512535")
    assert result["status"] == "USDC_FEE_RECONSTRUCTABLE"


def test_non_usdc_fee_cannot_be_assumed_dollar_denominated() -> None:
    result = summarize([fill(1, 1000, "33", "0.1", "HYPE")])
    assert result["net"] is None
    assert result["status"] == "UNCONVERTED_NON_USDC_FEES"


def test_provider_comparison_has_explicit_tolerance_and_incomplete_state() -> None:
    assert compare("32.512535", "32.512535", True)[0] == "MATCH"
    assert compare("32.512535", "31.0", True)[0] == "DIFFERS"
    assert compare("32.512535", "32.512535", False)[0] == "INCONCLUSIVE"
    assert compare(None, "32.512535", True)[0] == "INCONCLUSIVE"


def test_single_fills_page_complete_and_sort_order() -> None:
    class FakeClient:
        def fills_page(self, wallet: str, start: int, end: int) -> list[dict]:
            assert wallet == "wallet"
            assert start == 1000
            assert end == 5000
            return [fill(2, 3000, "3"), fill(1, 2000, "1")]
    rows, warnings, pages = get_full_fills(FakeClient(), "wallet", 1000, 5000)
    assert len(rows) == 2
    assert rows[0]["tid"] == 1
    assert pages == 1
    assert not warnings


def test_out_of_window_fills_do_not_enter_reconstructed_pnl() -> None:
    class FakeClient:
        def fills_page(self, wallet: str, start: int, end: int) -> list[dict]:
            return [fill(1, 999, "100000"), fill(2, 1100, "1")]
    rows, warnings, _ = get_full_fills(FakeClient(), "wallet", 1000, 5000)
    assert len(rows) == 1
    assert warnings == ["OUT_OF_RANGE_OR_MALFORMED_FILL"]
    assert Decimal(summarize(rows)["net"]) == Decimal("0.9")
