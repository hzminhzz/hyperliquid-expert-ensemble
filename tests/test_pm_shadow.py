"""Adversarial PM-01 shadow evidence tests; no network, market writes, or brokers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

import pytest

from ensemble.pm_portfolio import (
    AccountEvent,
    Coverage,
    WalletSource,
    reconstruct_events,
    reconstruct_timeline,
    classify_transition,
    PortfolioDecision,
)
from ensemble.pm_shadow import (
    MarketDataBlocked,
    MinuteBar,
    Scenario,
    VerifiedHyperliquidMinuteSource,
    replay_shadow,
)
from ensemble.pm_research import _write_private_evidence
from ensemble.pm_sources import ORIGINAL_SIX, WAVE_ONE


BASE = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
BTC = "hyperliquid:mainnet:perp:default:BTC"
ETH = "hyperliquid:mainnet:perp:default:ETH"
SP500 = "hyperliquid:mainnet:perp:xyz:SP500"


def wallet():
    return WalletSource("w", "original", Coverage.PROSPECTIVE_VERIFIED, BASE.isoformat())


def snapshot(seq, when=BASE, positions=None, *, partial=False, equity="10000"):
    return AccountEvent(
        kind="ACCOUNT_SNAPSHOT",
        wallet_id="w",
        ledger_id="rust-ledger",
        seq=seq,
        event_at=when,
        received_at=when,
        known_at=when,
        positions={} if positions is None else positions,
        equity=Decimal(equity),
        partial_history=partial,
        coverage="KnownFlat" if positions is None else "Seeded",
    )


def change(seq, when, instrument, after, delta):
    return AccountEvent(
        kind="POSITION_CHANGE",
        wallet_id="w",
        ledger_id="rust-ledger",
        seq=seq,
        event_at=when,
        received_at=when,
        known_at=when,
        instrument=instrument,
        quantity_after=Decimal(after),
        delta=Decimal(delta),
        coverage="Seeded",
        partial_history=False,
    )


def test_valid_empty_snapshot_is_known_flat_not_missing():
    d = reconstruct_events(wallet(), [snapshot(5)], BASE)
    assert d.book_known and d.eligible and d.targets == {}
    assert d.reason == "OBSERVED_ONLY"


def test_atomic_snapshot_removes_missing_hedge_leg_without_fabricating_exit():
    t = BASE + timedelta(minutes=1)
    initial = snapshot(10, positions={BTC: Decimal("2"), ETH: Decimal("-3")})
    next_snap = snapshot(99, t, {BTC: Decimal("1")})
    before, after = reconstruct_timeline(wallet(), [next_snap, initial], [BASE, t])
    assert before.source_positions == {BTC: Decimal(2), ETH: Decimal(-3)}
    assert after.source_positions == {BTC: Decimal(1)}
    assert ETH not in after.targets
    # global Rust seq can have other wallets' observations between 10 and 99.
    assert after.eligible


def test_all_transition_families_and_flip_no_volume_double_count():
    assert [
        classify_transition(Decimal(a), Decimal(b))
        for a, b in [
            ("0", "2"),
            ("2", "3"),
            ("3", "1"),
            ("1", "0"),
            ("1", "-2"),
            ("-2", "-3"),
            ("-3", "-1"),
            ("-1", "0"),
        ]
    ] == ["OPEN", "ADD", "REDUCE", "CLOSE", "FLIP", "ADD", "REDUCE", "CLOSE"]
    rows = [snapshot(100, positions={BTC: Decimal("1")})]
    for n, (new, delta) in enumerate(
        [
            ("2", "1"),
            ("1", "-1"),
            ("-2", "-3"),
            ("0", "2"),
        ],
        start=1,
    ):
        rows.append(change(n * 10 + 100, BASE + timedelta(seconds=n), BTC, new, delta))
    out = reconstruct_events(wallet(), rows, BASE + timedelta(seconds=4))
    assert out.book_known and out.eligible and not out.source_positions


def test_bad_delta_fails_closed_with_unknown_not_flat():
    rows = [
        snapshot(1, positions={BTC: Decimal(2)}),
        change(2, BASE + timedelta(seconds=1), BTC, "4", "1"),
    ]
    out = reconstruct_events(wallet(), rows, BASE + timedelta(seconds=1))
    assert not out.eligible and not out.book_known
    assert out.reason == "RUST_DELTA_STATE_CONFLICT"


def test_duplicate_exactly_once_and_conflicting_duplicate_is_blocked():
    seed = snapshot(1, positions={BTC: Decimal(1)})
    change1 = change(7, BASE + timedelta(seconds=1), BTC, "2", "1")
    out = reconstruct_events(wallet(), [seed, change1, change1], BASE + timedelta(seconds=1))
    assert out.eligible and out.source_positions[BTC] == Decimal(2)
    conflict = change(7, BASE + timedelta(seconds=1), BTC, "20", "19")
    bad = reconstruct_events(wallet(), [seed, change1, conflict], BASE + timedelta(seconds=1))
    assert not bad.book_known
    assert bad.reason == "CONFLICTING_DUPLICATE_CURSOR"


def test_late_known_source_and_future_rows_never_influence_past_cut():
    source = snapshot(1, positions={BTC: Decimal("1")})
    delayed = AccountEvent(
        kind="POSITION_CHANGE",
        wallet_id="w",
        ledger_id="rust-ledger",
        seq=2,
        event_at=BASE + timedelta(minutes=1),
        received_at=BASE + timedelta(minutes=1, seconds=2),
        known_at=BASE + timedelta(minutes=8),
        coverage="Seeded",
        instrument=BTC,
        quantity_after=Decimal(3),
        delta=Decimal(2),
    )
    early = reconstruct_events(wallet(), [delayed, source], BASE + timedelta(minutes=2))
    assert early.targets[BTC] == 1 and early.eligible
    later = reconstruct_events(
        wallet(),
        [delayed, source],
        BASE + timedelta(minutes=8),
        max_snapshot_age=timedelta(minutes=10),
    )
    assert later.targets[BTC] == 3
    assert not reconstruct_events(wallet(), [delayed], BASE).book_known


def test_out_of_order_ledger_corruption_and_clock_regressions_rejected():
    with pytest.raises(ValueError, match="clocks"):
        AccountEvent(
            kind="ACCOUNT_SNAPSHOT",
            wallet_id="w",
            ledger_id="r",
            seq=1,
            event_at=BASE + timedelta(seconds=2),
            received_at=BASE,
            known_at=BASE,
            positions={},
            equity=Decimal(1),
        )
    a = snapshot(8, positions={BTC: Decimal(1)})
    b = snapshot(7, BASE + timedelta(seconds=1), {BTC: Decimal(4)})
    out = reconstruct_events(wallet(), [a, b], BASE + timedelta(seconds=1))
    assert out.reason == "OUT_OF_ORDER_SOURCE"
    assert not out.eligible


def test_source_feed_gap_requires_quarantine_even_after_reseed():
    gap = AccountEvent(
        kind="COVERAGE_CHANGE",
        wallet_id="__all_wallets__",
        ledger_id="rust-ledger",
        seq=2,
        event_at=BASE + timedelta(seconds=1),
        received_at=BASE + timedelta(seconds=1),
        known_at=BASE + timedelta(seconds=1),
        quality="degraded",
        gap_status="DISCONNECTED_GAP_UNVERIFIED",
    )
    cuts = [BASE, BASE + timedelta(seconds=1), BASE + timedelta(seconds=2)]
    timeline = reconstruct_timeline(
        wallet(),
        [
            snapshot(1, positions={BTC: Decimal(1)}),
            gap,
            snapshot(3, BASE + timedelta(seconds=2), {BTC: Decimal(1)}),
        ],
        cuts,
    )
    assert timeline[0].eligible
    assert not timeline[1].book_known
    assert timeline[1].reason == "SOURCE_COVERAGE_GAP"
    assert timeline[2].book_known and not timeline[2].eligible
    assert timeline[2].history_partial


def test_historical_partial_state_never_qualifies():
    out = reconstruct_events(
        wallet(), [snapshot(1, positions={BTC: Decimal(1)}, partial=True)], BASE
    )
    assert out.book_known and not out.eligible and out.history_partial


def test_scoped_partial_history_blocks_real_money_shadow_pnl():
    out = reconstruct_events(
        wallet(), [snapshot(1, positions={BTC: Decimal(1)}, partial=True)], BASE
    )
    replay = replay_shadow([out], FakeMarket(), Scenario("5m", 5))
    assert replay.status == "BLOCKED"
    assert replay.follower_pnl is None
    assert replay.reason == "UNVERIFIED_PARTIAL_SOURCE_ACCOUNT_HISTORY"


def test_gap_contamination_blocks_even_a_later_scoped_snapshot():
    gap = AccountEvent(
        kind="COVERAGE_CHANGE",
        wallet_id="__all_wallets__",
        ledger_id="rust-ledger",
        seq=2,
        event_at=BASE + timedelta(seconds=1),
        received_at=BASE + timedelta(seconds=1),
        known_at=BASE + timedelta(seconds=1),
        quality="degraded",
    )
    after = reconstruct_events(
        wallet(),
        [
            snapshot(1, positions={BTC: Decimal(1)}),
            gap,
            snapshot(3, BASE + timedelta(seconds=2), {BTC: Decimal(1)}),
        ],
        BASE + timedelta(seconds=2),
    )
    assert after.coverage_gap_seen
    outcome = replay_shadow([after], FakeMarket(), Scenario("5m", 5))
    assert outcome.status == "BLOCKED"
    assert outcome.reason == "UNRECONCILED_SOURCE_COVERAGE_GAP"


def test_snapshot_age_and_zero_equity_block():
    stale = reconstruct_events(wallet(), [snapshot(1)], BASE + timedelta(minutes=6))
    assert not stale.eligible and stale.reason == "STALE_ACCOUNT_SNAPSHOT"
    zero = reconstruct_events(wallet(), [snapshot(1, equity="0")], BASE)
    assert not zero.eligible and zero.reason == "NO_POSITIVE_ACCOUNT_EQUITY"


def test_invalid_instrument_and_non_finite_data_fails_validation():
    with pytest.raises(ValueError, match="instrument"):
        snapshot(1, positions={"BTC": Decimal(1)})
    with pytest.raises(ValueError, match="non-finite"):
        snapshot(1, positions={BTC: Decimal("NaN")})


class FakeMarket:
    def __init__(self, *, available=(BTC, ETH), prices=None, quote=Decimal("500000")):
        self.available = set(available)
        self.prices = prices or {}
        self.quote = quote

    def get(self, instrument, at):
        if instrument not in self.available:
            raise MarketDataBlocked("NO_SUPPORTED_EXCHANGE_MARKET")
        open_price = self.prices.get((instrument, at), Decimal(100))
        return MinuteBar(
            VerifiedHyperliquidMinuteSource._minute(at), open_price, self.quote, "a" * 64
        )


def decision(when, positions, *, equity=Decimal("10000"), known=BASE, name="w"):
    return PortfolioDecision(
        name,
        when,
        {k: float(v) for k, v in positions.items()},
        True,
        "OBSERVED_ONLY",
        source_positions=positions,
        source_equity=equity,
        book_known=True,
        history_partial=False,
        source_cursor=10,
        first_known_at=known,
    )


def test_shadow_long_short_rebalanced_whole_portfolio_and_market_control():
    t2 = BASE + timedelta(hours=2)
    rows = [
        decision(BASE, {BTC: Decimal("1"), ETH: Decimal("-2")}),
        decision(t2, {BTC: Decimal("0"), ETH: Decimal("0")}),
    ]
    at_1 = BASE + timedelta(minutes=5)
    at_2 = t2 + timedelta(minutes=5)
    market = FakeMarket(
        prices={
            (BTC, at_1): Decimal(100),
            (ETH, at_1): Decimal(100),
            (BTC, at_2): Decimal(110),
            (ETH, at_2): Decimal(90),
        }
    )
    out = replay_shadow(rows, market, Scenario("5m", 5, portfolio_multiplier=Decimal("0.1")))
    assert out.status == "AS_KNOWN_RETROSPECTIVE_HYPOTHETICAL"
    assert out.accounting_reconciled and out.hypothetical_fill_count == 4
    assert out.open_positions == 0 and out.last_hypothetical_fill_at == at_2
    assert out.follower_pnl > 0
    assert out.follower_pnl > out.market_only_pnl
    assert sum(out.per_instrument_pnl.values()) == out.follower_pnl
    assert out.fees > 0 and out.spread_slippage > 0 and out.funding_assumed > 0
    assert out.source_account_pnl is None and out.actual_broker_pnl is None


def test_5m_vs_60m_distinct_prices_future_clock_and_costs():
    t2 = BASE + timedelta(hours=2)
    decisions = [decision(BASE, {BTC: Decimal(1)}), decision(t2, {BTC: Decimal(0)})]
    market = FakeMarket(
        prices={
            (BTC, BASE + timedelta(minutes=5)): Decimal(100),
            (BTC, BASE + timedelta(minutes=60)): Decimal(115),
            (BTC, t2 + timedelta(minutes=5)): Decimal(120),
            (BTC, t2 + timedelta(minutes=60)): Decimal(90),
        }
    )
    a = replay_shadow(decisions, market, Scenario("5m", 5))
    b = replay_shadow(decisions, market, Scenario("60m", 60, slippage_bps=Decimal(30)))
    assert a.follower_pnl is not None and b.follower_pnl is not None
    assert a.follower_pnl > b.follower_pnl
    assert a.last_hypothetical_fill_at == t2 + timedelta(minutes=5)
    assert b.last_hypothetical_fill_at == t2 + timedelta(minutes=60)


def test_missing_hedge_leg_market_blocks_whole_book_no_cherrypicking():
    out = replay_shadow(
        [decision(BASE, {BTC: Decimal(1), SP500: Decimal(-2)})],
        FakeMarket(available=(BTC,)),
        Scenario("5m", 5),
    )
    assert out.status == "BLOCKED" and out.follower_pnl is None
    assert out.hypothetical_fill_count == 0
    assert out.reason == "NO_SUPPORTED_EXCHANGE_MARKET"


def test_liquidity_participation_fails_closed_even_if_one_leg_good():
    result = replay_shadow(
        [decision(BASE, {BTC: Decimal(1), ETH: Decimal(-1)})],
        FakeMarket(quote=Decimal(1)),
        Scenario("5m", 5),
    )
    assert result.status == "BLOCKED" and result.reason == "MINUTE_PARTICIPATION_EXCEEDED"
    assert not result.accounting_reconciled


def test_follower_portfolio_risk_limit_applies_across_hedges():
    result = replay_shadow(
        [decision(BASE, {BTC: Decimal(200), ETH: Decimal(-200)}, equity=Decimal(1000))],
        FakeMarket(),
        Scenario("5m", 5),
    )
    assert result.status == "BLOCKED" and result.reason == "PORTFOLIO_GROSS_RISK_EXCEEDED"


def test_prices_do_not_require_broker_credentials():
    # Instrument resolving must reject opaque, invalid or unsupportable DEX IDs.
    with pytest.raises(MarketDataBlocked, match="UNSUPPORTED_MARKET_SYMBOL"):
        VerifiedHyperliquidMinuteSource._family("hyperliquid:mainnet:perp:default:#999")
    with pytest.raises(MarketDataBlocked, match="UNSUPPORTED_HIP3_DEX"):
        VerifiedHyperliquidMinuteSource._family("hyperliquid:mainnet:perp:io:ANTH")
    assert VerifiedHyperliquidMinuteSource._family(BTC) == Path(
        "crypto_futures_ohlcv_1m_HYPERLIQUID_BTC_PERP"
    )


def test_prospective_first_known_after_decision_rejected_in_shadow():
    out = replay_shadow(
        [decision(BASE, {BTC: Decimal(1)}, known=BASE + timedelta(seconds=1))],
        FakeMarket(),
        Scenario("5m", 5),
    )
    assert out.status == "BLOCKED" and out.reason == "MISSING_FIRST_KNOWN_CLOCK"


def test_private_evidence_repeated_runs_have_identical_bytes_and_sha(tmp_path):
    out = tmp_path / "private"
    artifact = {
        "wallet_count": 36,
        "wallets": list(ORIGINAL_SIX + WAVE_ONE),
        "fingerprint": sha256(b"immutable-source").hexdigest(),
    }
    p1, h1 = _write_private_evidence(out, artifact)
    p2, h2 = _write_private_evidence(out, artifact)
    assert p1 == p2 and h1 == h2 == sha256(p1.read_bytes()).hexdigest()
    assert p1.stat().st_mode & 0o077 == 0
    assert out.stat().st_mode & 0o077 == 0
    assert json.loads(p1.read_text()) == artifact
