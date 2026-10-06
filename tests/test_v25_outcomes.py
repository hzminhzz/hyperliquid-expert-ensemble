"""V25 qualification: immutable forward-outcome ledger (Q12)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ensemble.outcomes import (
    OutcomeSource,
    OutcomeStore,
    PricePoint,
    compute_outcome_record,
    seal_outcome_manifest,
)

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def source() -> OutcomeSource:
    return OutcomeSource(
        source_id="ens:1",
        coin="BTC",
        emitted_at=T0,
        signal_price=Decimal(100),
        context=(("regime", "calm"),),
        coverage=(("reliability_mass", "1.0"),),
        restated_after_emission=True,
    )


def prices() -> list[PricePoint]:
    return [
        PricePoint(T0, Decimal(100)),
        PricePoint(T0 + timedelta(seconds=1), Decimal(101)),
        PricePoint(T0 + timedelta(seconds=30), Decimal(99)),
        PricePoint(T0 + timedelta(minutes=1), Decimal(102)),
        PricePoint(T0 + timedelta(minutes=5), Decimal(105)),
    ]


def test_q12_exact_forward_return_latency_cost_mfe_and_mae():
    manifest = seal_outcome_manifest(
        sources=[source()],
        created_at=T0,
        horizons=[("1m", timedelta(minutes=1))],
        latency_ms=1000,
        cost_bps=Decimal(10),
    )
    record = compute_outcome_record(
        manifest=manifest,
        source=source(),
        horizon="1m",
        prices=prices(),
    )
    assert record is not None

    assert record.raw_return == Decimal("0.02")
    assert record.latency_return == Decimal(102) / Decimal(101) - Decimal(1)
    assert record.net_return == record.latency_return - Decimal("0.001")
    assert record.mfe == Decimal(102) / Decimal(101) - Decimal(1)
    assert record.mae == Decimal(99) / Decimal(101) - Decimal(1)
    assert record.context == (("regime", "calm"),)
    assert record.coverage == (("reliability_mass", "1.0"),)
    assert record.restated_after_emission is True


def test_q12_manifest_is_content_addressed_and_order_independent():
    a = source()
    b = OutcomeSource(
        source_id="ens:2",
        coin="ETH",
        emitted_at=T0,
        signal_price=Decimal(200),
    )
    first = seal_outcome_manifest(sources=[a, b], created_at=T0)
    second = seal_outcome_manifest(sources=[b, a], created_at=T0 + timedelta(hours=1))

    assert first.manifest_hash == second.manifest_hash
    assert first.manifest_id == second.manifest_id
    assert first.sources == second.sources


def test_q12_append_is_idempotent_and_conflicting_rewrite_is_rejected(tmp_path: Path):
    manifest = seal_outcome_manifest(
        sources=[source()],
        created_at=T0,
        horizons=[("1m", timedelta(minutes=1))],
    )
    record = compute_outcome_record(
        manifest=manifest,
        source=source(),
        horizon="1m",
        prices=prices(),
    )
    assert record is not None

    store = OutcomeStore(tmp_path / "outcomes.db")
    try:
        store.register_manifest(manifest)
        assert store.append(record) is True
        assert store.append(record) is False

        conflicting = replace(record, horizon_price=Decimal(999))
        with pytest.raises(ValueError, match="immutable outcome"):
            store.append(conflicting)
    finally:
        store.close()


def test_q12_restart_resumes_from_remaining_manifest_pairs(tmp_path: Path):
    manifest = seal_outcome_manifest(
        sources=[source()],
        created_at=T0,
        horizons=[
            ("1m", timedelta(minutes=1)),
            ("5m", timedelta(minutes=5)),
        ],
    )
    one_minute = compute_outcome_record(
        manifest=manifest,
        source=source(),
        horizon="1m",
        prices=prices(),
    )
    assert one_minute is not None

    path = tmp_path / "outcomes.db"
    first = OutcomeStore(path)
    first.register_manifest(manifest)
    first.append(one_minute)
    first.close()

    resumed = OutcomeStore(path)
    try:
        resumed.register_manifest(manifest)
        assert resumed.pending_pairs(manifest) == (("ens:1", "5m"),)
    finally:
        resumed.close()


def test_q12_not_yet_knowable_horizon_does_not_create_record():
    manifest = seal_outcome_manifest(
        sources=[source()],
        created_at=T0,
        horizons=[("24h", timedelta(hours=24))],
    )
    record = compute_outcome_record(
        manifest=manifest,
        source=source(),
        horizon="24h",
        prices=prices(),
    )
    assert record is None
