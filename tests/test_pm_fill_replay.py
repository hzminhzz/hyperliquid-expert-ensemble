"""Fill-only PM-01 source continuity and safe oracle-replay tests.

All test records are synthetic. No Rust, vendor requests or trading execution.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import gzip
import json

import pytest

from ensemble.pm_fill_replay import (
    _read_pair,
    _source_fills,
    _asof_full_book_anchor,
    _market_instrument,
    replay_wallet,
    CUT,
)
from ensemble.pm_shadow import MarketDataBlocked, MinuteBar
from ensemble.pm_sources import CohortEntry


ORIGIN = datetime(2026, 10, 20, tzinfo=UTC)
ADDRESS = "0x" + "1" * 40


def _fill(coin, tid, when, before, size, side, *, price="100"):
    return {
        "user": ADDRESS,
        "coin": coin,
        "tid": tid,
        "time": int(when.timestamp() * 1000),
        "side": side,
        "sz": str(size),
        "px": price,
        "start_position": str(before),
    }


def _write_pair(root, coin, rows, *, has_more=False):
    folder = root / "slow_pod_data_v1/hyperx_fills_cache_v1/pages/W001" / coin
    folder.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        {
            "page": 1,
            "page_size": 2000,
            "has_more": has_more,
            "fills": sorted(rows, key=lambda f: f["time"], reverse=True),
        }
    ).encode()
    (folder / "page-00001.json.gz").write_bytes(gzip.compress(raw))
    (folder / "page-00001.meta.json").write_text(
        json.dumps({"alias": "W001", "coin": coin, "page": 1, "sha256": sha256(raw).hexdigest()})
    )
    return folder


def _fixture(root: Path):
    rows = [
        _fill("BTC", 1, ORIGIN, 0, 1, "B"),
        _fill("BTC", 2, ORIGIN + timedelta(minutes=1), 1, 1, "A"),
        _fill("BTC", 3, ORIGIN + timedelta(minutes=2), 0, 2, "B"),
    ]
    return _write_pair(root, "BTC", rows), rows


class FixedMarket:
    """Synthetic source: always 100, high volume, exact requested minute."""

    def __init__(self, blocked=()):
        self.blocked = set(blocked)
        self.observed = []

    def get(self, instrument, action_at):
        self.observed.append((instrument, action_at))
        if instrument in self.blocked:
            raise MarketDataBlocked("TEST_MISSING_NATIVE_1M")
        minute = action_at.replace(second=0, microsecond=0)
        if action_at != minute:
            minute += timedelta(minutes=1)
        return MinuteBar(minute, Decimal("100"), Decimal("1000000"), "a" * 64)


def _entry():
    return CohortEntry(
        "W001",
        "original",
        ADDRESS,
        sha256(ADDRESS.encode()).hexdigest(),
        "VENDOR_ROSTER_CACHED_HISTORICAL_ONLY",
        1,
        10,
    )


def test_historical_source_filter_and_position_linking(tmp_path):
    _fixture(tmp_path)
    rows = _read_pair(tmp_path, "W001", ADDRESS, "BTC")
    assert len(rows) == 3
    ordered = sorted(rows, key=lambda e: e[0])
    assert ordered[0][3:5] == (Decimal(0), Decimal(1))
    assert ordered[-1][3:5] == (Decimal(0), Decimal(2))
    merged, issues = _source_fills(tmp_path, "W001", ADDRESS, ["BTC"])
    assert not issues and len(merged) == 3


def test_same_exchange_millisecond_tids_must_not_undo_position_chain(tmp_path):
    same = ORIGIN
    # Trade-ID order intentionally contradicts position order.
    rows = [
        _fill("BTC", 99, same, 0, 1, "B"),
        _fill("BTC", 1, same, 1, 1, "B"),
        _fill("BTC", 55, same + timedelta(minutes=2), 2, 2, "A"),
    ]
    _write_pair(tmp_path, "BTC", rows)
    linked, errors = _source_fills(tmp_path, "W001", ADDRESS, ["BTC"])
    assert errors == []
    assert [e[1] for e in linked] == [99, 1, 55]
    assert [e[4] for e in linked] == [Decimal(1), Decimal(2), Decimal(0)]


def test_bad_checksum_rejected(tmp_path):
    folder, _ = _fixture(tmp_path)
    file = folder / "page-00001.meta.json"
    info = json.loads(file.read_text())
    info["sha256"] = "0" * 64
    file.write_text(json.dumps(info))
    with pytest.raises(ValueError, match="SHA mismatch"):
        _read_pair(tmp_path, "W001", ADDRESS, "BTC")


def test_duplicate_trades_rejected(tmp_path):
    _, rows = _fixture(tmp_path)
    rows[-1]["tid"] = rows[0]["tid"]
    _write_pair(tmp_path, "BTC", rows)
    with pytest.raises(ValueError, match="duplicate"):
        _read_pair(tmp_path, "W001", ADDRESS, "BTC")


def test_vendor_event_after_frozen_cutoff_excluded(tmp_path):
    _, rows = _fixture(tmp_path)
    rows[-1]["time"] = int(CUT.timestamp() * 1000) + 1000
    _write_pair(tmp_path, "BTC", rows)
    assert len(_read_pair(tmp_path, "W001", ADDRESS, "BTC")) == 2


def test_wrong_identity_rejected(tmp_path):
    _fixture(tmp_path)
    with pytest.raises(ValueError, match="wallet/coin mismatch"):
        _read_pair(tmp_path, "W001", "0x" + "2" * 40, "BTC")


def _nonflat_two_coin_source(tmp_path):
    # Source already has long BTC and short ETH when first observed. No
    # fabricated flat start, and no reliance on an external Rust snapshot.
    _write_pair(
        tmp_path,
        "BTC",
        [
            _fill("BTC", 1, ORIGIN, 2, 1, "B"),
            _fill("BTC", 3, ORIGIN + timedelta(hours=1), 3, 3, "A"),
        ],
    )
    _write_pair(
        tmp_path,
        "ETH",
        [
            _fill("ETH", 2, ORIGIN + timedelta(minutes=1), -4, 1, "B"),
            _fill("ETH", 4, ORIGIN + timedelta(hours=1), -3, 3, "B"),
        ],
    )


def test_nonflat_source_inventory_can_anchor_after_all_coins_observed(tmp_path):
    _nonflat_two_coin_source(tmp_path)
    rows, errs = _source_fills(tmp_path, "W001", ADDRESS, ["BTC", "ETH"])
    assert not errs
    anchor = _asof_full_book_anchor(rows, ["BTC", "ETH"])
    assert anchor is not None
    when, positions, warmup_rows, gap_count, nonflat_count = anchor
    assert when == ORIGIN + timedelta(minutes=1)
    assert positions == {"BTC": Decimal(3), "ETH": Decimal(-3)}
    assert warmup_rows == 2 and gap_count == 0 and nonflat_count == 2


def test_complete_hindsight_two_coin_follower_and_control_reconciled(tmp_path):
    _nonflat_two_coin_source(tmp_path)
    for delay in (5, 15, 60):
        outcome = replay_wallet(tmp_path, _entry(), FixedMarket(), delay)
        assert outcome["status"] == "HISTORICAL_HINDSIGHT_SIMULATION_ONLY"
        assert Decimal(outcome["pnl"]) < 0  # cost + financing on fixed synthetic prices
        assert outcome["source_warmup_fills_excluded"] == 2
        assert outcome["nonzero_first_observed_instruments"] == 2
        assert outcome["hypothetical_fill_count"] > 0
        assert outcome["actual_follower_fills"] is None
        assert outcome["source_equity_accounting_reconciled"] is False
        assert "matched_long_only_market_control_pnl" in outcome


def test_same_hypothetical_minute_actions_are_one_whole_book_rebalance(tmp_path):
    """Do not overtrade the same 1m open or reuse its volume budget."""
    _write_pair(
        tmp_path,
        "BTC",
        [
            _fill("BTC", 1, ORIGIN, 2, 1, "B"),
            _fill("BTC", 3, ORIGIN + timedelta(hours=1, seconds=10), 3, 1, "B"),
        ],
    )
    _write_pair(
        tmp_path,
        "ETH",
        [
            _fill("ETH", 2, ORIGIN + timedelta(minutes=1), -4, 1, "B"),
            _fill("ETH", 4, ORIGIN + timedelta(hours=1, seconds=20), -3, 1, "B"),
        ],
    )
    result = replay_wallet(tmp_path, _entry(), FixedMarket(), 5)
    assert result["status"] == "HISTORICAL_HINDSIGHT_SIMULATION_ONLY"
    # Two fills for the initial book, two for the combined later target.
    assert result["hypothetical_fill_count"] == 4
    assert result["processed_source_fills_after_anchor"] == 2


def test_missing_hedge_market_blocks_entire_wallet_and_no_partial_pnl(tmp_path):
    _nonflat_two_coin_source(tmp_path)
    out = replay_wallet(
        tmp_path,
        _entry(),
        FixedMarket(blocked={"hyperliquid:mainnet:perp:default:ETH"}),
        5,
    )
    assert out["status"] == "BLOCKED_MARKET_OR_EXECUTION"
    assert out["reason"] == "TEST_MISSING_NATIVE_1M"
    assert out["instrument"].endswith(":ETH")
    assert out["pnl"] is None


def test_post_anchor_chain_break_fails_closed_without_partial_pnl(tmp_path):
    _nonflat_two_coin_source(tmp_path)
    _write_pair(
        tmp_path,
        "BTC",
        [
            _fill("BTC", 1, ORIGIN, 2, 1, "B"),
            _fill("BTC", 3, ORIGIN + timedelta(hours=1), 400, 1, "B"),
        ],
    )
    out = replay_wallet(tmp_path, _entry(), FixedMarket(), 5)
    assert out["status"] == "BLOCKED_SOURCE_CONTINUITY"
    assert out["instrument"] == "BTC" and out["pnl"] is None


def test_liquidity_cap_blocks_whole_source_book(tmp_path):
    _nonflat_two_coin_source(tmp_path)

    class Illiquid(FixedMarket):
        def get(self, instrument, action_at):
            bar = super().get(instrument, action_at)
            return MinuteBar(bar.timestamp, bar.open, Decimal("1"), bar.source_sha256)

    result = replay_wallet(tmp_path, _entry(), Illiquid(), 5)
    assert result["status"] == "BLOCKED_MARKET_OR_EXECUTION"
    assert result["reason"] == "MINUTE_PARTICIPATION_EXCEEDED"
    assert result["pnl"] is None


def test_paired_long_only_control_diverges_for_a_profitable_short(tmp_path):
    _nonflat_two_coin_source(tmp_path)

    class MovingMarket(FixedMarket):
        def get(self, instrument, action_at):
            bar = super().get(instrument, action_at)
            price = (
                Decimal("80")
                if instrument.endswith(":ETH") and action_at >= ORIGIN + timedelta(hours=1)
                else Decimal("100")
            )
            return MinuteBar(bar.timestamp, price, bar.quote_volume, bar.source_sha256)

    out = replay_wallet(tmp_path, _entry(), MovingMarket(), 5)
    assert out["status"] == "HISTORICAL_HINDSIGHT_SIMULATION_ONLY"
    assert Decimal(out["incremental_vs_matched_long_control"]) > 0


def test_delayed_order_after_cutoff_is_right_censored_not_time_traveled(tmp_path):
    rows = [
        _fill("BTC", 1, ORIGIN, 0, 1, "B"),
        _fill("BTC", 2, CUT - timedelta(minutes=30), 1, 1, "A"),
    ]
    _write_pair(tmp_path, "BTC", rows)
    out = replay_wallet(tmp_path, _entry(), FixedMarket(), 60)
    assert out["status"] == "HISTORICAL_HINDSIGHT_SIMULATION_ONLY"
    assert out["right_censored_source_fills"] == 1
    assert out["open_positions"] == 1
    assert out["terminal_mark_at"] < CUT.isoformat()


def test_source_gap_before_complete_vendor_roster_anchor_is_censored(tmp_path):
    _nonflat_two_coin_source(tmp_path)
    _write_pair(
        tmp_path,
        "BTC",
        [
            _fill("BTC", 1, ORIGIN - timedelta(minutes=4), 0, 1, "B"),
            _fill("BTC", 3, ORIGIN, 7, 1, "A"),
            _fill("BTC", 5, ORIGIN + timedelta(hours=1), 6, 6, "A"),
        ],
    )
    result = replay_wallet(tmp_path, _entry(), FixedMarket(), 5)
    assert result["gaps_in_censored_warmup"] == 1
    assert result["status"] == "HISTORICAL_HINDSIGHT_SIMULATION_ONLY"
    assert result["nonzero_first_observed_instruments"] >= 1


def test_opaque_hip3_namespace_cannot_be_priced_by_other_market():
    with pytest.raises(MarketDataBlocked, match="UNRESOLVED_OPAQUE"):
        _market_instrument("#12270")
    assert _market_instrument("xyz:NVDA") == "hyperliquid:mainnet:perp:xyz:NVDA"


def test_partial_vendor_tail_older_than_study_boundary_is_window_covered(tmp_path):
    rows = [
        _fill("BTC", 1, datetime(2026, 4, 1, tzinfo=UTC), 0, 1, "B"),
        _fill("BTC", 2, ORIGIN, 1, 1, "A"),
    ]
    _write_pair(tmp_path, "BTC", rows, has_more=True)
    assert len(_read_pair(tmp_path, "W001", ADDRESS, "BTC")) == 2


def test_partial_vendor_tail_that_does_not_reach_study_boundary_blocks(tmp_path):
    _write_pair(tmp_path, "BTC", [_fill("BTC", 1, ORIGIN, 0, 1, "B")], has_more=True)
    with pytest.raises(ValueError, match="study start"):
        _read_pair(tmp_path, "W001", ADDRESS, "BTC")
