"""PM-01 fill-only research candidate cards cannot promote hindsight."""

from __future__ import annotations

from hashlib import sha256

import pytest

from ensemble.pm_fill_cards import build_fill_evidence_cards, REQUIRED_EXAMPLES
from ensemble.pm_sources import CohortEntry


def _cohort():
    items = []
    aliases = (
        [f"W{n:03d}" for n in range(1, 7)]
        + [f"A{n:03d}" for n in range(1, 11)]
        + ["CA-005", "W042", "VS066"]
        + [f"B{n:03d}" for n in range(1, 18)]
    )
    assert len(aliases) == 36
    for i, alias in enumerate(aliases, start=1):
        addr = "0x" + f"{i:040x}"
        group = "original" if i <= 6 else "wave1" if i <= 16 else "wave2"
        items.append(
            CohortEntry(
                alias,
                group,
                addr,
                sha256(addr.encode()).hexdigest(),
                "VENDOR_ROSTER_CACHED_HISTORICAL_ONLY",
                1,
                2,
            )
        )
    return tuple(items)


def _rows(cohort):
    rows = []
    for entry in cohort:
        for latency in (5, 15, 60):
            rows.append(
                {
                    "wallet": entry.alias,
                    "cohort": entry.cohort,
                    "latency_minutes": latency,
                    "status": "BLOCKED_MARKET_OR_EXECUTION",
                    "reason": "MISSING_MARKET_DAY",
                    "instrument": "hyperliquid:mainnet:perp:default:BTC",
                    "pnl": None,
                    "source_fills": 50,
                    "instruments": 2,
                    "known_at": "UNKNOWN_VENDOR_HISTORICAL_ORACLE",
                    "scope": "FROZEN_VENDOR_REPORTED_INSTRUMENTS_ONLY",
                    "oracle_hindsight": True,
                    "blocked_reasons": [],
                    "source_equity_accounting_reconciled": False,
                    "actual_follower_fills": None,
                }
            )
    return rows


def test_all_36_wallets_have_evidence_cards_and_no_fake_promotions():
    cohort = _cohort()
    result = build_fill_evidence_cards(
        cohort,
        _rows(cohort),
        risk_budget="0.01",
        study_start="2026-05-29T00:00:00+00:00",
        cutoff="2026-10-29T00:00:00+00:00",
    )
    assert len(result["cards"]) == 36
    assert result["qualified_prospective_wallets"] == 0
    assert set(result["example_wallets"]) == set(REQUIRED_EXAMPLES)
    for card in result["cards"]:
        assert card["prospective_follower_qualified"] is False
        assert card["historical_first_known_at"] is None
        assert card["account_equity_reconciled"] is False
        assert card["research_allocation_status"] == ("REQUIRES_PIT_AND_EXECUTION_VERIFICATION")
        assert all(r["modeled_follower_pnl"] is None for r in card["latency_scenarios"])
        assert all(r["blocked_reason"] == "MISSING_MARKET_DAY" for r in card["latency_scenarios"])


def test_positive_historical_oracle_pnl_not_qualified_even_with_better_baseline():
    cohort = _cohort()
    rows = _rows(cohort)
    for row in rows:
        if row["wallet"] == "CA-005":
            row.update(
                {
                    "status": "HISTORICAL_HINDSIGHT_SIMULATION_ONLY",
                    "pnl": "1200.0",
                    "matched_long_only_market_control_pnl": "-120.0",
                    "incremental_vs_matched_long_control": "1320.0",
                    "hypothetical_fill_count": 19,
                }
            )
    result = build_fill_evidence_cards(
        cohort,
        rows,
        risk_budget="0.01",
        study_start="x",
        cutoff="y",
    )
    card = result["example_wallets"]["CA-005"]
    assert all(x["incremental_vs_market_only"] == "1320.0" for x in card["latency_scenarios"])
    assert not card["prospective_follower_qualified"]
    assert "NO_INDEPENDENT_PROSPECTIVE_FOLLOWER_EVIDENCE" in card["qualification_blockers"]


def test_duplicate_or_missing_scenario_and_nonnull_blocked_pnl_fail_closed():
    cohort = _cohort()
    original = _rows(cohort)
    with pytest.raises(ValueError, match="MISSING_SCENARIOS"):
        build_fill_evidence_cards(
            cohort,
            original[:-1],
            risk_budget="0.01",
            study_start="x",
            cutoff="y",
        )
    invalid = [dict(x) for x in original]
    invalid[0]["pnl"] = "100"
    with pytest.raises(ValueError, match="UNVERIFIED_SCENARIO_HAS_PNL"):
        build_fill_evidence_cards(
            cohort,
            invalid,
            risk_budget="0.01",
            study_start="x",
            cutoff="y",
        )
    duplicated = [dict(x) for x in original]
    duplicated[0]["latency_minutes"] = 15
    with pytest.raises(ValueError, match="MISSING_LATENCY"):
        build_fill_evidence_cards(
            cohort,
            duplicated,
            risk_budget="0.01",
            study_start="x",
            cutoff="y",
        )
