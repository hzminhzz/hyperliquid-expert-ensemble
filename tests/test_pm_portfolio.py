import pytest
from datetime import datetime, timezone, timedelta

from ensemble.pm_portfolio import (
    Coverage,
    WalletSource,
    KnownPosition,
    reconstruct_asof,
    validate_36_wallet_cohort,
)


def cohort():
    return (
        [WalletSource(f"original-{i}", "original", Coverage.HISTORICAL_ONLY) for i in range(6)]
        + [WalletSource(f"wave1-{i}", "wave1", Coverage.UNKNOWN) for i in range(10)]
        + [WalletSource(f"wave2-{i}", "wave2", Coverage.UNKNOWN) for i in range(20)]
    )


def test_complete_cohort_is_admitted_but_not_qualified():
    wallets = validate_36_wallet_cohort(cohort())
    assert len(wallets) == 36
    assert not any(w.replay_eligible for w in wallets)


def test_missing_wallet_fails_closed():
    with pytest.raises(ValueError, match="incomplete"):
        validate_36_wallet_cohort(cohort()[:-1])


def test_duplicate_wallet_fails_closed_even_with_case_difference():
    wallets = cohort()
    wallets[-1] = WalletSource("ORIGINAL-0", "wave2", Coverage.UNKNOWN)
    with pytest.raises(ValueError, match="duplicate"):
        validate_36_wallet_cohort(wallets)


def test_prospective_claim_requires_first_known():
    with pytest.raises(ValueError, match="first-known"):
        WalletSource("x", "original", Coverage.PROSPECTIVE_VERIFIED)


def test_verified_wallet_has_explicit_timestamp():
    w = WalletSource("x", "original", Coverage.PROSPECTIVE_VERIFIED, "2026-10-30T00:00:00Z")
    assert w.replay_eligible


def test_asof_portfolio_excludes_future_and_scales_known_positions():
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    wallet = WalletSource("w", "original", Coverage.PROSPECTIVE_VERIFIED, now.isoformat())

    def pos(seq, known, quantity, complete=True):
        return KnownPosition(
            "w",
            "hyperliquid:mainnet:perp:default:BTC",
            quantity,
            known,
            now - timedelta(minutes=1),
            Coverage.PROSPECTIVE_VERIFIED,
            seq,
            complete,
        )

    before = pos(1, now, 2.0)
    future = pos(2, now + timedelta(minutes=5), 8.0)
    result = reconstruct_asof(wallet, [future, before], now, 0.25)
    assert result.eligible
    assert result.targets == {"hyperliquid:mainnet:perp:default:BTC": 0.5}


def test_incomplete_snapshot_is_not_assumed_flat():
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    wallet = WalletSource("w", "original", Coverage.PROSPECTIVE_VERIFIED, now.isoformat())
    p = KnownPosition(
        "w", "hyperliquid:mainnet:perp:default:BTC", 1, now, now, Coverage.PROSPECTIVE_VERIFIED, 1
    )
    result = reconstruct_asof(wallet, [p], now)
    assert not result.eligible
    assert result.reason == "INCOMPLETE_PORTFOLIO_SNAPSHOT"


def test_historical_wallet_cannot_be_promoted_by_observed_fills():
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    wallet = WalletSource("w", "wave2", Coverage.HISTORICAL_ONLY)
    assert not reconstruct_asof(wallet, [], now).eligible
