"""Point-in-time recorder: append-only cuts, source timestamps, and censored outcomes."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ensemble.research_recorder import (
    HORIZONS,
    MODEL_ID,
    CutInput,
    ResearchRecorder,
)

T0 = datetime(2026, 10, 8, 12, 0, 5, tzinfo=UTC)
BTC = "hyperliquid:mainnet:perp:default:BTC"


def cut(
    *,
    mid: Decimal | None = Decimal(100),
    quoted_at: datetime | None = T0 - timedelta(seconds=2),
    consensus: str = "0.25",
    direction: int = 1,
) -> CutInput:
    return CutInput(
        instrument_id=BTC,
        consensus=Decimal(consensus),
        lower_bound=Decimal("0.2"),
        upper_bound=Decimal("0.3"),
        direction=direction,
        grade="B",
        coverage=Decimal(1),
        supporting_clusters=5,
        opposing_clusters=1,
        flat_clusters=197,
        mid=mid,
        mid_known_at=quoted_at,
    )


def test_minute_cut_is_immutable_after_replay_and_restart(tmp_path):
    path = tmp_path / "research.db"
    first = ResearchRecorder(path)
    try:
        assert first.capture(
            decision_at=T0,
            ledger_id="ledger-a",
            ledger_seq=100,
            universe_revision="sha1",
            cuts=[cut()],
        ) == 1
        assert first.capture(
            decision_at=T0 + timedelta(seconds=30),
            ledger_id="ledger-a",
            ledger_seq=101,
            universe_revision="sha2",
            cuts=[cut(mid=Decimal(200), consensus="0.8")],
        ) == 0
    finally:
        first.close()

    second = ResearchRecorder(path)
    try:
        row = second.conn.execute(
            "SELECT ledger_seq,universe_revision,consensus,price_mid,model_id FROM signal_cuts"
        ).fetchone()
        assert row == (100, "sha1", "0.25", "100", MODEL_ID)
        assert second.stats()["cut_rows"] == 1
        assert second.stats()["price_rows"] == 1
    finally:
        second.close()


def test_future_markout_requires_time_maturity_and_uses_first_future_price(tmp_path):
    recorder = ResearchRecorder(tmp_path / "research.db", assumed_cost_bps=Decimal(10))
    try:
        recorder.capture(
            decision_at=T0,
            ledger_id="ledger",
            ledger_seq=1,
            universe_revision="a",
            cuts=[cut()],
        )
        assert recorder.settle_due(now=T0 + timedelta(minutes=15))["15m"] == 0
        t1 = T0 + timedelta(minutes=15, seconds=30)
        recorder.capture(
            decision_at=t1,
            ledger_id="ledger",
            ledger_seq=2,
            universe_revision="a",
            cuts=[cut(mid=Decimal(105), quoted_at=t1)],
        )
        recorder.settle_due(now=T0 + timedelta(minutes=17))
        row = recorder.conn.execute(
            """
            SELECT status,market_return,directional_gross,net_after_assumed_cost,
                   o.price_known_at
            FROM forward_outcomes o
            JOIN signal_cuts c ON c.cut_id=o.cut_id
            WHERE o.horizon='15m' AND c.ledger_seq=1
            """
        ).fetchone()
        assert row == (
            "VALID",
            "0.05",
            "0.05",
            "0.049",
            t1.isoformat(),
        )
    finally:
        recorder.close()


def test_future_price_must_not_leak_from_before_target_or_after_tolerance(tmp_path):
    recorder = ResearchRecorder(tmp_path / "research.db")
    try:
        recorder.capture(
            decision_at=T0,
            ledger_id="ledger",
            ledger_seq=1,
            universe_revision="a",
            cuts=[cut()],
        )
        recorder.capture(
            decision_at=T0 + timedelta(minutes=14, seconds=30),
            ledger_id="ledger",
            ledger_seq=2,
            universe_revision="a",
            cuts=[cut(mid=Decimal(120), quoted_at=T0 + timedelta(minutes=14, seconds=30))],
        )
        recorder.capture(
            decision_at=T0 + timedelta(minutes=17),
            ledger_id="ledger",
            ledger_seq=3,
            universe_revision="a",
            cuts=[cut(mid=Decimal(130), quoted_at=T0 + timedelta(minutes=17))],
        )
        recorder.settle_due(now=T0 + timedelta(minutes=18))
        row = recorder.conn.execute(
            """
            SELECT status,market_return
            FROM forward_outcomes o JOIN signal_cuts c USING(cut_id)
            WHERE o.horizon='15m' AND c.ledger_seq=1
            """
        ).fetchone()
        assert row == ("CENSORED", None)
    finally:
        recorder.close()


def test_missing_and_stale_entry_quote_are_recorded_as_missing_not_zero(tmp_path):
    recorder = ResearchRecorder(tmp_path / "research.db")
    try:
        recorder.capture(
            decision_at=T0,
            ledger_id="ledger",
            ledger_seq=1,
            universe_revision="a",
            cuts=[
                cut(mid=Decimal(100), quoted_at=T0 - timedelta(seconds=35)),
            ],
        )
        row = recorder.conn.execute(
            "SELECT price_mid,price_known_at FROM signal_cuts"
        ).fetchone()
        assert row == (None, None)
        recorder.settle_due(now=T0 + timedelta(minutes=17))
        status = recorder.conn.execute(
            "SELECT status FROM forward_outcomes WHERE horizon='15m'"
        ).fetchone()[0]
        assert status == "CENSORED"
    finally:
        recorder.close()


def test_short_and_flat_cost_semantics_and_all_registered_horizons(tmp_path):
    assert [x[0] for x in HORIZONS] == [
        "1m", "5m", "15m", "1h", "4h", "24h"
    ]
    recorder = ResearchRecorder(tmp_path / "research.db")
    try:
        recorder.capture(
            decision_at=T0,
            ledger_id="ledger",
            ledger_seq=1,
            universe_revision="a",
            cuts=[cut(direction=-1)],
        )
        recorder.capture(
            decision_at=T0 + timedelta(minutes=15),
            ledger_id="ledger",
            ledger_seq=2,
            universe_revision="a",
            cuts=[cut(mid=Decimal(105), quoted_at=T0 + timedelta(minutes=15))],
        )
        recorder.settle_due(now=T0 + timedelta(minutes=17))
        row = recorder.conn.execute(
            """
            SELECT directional_gross,net_after_assumed_cost FROM forward_outcomes o
            JOIN signal_cuts c USING(cut_id)
            WHERE horizon='15m' AND c.ledger_seq=1
            """
        ).fetchone()
        assert row == ("-0.05", "-0.051")
    finally:
        recorder.close()


def test_naive_times_are_rejected(tmp_path):
    recorder = ResearchRecorder(tmp_path / "research.db")
    try:
        with pytest.raises(ValueError, match="timezone-aware"):
            recorder.capture(
                decision_at=datetime.fromisoformat("2026-10-08T12:00:00"),
                ledger_id="a",
                ledger_seq=1,
                universe_revision="v",
                cuts=[cut()],
            )
    finally:
        recorder.close()
