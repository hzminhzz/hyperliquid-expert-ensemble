"""Propr display scoped 90%-coverage; never pretend HIP-3 means tradable."""

from __future__ import annotations

from hashlib import sha256

import pytest

from ensemble.pm_propr_scope import (
    SCOPE_STRICT,
    SCOPE_TENTATIVE,
    SCOPE_OTHER_DEX,
    SCOPE_OPAQUE,
    SCOPE_OUTSIDE,
    build_scope,
    classify_coin,
    _coverage,
    _coin_from_market,
    add_fill_scope_summary,
    _wallet_propr_day_coverage,
)
from ensemble.pm_sources import CohortEntry


def _case():
    public = {
        "schema": "propr_public_universe_browser_snapshot_v1",
        "retrieved_on_utc_date": "2026-10-30",
        "snapshot_market_count": 4,
        "not_proof_of_historical_availability": True,
        "category_counts": {
            "crypto": 2,
            "equities": 1,
            "indices": 1,
            "commodities": 0,
            "fx": 0,
        },
        "market_symbols": {
            "crypto": ["BTC", "ETH"],
            "equities": ["NVDA"],
            "indices": ["SP500"],
            "commodities": [],
        },
    }
    aliases = tuple("W" + str(i).zfill(3) for i in range(36))
    compat = {
        "schema": "propr_wallet_roster_candidate_match_v1",
        "snapshot_date": "2026-10-30",
        "not_pit": True,
        "not_verified_live_order_api_acceptance": True,
        "original_six": [],
        "wave_a": [],
        "wave_b": [],
    }
    for i, alias in enumerate(aliases):
        item = {
            "alias": alias,
            "vendor_completed_trade_roster_total": 100,
            "exact_native_crypto_completed_trade_count": 70,
            "tentative_hip3_completed_trade_count": 20,
            "exact_crypto_coverage_fraction": 0.7,
            "mapping_max_coverage_fraction": 0.9,
            "unmapped_top5": [["io:ANTH", 10, "UNVERIFIED_DEX_SCOPE_OR_SYMBOL"]],
        }
        compat["original_six" if i < 6 else "wave_a" if i < 16 else "wave_b"].append(item)
    inventory = []
    for i in range(4):
        inventory.append(
            {
                "instrument": "hyperliquid:mainnet:perp:default:BTC",
                "date_utc": f"2026-10-{i + 1:02}",
                "status": "SOURCE_DAY_SHA_VERIFIED" if i < 3 else "MISSING_NATIVE_MARKET_DAY",
            }
        )
    for i in range(4):
        inventory.append(
            {
                "instrument": "hyperliquid:mainnet:perp:xyz:NVDA",
                "date_utc": f"2026-10-{i + 1:02}",
                "status": "SOURCE_DAY_SHA_VERIFIED" if i < 2 else "MISSING_NATIVE_MARKET_DAY",
            }
        )
    for i in range(2):
        inventory.append(
            {
                "instrument": "hyperliquid:mainnet:perp:io:ANTH",
                "date_utc": f"2026-10-{i + 1:02}",
                "status": "SOURCE_DAY_SHA_VERIFIED" if i == 0 else "MISSING_NATIVE_MARKET_DAY",
            }
        )
    market = {
        "schema": "pm01_native_1m_36_wallet_coverage_v1",
        "wallet_count": 36,
        "instrument_day_inventory": inventory,
        "unique_instrument_days_audited": 10,
        "sha_verified_instrument_days": 6,
        "missing_or_invalid_instrument_days": 4,
        "gaps_by_market": [
            {
                "instrument": "hyperliquid:mainnet:perp:default:BTC",
                "wallet_count": 5,
                "source_book_samples": 55,
            },
            {
                "instrument": "hyperliquid:mainnet:perp:xyz:NVDA",
                "wallet_count": 3,
                "source_book_samples": 33,
            },
            {
                "instrument": "hyperliquid:mainnet:perp:io:ANTH",
                "wallet_count": 19,
                "source_book_samples": 200,
            },
        ],
    }
    return public, compat, market, aliases


