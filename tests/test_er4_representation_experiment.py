"""ER4 qualification: outcome semantics and frozen representation protocol."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ensemble.outcomes import (
    OutcomeSource,
    PricePoint,
    compute_outcome_record,
    seal_outcome_manifest,
)
from ensemble.representation_experiment import (
    ExperimentStatus,
    assess_readiness,
    default_er4_protocol,
)

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def test_cost_is_subtracted_after_direction_for_long_and_short():
    prices = [
        PricePoint(T0, Decimal(100)),
        PricePoint(T0 + timedelta(minutes=15), Decimal(100)),
    ]
    for direction in (-1, 1):
        source = OutcomeSource(
            source_id=f"s:{direction}",
            coin="BTC",
            emitted_at=T0,
            signal_price=Decimal(100),
            direction=direction,
        )
        manifest = seal_outcome_manifest(
            sources=[source],
            created_at=T0,
            horizons=[("15m", timedelta(minutes=15))],
            cost_bps=Decimal(10),
        )
        record = compute_outcome_record(
            manifest=manifest, source=source, horizon="15m", prices=prices
        )
        assert record is not None
        assert record.net_return == Decimal("-0.001")


def test_price_beyond_max_lateness_is_censored():
    source = OutcomeSource(
        source_id="late",
        coin="BTC",
        emitted_at=T0,
        signal_price=Decimal(100),
    )
    manifest = seal_outcome_manifest(
        sources=[source],
        created_at=T0,
        horizons=[("15m", timedelta(minutes=15))],
        max_price_lateness=timedelta(seconds=30),
    )
    prices = [
        PricePoint(T0, Decimal(100)),
        PricePoint(T0 + timedelta(minutes=15, seconds=31), Decimal(101)),
    ]
    assert (
        compute_outcome_record(
            manifest=manifest, source=source, horizon="15m", prices=prices
        )
        is None
    )


def test_outcome_known_at_uses_price_availability_not_only_market_timestamp():
    source = OutcomeSource(
        source_id="known",
        coin="BTC",
        emitted_at=T0,
        signal_price=Decimal(100),
    )
    manifest = seal_outcome_manifest(
        sources=[source],
        created_at=T0,
        horizons=[("15m", timedelta(minutes=15))],
    )
    available = T0 + timedelta(minutes=15, seconds=20)
    prices = [
        PricePoint(T0, Decimal(100), known_at=T0),
        PricePoint(
            T0 + timedelta(minutes=15),
            Decimal(101),
            known_at=available,
        ),
    ]
    record = compute_outcome_record(
        manifest=manifest, source=source, horizon="15m", prices=prices
    )
    assert record is not None
    assert record.outcome_known_at == available


def test_er4_protocol_is_frozen_and_r2_optional():
    protocol = default_er4_protocol(
        frozen_at=T0,
        holdout_start=T0 + timedelta(days=1),
        holdout_end=T0 + timedelta(days=31),
    )
    assert protocol.primary_horizon == "15m"
    assert protocol.secondary_horizon == "1h"
    assert protocol.attached_horizons == ("1m", "5m", "15m", "1h", "4h", "24h")
    assert protocol.arms[0].arm_id == "R0"
    assert protocol.arms[1].arm_id == "R1"
    assert protocol.arms[2].requires_annotations is True

    no_annotations = assess_readiness(
        protocol,
        available_rows=100,
        mature_rows=100,
        annotation_available=False,
        minimum_mature_rows=50,
    )
    assert no_annotations.status is ExperimentStatus.READY
    assert no_annotations.runnable_arms == ("R0", "R1")


def test_absent_or_immature_future_data_is_blocked_not_promoted():
    protocol = default_er4_protocol(
        frozen_at=T0,
        holdout_start=T0 + timedelta(days=1),
        holdout_end=T0 + timedelta(days=31),
    )
    absent = assess_readiness(
        protocol,
        available_rows=0,
        mature_rows=0,
        annotation_available=False,
        minimum_mature_rows=50,
    )
    assert absent.status is ExperimentStatus.BLOCKED
    assert absent.reason == "NO_RECORDED_EVIDENCE_ROWS"

    immature = assess_readiness(
        protocol,
        available_rows=100,
        mature_rows=10,
        annotation_available=True,
        minimum_mature_rows=50,
    )
    assert immature.status is ExperimentStatus.BLOCKED
    assert immature.reason == "INSUFFICIENT_OUTCOME_MATURITY"
