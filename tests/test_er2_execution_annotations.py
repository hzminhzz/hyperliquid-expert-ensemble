"""ER2 qualification: optional execution annotation state machines."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ensemble.execution_annotations import (
    ExecutionLabel,
    ExecutionObservation,
    NativeProgramStatus,
    SegmentStatus,
    SegmentTermination,
    advance_segment,
    apply_timer,
    classify_mixed_activity,
    interrupt_segment,
    start_segment,
    update_native_program,
)

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def obs(
    event_id: str,
    delta: str,
    seconds: int,
    *,
    quantity_after: str = "1",
    oid: str | None = None,
    program: str | None = None,
):
    return ExecutionObservation(
        event_id=event_id,
        expert_id="wallet",
        instrument_id="hyperliquid:mainnet:perp:default:BTC",
        delta_quantity=Decimal(delta),
        quantity_after=Decimal(quantity_after),
        event_time=T0 + timedelta(seconds=seconds),
        known_at=T0 + timedelta(seconds=seconds),
        oid=oid,
        native_program_id=program,
    )


def test_native_program_link_is_observed_not_inferred_and_cancel_keeps_execution():
    program = update_native_program(
        None,
        program_id="twap-1",
        status=NativeProgramStatus.EXECUTING,
        observed_at=T0,
        executed_delta=Decimal("0.4"),
        intended_quantity=Decimal(2),
        source_ref="fill:1",
    )
    program = update_native_program(
        program,
        program_id="twap-1",
        status=NativeProgramStatus.TERMINATED,
        observed_at=T0 + timedelta(minutes=1),
        executed_delta=Decimal("0.2"),
    )
    assert program.executed_quantity == Decimal("0.6")
    assert program.intended_quantity == Decimal(2)
    assert program.status is NativeProgramStatus.TERMINATED


def test_native_link_labels_segment_confirmed_without_changing_quantity():
    first = start_segment(obs("a", "0.2", 0, program="twap-1"), segment_id="seg")
    assert first is not None
    assert first.label is ExecutionLabel.NATIVE_CONFIRMED
    assert first.net_quantity == Decimal("0.2")


def test_pause_and_timeout_are_only_known_when_timer_fires():
    segment = start_segment(obs("a", "1", 0), segment_id="seg")
    assert segment is not None
    assert apply_timer(segment, now=T0 + timedelta(seconds=89)).status is SegmentStatus.ACTIVE
    paused = apply_timer(segment, now=T0 + timedelta(seconds=90))
    assert paused.status is SegmentStatus.PAUSED
    assert paused.termination is None

    # Timeout is not backdated into the prior paused object.
    terminal = apply_timer(paused, now=T0 + timedelta(minutes=10))
    assert terminal.status is SegmentStatus.TERMINAL
    assert terminal.termination is SegmentTermination.TIMEOUT_UNKNOWN
    assert paused.status is SegmentStatus.PAUSED


def test_schedule_like_requires_four_observations_and_two_minute_span():
    segment = start_segment(obs("a", "0.1", 0), segment_id="seg")
    assert segment is not None
    for idx, seconds in enumerate((40, 80, 120), start=1):
        segment, nxt = advance_segment(
            segment,
            obs(f"a{idx}", "0.1", seconds),
            now=T0 + timedelta(seconds=seconds),
        )
        assert nxt is None
    assert segment.label is ExecutionLabel.SCHEDULE_LIKE
    assert segment.net_quantity == Decimal("0.4")


def test_opposite_flow_terminates_conservatively_and_starts_new_segment():
    segment = start_segment(obs("a", "1", 0), segment_id="seg")
    assert segment is not None
    terminal, next_segment = advance_segment(
        segment,
        obs("b", "-0.2", 20, quantity_after="0.8"),
        now=T0 + timedelta(seconds=20),
    )
    assert terminal.termination is SegmentTermination.DIRECTION_CHANGE
    assert next_segment is not None
    assert next_segment.direction == -1
    assert terminal.net_quantity == Decimal(1)
    assert next_segment.net_quantity == Decimal("-0.2")


def test_flat_boundary_and_interrupt_do_not_fabricate_completion():
    segment = start_segment(obs("a", "1", 0), segment_id="seg")
    assert segment is not None
    terminal, next_segment = advance_segment(
        segment,
        obs("b", "-1", 30, quantity_after="0"),
        now=T0 + timedelta(seconds=30),
    )
    assert terminal.termination is SegmentTermination.FLAT_BOUNDARY
    assert next_segment is None

    fresh = start_segment(obs("c", "-0.5", 40, quantity_after="-0.5"), segment_id="new")
    assert fresh is not None
    interrupted = interrupt_segment(fresh)
    assert interrupted.termination is SegmentTermination.INTERRUPTED


def test_mixed_two_sided_activity_abstains_from_directional_story():
    observations = (
        obs("a", "0.1", 0),
        obs("b", "-0.1", 10),
        obs("c", "0.1", 20),
        obs("d", "-0.1", 30),
    )
    assert classify_mixed_activity(observations) is ExecutionLabel.INVENTORY_OR_MIXED


def test_terminal_segment_is_immutable():
    segment = start_segment(obs("a", "1", 0), segment_id="seg")
    assert segment is not None
    terminal = interrupt_segment(segment)
    with pytest.raises(ValueError, match="terminal segment"):
        advance_segment(
            terminal,
            obs("b", "1", 10),
            now=T0 + timedelta(seconds=10),
        )


def test_missing_metadata_remains_unknown_not_negative_native_label():
    segment = start_segment(obs("a", "0.1", 0), segment_id="seg")
    assert segment is not None
    assert segment.label is ExecutionLabel.DIRECTIONAL_SEGMENT_UNKNOWN
    assert not segment.native_program_refs
    assert not segment.oid_refs