def test_venue_scope_requires_native_crypto_or_tentative_xyz_only():
    public, _, _, _ = _case()
    assert classify_coin("BTC", public) == SCOPE_STRICT
    assert classify_coin("xyz:NVDA", public) == SCOPE_TENTATIVE
    assert classify_coin("xyz:SP500", public) == SCOPE_TENTATIVE
    assert classify_coin("io:NVDA", public) == SCOPE_OTHER_DEX
    assert classify_coin("para:NVDA", public) == SCOPE_OTHER_DEX
    assert classify_coin("xyz:BTC", public) == SCOPE_OUTSIDE
    assert classify_coin("SOL", public) == SCOPE_OUTSIDE
    assert classify_coin("#123", public) == SCOPE_OPAQUE
    assert _coin_from_market("hyperliquid:mainnet:perp:xyz:NVDA") == "xyz:NVDA"
    with pytest.raises(ValueError, match="UNVERIFIED_HYPERLIQUID_MARKET"):
        _coin_from_market("binance:spot:NVDA")


def test_actual_market_denominator_not_just_missing_queue():
    public, compat, market, aliases = _case()
    result = build_scope(public, compat, market, aliases)
    assert result["source_cohort_wallet_count"] == 36
    assert result["trade_roster_coverage"]["strict_native_display_fraction"] == 0.7
    assert result["trade_roster_coverage"]["strict_plus_tentative_upper_bound_fraction"] == 0.9
    counts = result["market_day_coverage"]
    assert counts["original_all_market_days"]["required_instrument_utc_days"] == 10
    assert counts["strict_native_crypto_display"]["sha_verified_days"] == 3
    assert counts["strict_native_crypto_display"]["required_instrument_utc_days"] == 4
    assert counts["strict_native_crypto_display"]["additional_exact_days_required_for_90pct"] == 1
    assert counts["strict_plus_tentative_xyz_display"]["sha_verified_days"] == 5
    assert counts["strict_plus_tentative_xyz_display"]["required_instrument_utc_days"] == 8
    assert (
        counts["strict_plus_tentative_xyz_display"]["additional_exact_days_required_for_90pct"] == 3
    )
    assert counts["excluded_or_unverified_market_scope"]["required_instrument_utc_days"] == 2
    assert counts["excluded_or_unverified_market_scope"]["sha_verified_days"] == 1
    assert result["out_of_scope_gap_days_by_reason"][SCOPE_OTHER_DEX] == 1
    queue = result["propr_only_priority_market_acquisition_queue"]
    assert len(queue) == 3
    assert queue[0]["provider_symbol"] == "BTC"
    assert [x["provider_symbol"] for x in queue[1:]] == ["xyz:NVDA", "xyz:NVDA"]
    assert all(not x["tradability_confirmed"] for x in queue)
    assert result["pm01_scientific_qualified"] is False
    assert result["excluded_source_hedges_are_not_flat"] is True
    assert len(result["all_36_wallet_vendor_roster_market_matches"]) == 36


def test_90pct_gate_never_uses_zero_denominator_or_partial_days():
    assert _coverage([])["target_reached"] is False
    assert _coverage([])["coverage_fraction"] is None
    data = [{"status": "SOURCE_DAY_SHA_VERIFIED"} for _ in range(9)] + [
        {"status": "CHD_CONFIRMED_NO_TRADE_SOURCE"}
    ]
    a = _coverage(data)
    assert a["target_reached"] is True
    assert a["unavailable_days"] == 1
    assert a["not_actual_executable_minute_coverage"] is True
    data.append({"status": "MISSING_NATIVE_MARKET_DAY"})
    assert _coverage(data)["target_reached"] is False
    assert _coverage(data)["additional_exact_days_required_for_90pct"] == 1


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda p, c, m, a: m.pop("instrument_day_inventory"),
            "MISSING_COMPLETE_MARKET_DAY_DENOMINATOR",
        ),
        (
            lambda p, c, m, a: m["instrument_day_inventory"].append(
                m["instrument_day_inventory"][0]
            ),
            "MISSING_COMPLETE_MARKET_DAY_DENOMINATOR",
        ),
        (
            lambda p, c, m, a: m.update(sha_verified_instrument_days=7),
            "MARKET_DAY_SOURCE_SHA_COUNTS_DRIFT",
        ),
        (
            lambda p, c, m, a: c["original_six"][0].update(alias="FORGED"),
            "PROPR_COMPATIBILITY_WALLET_IDENTITY_MISMATCH",
        ),
        (
            lambda p, c, m, a: p.update(not_proof_of_historical_availability=False),
            "PROPR_HISTORY_FALSIFIED",
        ),
        (
            lambda p, c, m, a: c.update(not_verified_live_order_api_acceptance=False),
            "UNSUPPORTED_HISTORICAL_OR_ORDER",
        ),
        (lambda p, c, m, a: m.update(wallet_count=35), "MISSING_36_WALLET_COHORT"),
        (
            lambda p, c, m, a: p.update(retrieved_on_utc_date="2026-10-31"),
            "PROPR_SNAPSHOT_DATE_DRIFT",
        ),
    ],
)
def test_wrong_source_or_schema_fails_closed(mutate, error):
    p, c, m, a = _case()
    mutate(p, c, m, a)
    with pytest.raises(ValueError, match=error):
        build_scope(p, c, m, a)


