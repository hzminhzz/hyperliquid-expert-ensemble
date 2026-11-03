"""Causal and accounting regression tests for the read-only production wallet audit."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ensemble.wallet_audit import (
    Episode,
    ObservedWallet,
    day_block_uncertainty,
    dedup_episodes,
    duplicate_wallet_signatures,
    holm,
    policy_comparisons,
    same_day_policy_comparisons,
    scan_ledger,
    validate_episode,
)


def t(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


def episode(
    day: int,
    wallet: str = "wallet1",
    coin: str = "BTC",
    horizon: str = "1h",
    delta: float = 15.0,
    known_hour: int = 12,
    label_delay_days: int = 0,
) -> Episode:
    known = t(day, known_hour)
    return Episode(
        wallet, "hyperliquid:mainnet:perp:default:" + coin, horizon, "ENTER",
        1, known, known + timedelta(seconds=2),
        known + timedelta(days=label_delay_days, hours=4),
        "UNSPECIFIED", delta + 20.0, delta + 10.0, 10.0, 24.0,
    )


def decision_and_mark() -> tuple[dict, dict, dict]:
    known = t(15)
    ready = known + timedelta(seconds=2)
    d = {
        "wallet_id": "wallet1", "instrument_id": "hyperliquid:mainnet:perp:default:BTC",
        "family": "ENTER", "copy_direction": 1, "first_event_at": (known - timedelta(milliseconds=5)).isoformat(),
        "first_known_at": known.isoformat(), "earliest_actionable_at": ready.isoformat(),
        "payload": {"source_quantity_before": "0.000", "source_coverage": "Seeded"},
    }
    m = {
        "status": "VALID_HYPOTHETICAL", "direction": 1,
        "price_model": "MID_TO_MID_HYPOTHETICAL_NOT_EXECUTABLE",
        "entry_at": (ready + timedelta(seconds=30)).isoformat(),
        "exit_at": (ready + timedelta(hours=1, seconds=25)).isoformat(),
        "signed_gross_return": "0.002", "hypothetical_net_return": "0.001",
        "cost_bps": "10",
    }
    c = {
        "control_family": "same_coin_lagged_1h_momentum", "market_only_net_return": "0.0005",
        "eligible_at": known.isoformat(),
    }
    return d, m, c


def test_valid_entry_cost_and_sampled_delay() -> None:
    d, m, c = decision_and_mark()
    record, reason = validate_episode(d, m, c, "1h", (t(15) + timedelta(hours=2)).isoformat())
    assert reason == "ACCEPTED"
    assert record is not None
    assert round(record.gross_bps) == 20
    assert round(record.net_10bps) == 10
    assert round(record.delta_bps or 0) == 5
    assert record.delay_s == 30.0


def test_inherited_position_not_a_new_enter() -> None:
    d, m, c = decision_and_mark()
    d["payload"]["source_quantity_before"] = "0.3"
    assert validate_episode(d, m, c, "1h", t(16).isoformat())[1] == "INHERITED_POSITION_OR_INVALID_OPEN"


def test_reduction_cannot_invert_to_short_signal() -> None:
    d, m, c = decision_and_mark()
    d["family"] = "DE_RISK"
    d["copy_direction"] = -1
    assert validate_episode(d, m, c, "1h", t(16).isoformat())[1] == "NON_ENTRY_ACTION"


def test_known_before_event_is_rejected() -> None:
    d, m, c = decision_and_mark()
    d["first_event_at"] = (t(15) + timedelta(seconds=3)).isoformat()
    assert validate_episode(d, m, c, "1h", t(16).isoformat())[1] == "CAUSAL_TIMESTAMP_FAILURE"


def test_late_sample_or_outcome_is_rejected() -> None:
    d, m, c = decision_and_mark()
    m["entry_at"] = (t(15) + timedelta(seconds=200)).isoformat()
    assert validate_episode(d, m, c, "1h", t(16).isoformat())[1] == "ENTRY_PRICE_NOT_ACTIONABLE"
    d, m, c = decision_and_mark()
    assert validate_episode(d, m, c, "1h", t(15).isoformat())[1] == "FUTURE_OUTCOME_LEAKAGE"


def test_corrupt_cost_is_rejected_without_double_count() -> None:
    d, m, c = decision_and_mark()
    m["hypothetical_net_return"] = "0.000"
    assert validate_episode(d, m, c, "1h", t(16).isoformat())[1] == "CORRUPT_MARKOUT_COST"


def test_control_cannot_be_chosen_after_wallet_signal() -> None:
    d, m, c = decision_and_mark()
    c["eligible_at"] = (t(15) + timedelta(hours=1)).isoformat()
    assert validate_episode(d, m, c, "1h", t(16).isoformat())[1] == "CONTROL_NOT_POINT_IN_TIME"


def test_dedup_opposite_actions_in_same_horizon() -> None:
    first = episode(15)
    overlapping = Episode(
        "wallet1", first.instrument_id, "1h", "REVERSE", -1,
        first.known_at + timedelta(minutes=20),
        first.actionable_at + timedelta(minutes=20),
        first.outcome_known_at + timedelta(minutes=20), "Seeded",
        10.0, 0.0, 10.0, 1.0,
    )
    other_wallet = episode(15, "wallet2")
    next_hour = Episode(
        "wallet1", first.instrument_id, "1h", "ENTER", 1,
        first.known_at + timedelta(hours=2),
        first.actionable_at + timedelta(hours=2),
        first.outcome_known_at + timedelta(hours=2),
        "Seeded", 20.0, 10.0, 0.0, 2.0,
    )
    selected, removed = dedup_episodes([first, overlapping, other_wallet, next_hour])
    assert removed == 1
    assert selected == [first, other_wallet, next_hour]


def test_equity_snapshots_not_cash_flow_adjusted_trading_returns() -> None:
    w = ObservedWallet("wallet1")
    w.snapshot(t(15).isoformat(), {"coverage": "Seeded", "equity": {"account_value": "100"}, "partial_history": True, "positions": []})
    w.snapshot(t(16).isoformat(), {"coverage": "Seeded", "equity": {"account_value": "300"}, "partial_history": True, "positions": []})
    out = w.public_dict()
    assert out["observed_equity_delta_usd_NOT_PNL"] == 200.0
    assert out["verified_trading_pnl_usd"] is None
    assert out["verified_cash_flow_adjusted_return"] is None


def test_missing_coverage_does_not_start_valid_performance() -> None:
    w = ObservedWallet("wallet1")
    w.snapshot(t(15).isoformat(), {"coverage": "Degraded", "equity": {"account_value": "100"}})
    w.snapshot(t(16).isoformat(), {"coverage": "Seeded", "equity": {"account_value": "110"}})
    assert w.first_snapshot_at == t(15).isoformat()
    assert w.first_valid_snapshot_at == t(16).isoformat()
    assert w.first_equity == 110
    assert w.gaps_over_120m == 1


def test_read_only_ledger_source_includes_removed_wallet(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.sqlite3"
    db = sqlite3.connect(ledger)
    db.executescript("""
    CREATE TABLE ledger_meta(ledger_id TEXT,current_seq INTEGER);
    CREATE TABLE outbox_events(seq INTEGER PRIMARY KEY,kind TEXT,wallet_id TEXT,event_time TEXT,known_at TEXT,payload TEXT);
    INSERT INTO ledger_meta VALUES('test',3);
    """)
    for seq, wallet_id, kind, payload in [
        (1, "removed", "ACCOUNT_SNAPSHOT", {"coverage": "Seeded", "equity": {"account_value": "10"}}),
        (2, "removed", "POSITION_CHANGE", {"transition": "OPEN"}),
        (3, "active", "ACCOUNT_SNAPSHOT", {"coverage": "KnownFlat", "equity": {"account_value": "20"}}),
    ]:
        db.execute("INSERT INTO outbox_events VALUES(?,?,?,?,?,?)", (seq, kind, wallet_id, t(15).isoformat(), t(15).isoformat(), json.dumps(payload)))
    db.commit()
    db.close()
    wallets, manifest = scan_ledger(ledger)
    assert set(wallets) == {"removed", "active"}
    assert wallets["removed"].actions["OPEN"] == 1
    assert manifest["read_through_seq"] == 3


def test_day_bootstrap_is_deterministic_and_holm_conservative() -> None:
    blocks = {"2026-10-15": [10.0, 11.0], "2026-10-16": [-3.0], "2026-10-17": [4.0]}
    assert day_block_uncertainty(blocks) == day_block_uncertainty(blocks)
    adjusted = holm({"a": 0.01, "b": 0.04, "c": 0.06})
    assert adjusted["a"] >= 0.01
    assert adjusted["a"] <= adjusted["b"] <= adjusted["c"]


def test_topk_never_uses_future_matured_outcome() -> None:
    # Wallet A has five prior days of good results; B has future-dated labels.
    episodes: list[Episode] = []
    for day in (10, 11, 12, 13, 14):
        episodes.extend(episode(day, wallet="A", coin=f"ALT{i}", delta=20.0) for i in range(2))
        episodes.extend(episode(day, wallet="B", coin=f"MEME{i}", delta=100.0, label_delay_days=20) for i in range(2))
    episodes.extend([episode(15, wallet="A", delta=8.0), episode(15, wallet="B", delta=-20.0)])
    _, days = policy_comparisons(episodes)
    value = [x for x in days if x["horizon"] == "1h" and x["policy"] == "EX_ANTE_TOP_5" and x["utc_day"] == "2026-10-15"]
    assert len(value) == 1
    assert value[0]["paired_events"] == 1
    assert value[0]["selected_wallet_count"] == 1


def test_identical_wallet_trajectories_are_not_independent_evidence() -> None:
    observations = [
        episode(day, wallet=wallet, coin=f"ALT{i}")
        for wallet in ("A", "B")
        for day in (10, 11, 12, 13, 14)
        for i in range(2)
    ]
    clusters, labels = duplicate_wallet_signatures(observations)
    assert len(clusters) == 1
    assert clusters[0]["wallet_count"] == 2
    assert labels["A"] == labels["B"]


def test_policy_delta_uses_exact_same_test_days() -> None:
    observations = [
        {"horizon": "1h", "policy": "ALL_WALLETS_COIN_TIME_DEDUP", "utc_day": "2026-10-10", "mean_incremental_bps": 2.0},
        {"horizon": "1h", "policy": "ALL_WALLETS_COIN_TIME_DEDUP", "utc_day": "2026-10-11", "mean_incremental_bps": 12.0},
        {"horizon": "1h", "policy": "ALL_WALLETS_COIN_TIME_DEDUP", "utc_day": "2026-10-12", "mean_incremental_bps": -1.0},
        {"horizon": "1h", "policy": "EX_ANTE_TOP_5", "utc_day": "2026-10-11", "mean_incremental_bps": 17.0},
        {"horizon": "1h", "policy": "EX_ANTE_TOP_5", "utc_day": "2026-10-12", "mean_incremental_bps": -3.0},
    ]
    comparisons = same_day_policy_comparisons(observations)
    assert len(comparisons) == 1
    assert comparisons[0]["matched_utc_days"] == 2
    assert comparisons[0]["mean_topk_minus_all_wallet_coin_time_bps"] == 1.5
