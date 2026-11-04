"""Optional execution-program and directional-segment annotations.

This layer annotates additive path evidence. It never changes accounting state,
raw flow, cluster budget, or predictive authority.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum


class SegmentStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    TERMINAL = "TERMINAL"


class SegmentTermination(str, Enum):
    DIRECTION_CHANGE = "DIRECTION_CHANGE"
    FLAT_BOUNDARY = "FLAT_BOUNDARY"
    TIMEOUT_UNKNOWN = "TIMEOUT_UNKNOWN"
    INTERRUPTED = "INTERRUPTED"


class ExecutionLabel(str, Enum):
    NATIVE_CONFIRMED = "NATIVE_CONFIRMED"
    SCHEDULE_LIKE = "SCHEDULE_LIKE"
    DIRECTIONAL_SEGMENT_UNKNOWN = "DIRECTIONAL_SEGMENT_UNKNOWN"
    INVENTORY_OR_MIXED = "INVENTORY_OR_MIXED"


class NativeProgramStatus(str, Enum):
    OBSERVED = "OBSERVED"
    EXECUTING = "EXECUTING"
    FINISHED = "FINISHED"
    TERMINATED = "TERMINATED"
    ERROR = "ERROR"


@dataclass(slots=True, frozen=True)
class ExecutionObservation:
    event_id: str
    expert_id: str
    instrument_id: str
    delta_quantity: Decimal
    quantity_after: Decimal
    event_time: datetime
    known_at: datetime
    oid: str | None = None
    native_program_id: str | None = None
    crossed: bool | None = None
    forced: bool | None = None

    @property
    def direction(self) -> int:
        return 1 if self.delta_quantity > 0 else -1 if self.delta_quantity < 0 else 0


@dataclass(slots=True, frozen=True)
class NativeProgram:
    program_id: str
    status: NativeProgramStatus
    observed_at: datetime
    intended_quantity: Decimal | None = None
    executed_quantity: Decimal = Decimal(0)
    source_ref: str | None = None


@dataclass(slots=True, frozen=True)
class ExecutionSegment:
    segment_id: str
    expert_id: str
    instrument_id: str
    direction: int
    status: SegmentStatus
    label: ExecutionLabel
    first_event_time: datetime
    last_event_time: datetime
    first_known_at: datetime
    last_known_at: datetime
    net_quantity: Decimal
    gross_quantity: Decimal
    event_ids: tuple[str, ...]
    oid_refs: tuple[str, ...]
    native_program_refs: tuple[str, ...]
    material_seen: bool
    termination: SegmentTermination | None = None
    support_reason: str = ""


@dataclass(slots=True, frozen=True)
class SegmentPolicy:
    pause_after: timedelta = timedelta(seconds=90)
    end_after: timedelta = timedelta(minutes=10)
    material_quantity: Decimal = Decimal(0)
    schedule_min_observations: int = 4
    schedule_min_span: timedelta = timedelta(minutes=2)


def update_native_program(
    current: NativeProgram | None,
    *,
    program_id: str,
    status: NativeProgramStatus,
    observed_at: datetime,
    executed_delta: Decimal = Decimal(0),
    intended_quantity: Decimal | None = None,
    source_ref: str | None = None,
) -> NativeProgram:
    executed = (current.executed_quantity if current else Decimal(0)) + executed_delta
    intended = intended_quantity if intended_quantity is not None else (
        current.intended_quantity if current else None
    )
    return NativeProgram(
        program_id=program_id,
        status=status,
        observed_at=observed_at,
        intended_quantity=intended,
        executed_quantity=executed,
        source_ref=source_ref or (current.source_ref if current else None),
    )


def _label(segment: ExecutionSegment) -> tuple[ExecutionLabel, str]:
    if segment.native_program_refs:
        return ExecutionLabel.NATIVE_CONFIRMED, "qualified native program linkage"
    span = segment.last_event_time - segment.first_event_time
    if len(segment.event_ids) >= 4 and span >= timedelta(minutes=2):
        return ExecutionLabel.SCHEDULE_LIKE, "timing support guard met; heuristic only"
    return ExecutionLabel.DIRECTIONAL_SEGMENT_UNKNOWN, "insufficient native/schedule support"


def start_segment(
    observation: ExecutionObservation,
    *,
    segment_id: str,
    policy: SegmentPolicy | None = None,
) -> ExecutionSegment | None:
    policy = policy or SegmentPolicy()
    if observation.direction == 0:
        return None
    material = abs(observation.delta_quantity) >= policy.material_quantity
    segment = ExecutionSegment(
        segment_id=segment_id,
        expert_id=observation.expert_id,
        instrument_id=observation.instrument_id,
        direction=observation.direction,
        status=SegmentStatus.ACTIVE if material else SegmentStatus.CANDIDATE,
        label=ExecutionLabel.DIRECTIONAL_SEGMENT_UNKNOWN,
        first_event_time=observation.event_time,
        last_event_time=observation.event_time,
        first_known_at=observation.known_at,
        last_known_at=observation.known_at,
        net_quantity=observation.delta_quantity,
        gross_quantity=abs(observation.delta_quantity),
        event_ids=(observation.event_id,),
        oid_refs=((observation.oid,) if observation.oid else ()),
        native_program_refs=(
            (observation.native_program_id,) if observation.native_program_id else ()
        ),
        material_seen=material,
    )
    label, reason = _label(segment)
    return replace(segment, label=label, support_reason=reason)


def advance_segment(
    segment: ExecutionSegment,
    observation: ExecutionObservation,
    *,
    now: datetime,
    policy: SegmentPolicy | None = None,
) -> tuple[ExecutionSegment, ExecutionSegment | None]:
    """Advance one segment and optionally return a new opposite-direction segment."""
    policy = policy or SegmentPolicy()
    if segment.status is SegmentStatus.TERMINAL:
        raise ValueError("terminal segment is immutable")
    if now < segment.last_known_at:
        raise ValueError("timer/input time cannot move backward")

    if observation.direction == 0:
        return segment, None

    if observation.direction != segment.direction:
        if observation.quantity_after == 0:
            return (
                replace(
                    segment,
                    status=SegmentStatus.TERMINAL,
                    termination=SegmentTermination.FLAT_BOUNDARY,
                ),
                None,
            )
        terminal = replace(
            segment,
            status=SegmentStatus.TERMINAL,
            termination=SegmentTermination.DIRECTION_CHANGE,
        )
        return terminal, start_segment(
            observation,
            segment_id=f"{segment.segment_id}:next",
            policy=policy,
        )

    material_seen = segment.material_seen or (
        abs(observation.delta_quantity) >= policy.material_quantity
    )
    candidate = replace(
        segment,
        status=SegmentStatus.ACTIVE if material_seen else SegmentStatus.CANDIDATE,
        last_event_time=observation.event_time,
        last_known_at=observation.known_at,
        net_quantity=segment.net_quantity + observation.delta_quantity,
        gross_quantity=segment.gross_quantity + abs(observation.delta_quantity),
        event_ids=segment.event_ids + (observation.event_id,),
        oid_refs=tuple(dict.fromkeys(
            segment.oid_refs + ((observation.oid,) if observation.oid else ())
        )),
        native_program_refs=tuple(dict.fromkeys(
            segment.native_program_refs
            + ((observation.native_program_id,) if observation.native_program_id else ())
        )),
        material_seen=material_seen,
    )
    label, reason = _label(candidate)
    return replace(candidate, label=label, support_reason=reason), None


def apply_timer(
    segment: ExecutionSegment,
    *,
    now: datetime,
    policy: SegmentPolicy | None = None,
) -> ExecutionSegment:
    """Apply a timer at the time it becomes known; never backdate timeout knowledge."""
    policy = policy or SegmentPolicy()
    if segment.status is SegmentStatus.TERMINAL:
        return segment
    inactivity = now - segment.last_known_at
    if inactivity >= policy.end_after:
        return replace(
            segment,
            status=SegmentStatus.TERMINAL,
            termination=SegmentTermination.TIMEOUT_UNKNOWN,
        )
    if inactivity >= policy.pause_after:
        return replace(segment, status=SegmentStatus.PAUSED)
    return segment


def interrupt_segment(segment: ExecutionSegment) -> ExecutionSegment:
    if segment.status is SegmentStatus.TERMINAL:
        return segment
    return replace(
        segment,
        status=SegmentStatus.TERMINAL,
        termination=SegmentTermination.INTERRUPTED,
    )


def classify_mixed_activity(
    observations: tuple[ExecutionObservation, ...],
) -> ExecutionLabel:
    directions = {obs.direction for obs in observations if obs.direction}
    if len(directions) > 1:
        return ExecutionLabel.INVENTORY_OR_MIXED
    if any(obs.native_program_id for obs in observations):
        return ExecutionLabel.NATIVE_CONFIRMED
    if len(observations) >= 4:
        span = max(obs.event_time for obs in observations) - min(
            obs.event_time for obs in observations
        )
        if span >= timedelta(minutes=2):
            return ExecutionLabel.SCHEDULE_LIKE
    return ExecutionLabel.DIRECTIONAL_SEGMENT_UNKNOWN