def test_per_wallet_90pct_uses_only_required_propr_native_days():
    from datetime import UTC, datetime
    from decimal import Decimal

    public, _, _, _ = _case()
    stamp = datetime(2026, 10, 20, tzinfo=UTC)
    fills = [
        (stamp, 1, "BTC", Decimal(0), Decimal(1), Decimal(1), Decimal(100)),
        (stamp, 2, "io:NVDA", Decimal(0), Decimal(2), Decimal(2), Decimal(100)),
    ]
    statuses = {
        ("hyperliquid:mainnet:perp:default:BTC", "2026-10-20"): "SOURCE_DAY_SHA_VERIFIED",
        ("hyperliquid:mainnet:perp:default:BTC", "2026-10-29"): "MISSING_NATIVE_MARKET_DAY",
    }
    c = _wallet_propr_day_coverage(fills, ["BTC", "io:NVDA"], public, statuses)
    assert c["strict"]["required_instrument_utc_days"] == 2
    assert c["strict"]["sha_verified_days"] == 1
    assert c["strict"]["additional_exact_days_required_for_90pct"] == 1
    assert c["candidate"]["required_instrument_utc_days"] == 2
    assert not c["candidate"]["target_reached"]
    assert c["observed_unmapped_hedges_may_change_net_book"]


def test_per_wallet_missing_global_day_refuses_false_green():
    from datetime import UTC, datetime
    from decimal import Decimal

    public, _, _, _ = _case()
    source_time = datetime(2026, 10, 20, tzinfo=UTC)
    rows = [(source_time, 1, "BTC", Decimal(0), Decimal(1), Decimal(1), Decimal(100))]
    with pytest.raises(ValueError, match="PER_WALLET_SOURCE_CLOCKS_AND_GLOBAL_AUDIT_DISAGREE"):
        _wallet_propr_day_coverage(rows, ["BTC"], public, {})


def test_excluded_nonzero_hedge_never_treated_as_flat(monkeypatch, tmp_path):
    from datetime import datetime, UTC
    from decimal import Decimal
    import ensemble.pm_propr_scope as scope

    p, c, m, aliases = _case()
    fake = []
    for i, alias in enumerate(aliases):
        addr = "0x" + f"{i + 1:040x}"
        fake.append(
            CohortEntry(
                alias,
                "original" if i < 6 else "wave1" if i < 16 else "wave2",
                addr,
                sha256(addr.encode()).hexdigest(),
                "VENDOR",
                1,
                2,
            )
        )
    monkeypatch.setattr(scope, "load_cohort", lambda root: (fake, {}))
    monkeypatch.setattr(scope, "_coins", lambda root, alias: ["BTC", "io:NVDA"])
    rows = [
        (
            datetime(2026, 10, 15, tzinfo=UTC),
            1,
            "BTC",
            Decimal(0),
            Decimal(1),
            Decimal(1),
            Decimal(100),
        ),
        (
            datetime(2026, 10, 15, tzinfo=UTC),
            2,
            "io:NVDA",
            Decimal(0),
            Decimal(1),
            Decimal(1),
            Decimal(200),
        ),
    ]
    monkeypatch.setattr(scope, "_source_fills", lambda root, alias, address, coins: (rows, []))
    result = {}
    add_fill_scope_summary(result, tmp_path, aliases, p)
    assert result["actual_5month_fill_scope"]["wallets_with_any_nonzero_excluded_leg"] == 36
    assert result["actual_5month_fill_scope"]["candidate_upper_fill_fraction"] == 0.5
    assert result["actual_5month_fill_scope"]["candidate_upper_notional_fraction"] == 0.333333
    assert all(
        x["excluded_active_hedge_risk_cannot_be_assumed_zero"]
        for x in result["source_wallet_study_scope"]
    )
