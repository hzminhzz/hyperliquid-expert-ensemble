"""WP3 economic attribution tests: known-at controls, costs, denominator, estimator."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ensemble.copy_economics import (
    analyze_historical,
    baseline_net,
    control_directions,
    equal_day_estimate,
)
from ensemble.copy_research import RecordedMid

T0 = datetime(2026, 10, 20, 12, tzinfo=UTC)
BTC = "hyperliquid:mainnet:perp:default:BTC"


def quote(minutes_before: int, price: str) -> RecordedMid:
    return RecordedMid(BTC, T0 - timedelta(minutes=minutes_before), Decimal(price))


def test_market_only_controls_use_predecision_prices_never_future_or_stale() -> None:
    known = [
        quote(60, "100"), quote(15, "101"), quote(1, "102"),
        RecordedMid(BTC, T0 - timedelta(seconds=15), Decimal(103)),
        RecordedMid(BTC, T0 + timedelta(seconds=1), Decimal(900)),
    ]
    d = control_directions(known_at=T0, quotes=known)
    assert d == {
        "cash": 0, "long_only": 1,
        "lagged_15m_trend": 1, "lagged_1h_trend": 1,
        "lagged_1h_reversal": -1,
    }
    d2 = control_directions(
        known_at=T0, quotes=[quote(60, "100"), quote(15, "101")],
    )
    assert d2["cash"] == 0 and d2["long_only"] == 1
    assert d2["lagged_1h_trend"] is None
    assert d2["lagged_15m_trend"] is None


def test_controls_costs_pay_exactly_once_only_when_position_taken() -> None:
    for side in (1, -1):
        result = baseline_net(
            direction=side, entry_mid=Decimal(100),
            exit_mid=Decimal(100), cost_bps=20,
        )
        assert result == Decimal("-0.002")
    assert baseline_net(
        direction=0, entry_mid=Decimal(100),
        exit_mid=Decimal(110), cost_bps=30,
    ) == Decimal(0)
    with pytest.raises(ValueError, match="negative"):
        baseline_net(direction=1, entry_mid=Decimal(100),
                     exit_mid=Decimal(110), cost_bps=-1)


def test_equal_day_mean_and_ci_target_exact_same_estimand() -> None:
    records = [(T0, Decimal("0.001"))] * 9 + [
        (T0 + timedelta(days=1), Decimal("-0.001"))
    ]
    result = equal_day_estimate(records, seed=13, draws=1000)
    assert result["days"] == 2
    assert result["equal_day_mean_bps"] == 0
    assert result["ci95_bps"] is None
    more = records + [(T0 + timedelta(days=i), Decimal(0))
                      for i in range(2, 8)]
    report = equal_day_estimate(more, seed=13, draws=1000)
    assert report == equal_day_estimate(more, seed=13, draws=1000)
    assert report["days"] == 8
    assert report["bootstrap_unit"] == "UTC_DAY_NOT_INDEPENDENT_WALLETS"


def _fixture(tmp_path):
    journal = tmp_path / "op.db"
    outcome = tmp_path / "labels.db"
    prices = tmp_path / "mids.db"
    with closing(sqlite3.connect(journal)) as db, db:
        db.execute(
            "CREATE TABLE opportunity_decisions("
            "opportunity_id TEXT, wallet_id TEXT, instrument_id TEXT,"
            "family TEXT,first_known_at TEXT,earliest_actionable_at TEXT,"
            "first_ledger_seq INTEGER,payload_json TEXT)"
        )
        for seq, offset, coverage in (
            (1, 0, "Seeded"), (2, 3, "Seeded"), (3, 60, "UNSPECIFIED")
        ):
            known = T0 + timedelta(minutes=offset)
            db.execute(
                "INSERT INTO opportunity_decisions VALUES (?,?,?,?,?,?,?,?)",
                (f"op{seq}",f"wallet{seq}",BTC,"ENTER",known.isoformat(),
                 (known + timedelta(seconds=2)).isoformat(), seq,
                 json.dumps({"first_event_at":known.isoformat(),
                             "source_coverage":coverage})),
            )
    with closing(sqlite3.connect(outcome)) as db, db:
        db.execute(
            "CREATE TABLE copy_markouts(opportunity_id TEXT,horizon TEXT,"
            "status TEXT,value_json TEXT,outcome_known_at TEXT)"
        )
        for seq, offset in ((1, 0), (2, 3), (3, 60)):
            known = T0 + timedelta(minutes=offset)
            ready = known + timedelta(seconds=2)
            entry = ready + timedelta(seconds=43)
            exit_at = ready + timedelta(hours=1, seconds=20)
            gross = Decimal(103) / Decimal(102) - 1
            payload = {
                "direction": 1, "entry_at": entry.isoformat(),
                "exit_at": exit_at.isoformat(), "entry_mid": "102",
                "exit_mid": "103",
                "signed_gross_return": str(gross),
                "hypothetical_net_return": str(gross - Decimal("0.001")),
                "cost_bps": "10",
                "price_model": "MID_TO_MID_HYPOTHETICAL_NOT_EXECUTABLE",
            }
            db.execute(
                "INSERT INTO copy_markouts VALUES (?,?,?,?,?)",
                (f"op{seq}", "1h", "VALID_HYPOTHETICAL",
                 json.dumps(payload), (exit_at+timedelta(seconds=2)).isoformat()),
            )
        db.execute(
            "INSERT INTO copy_markouts VALUES (?,?,?,?,?)",
            ("op1","5m","NO_COPY","{}",T0.isoformat()),
        )
    with closing(sqlite3.connect(prices)) as db, db:
        db.execute(
            "CREATE TABLE reference_mids(instrument_id TEXT,minute_utc TEXT,"
            "known_at TEXT,mid TEXT,PRIMARY KEY(instrument_id,minute_utc))"
        )
        for offset, px in ((-61,"100"), (-60,"100"), (-15,"101"), (-1,"102"),
                           (0,"102"), (59,"103")):
            when = T0 + timedelta(minutes=offset,seconds=10)
            db.execute(
                "INSERT INTO reference_mids VALUES (?,?,?,?)",
                (BTC,when.replace(second=0).isoformat(),when.isoformat(),px),
            )
    return journal, outcome, prices


def test_read_only_attribution_dedups_coin_shocks_and_blocks_unverified(tmp_path) -> None:
    source, outcomes, prices = _fixture(tmp_path)
    report = analyze_historical(
        opportunity_db=source,outcome_db=outcomes,price_db=prices,
        cutoff=T0+timedelta(hours=3),
    )
    assert report["financial_execution"] is False
    assert report["denominator_1h"]["status_counts"]["VALID_HYPOTHETICAL"] == 3
    assert report["denominator_1h"]["valid_cost_and_causality"] == 3
    assert report["denominator_1h"]["independent_by_instrument_4h_bucket"] == 1
    assert report["denominator_1h"]["coverage_labeled_buckets"] == 1
    assert report["sample"]["wallet_count"] == 1
    assert report["comparators"]["cash"]["matched_buckets"] == 1
    assert report["comparators"]["cash"]["episode_weighted_wallet_net_bps"] > 0
    assert report["comparators"]["lagged_1h_trend"]["matched_buckets"] == 1
    assert report["comparators"]["lagged_1h_trend"]["equal_day_incremental"]["equal_day_mean_bps"] == 0
    assert report["verdict"] == "INCONCLUSIVE_UNQUALIFIED_HISTORY"
    with pytest.raises(ValueError,match="budget"):
        analyze_historical(
            opportunity_db=source,outcome_db=outcomes,price_db=prices,
            cutoff=T0+timedelta(hours=3),max_rows=1,
        )


def test_negative_gross_is_not_promoted_by_beating_a_weaker_control(tmp_path) -> None:
    source, outcomes, prices = _fixture(tmp_path)
    with closing(sqlite3.connect(outcomes)) as db, db:
        payload=json.loads(db.execute(
            "SELECT value_json FROM copy_markouts WHERE opportunity_id='op1' AND horizon='1h'"
        ).fetchone()[0])
        payload["direction"]=-1
        gross=-(Decimal(103)/Decimal(102)-1)
        payload["signed_gross_return"]=str(gross)
        payload["hypothetical_net_return"]=str(gross-Decimal("0.001"))
        db.execute(
            "UPDATE copy_markouts SET value_json=? WHERE opportunity_id='op1' AND horizon='1h'",
            (json.dumps(payload),),
        )
    result=analyze_historical(
        opportunity_db=source,outcome_db=outcomes,price_db=prices,
        cutoff=T0+timedelta(hours=3),
    )
    assert result["comparators"]["cash"]["equal_day_wallet_net"]["equal_day_mean_bps"] < 0
    assert result["comparators"]["cash"]["equal_day_incremental"]["equal_day_mean_bps"] < 0
    assert result["predictive_qualified"] is False
