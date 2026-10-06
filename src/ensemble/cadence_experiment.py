"""Frozen ER5 cadence/universe comparison protocol."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum


class CadenceMode(str, Enum):
    FIXED_60S = "FIXED_60S"
    FIXED_300S = "FIXED_300S"
    HYBRID_INTERRUPT = "HYBRID_INTERRUPT"
    EXPOSURE_EVENT = "EXPOSURE_EVENT"


class Stage2Status(str, Enum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"
    RETAIN_SIMPLE = "RETAIN_SIMPLE"
    SUPERIOR = "SUPERIOR"


@dataclass(slots=True, frozen=True)
class CadencePolicy:
    mode: CadenceMode
    fixed_seconds: int | None = None
    coalesce_seconds: int | None = None
    max_idle_seconds: int | None = None


@dataclass(slots=True, frozen=True)
class Stage2Protocol:
    protocol_id: str
    representation_id: str
    frozen_at: datetime
    er4_holdout_end: datetime
    holdout_start: datetime
    holdout_end: datetime
    common_grid_seconds: int
    universes: tuple[str, ...]
    policies: tuple[CadencePolicy, ...]
    practical_equivalence: Decimal

    @property
    def protocol_hash(self) -> str:
        payload = {
            "protocol_id": self.protocol_id,
            "representation_id": self.representation_id,
            "frozen_at": self.frozen_at.isoformat(),
            "er4_holdout_end": self.er4_holdout_end.isoformat(),
            "holdout_start": self.holdout_start.isoformat(),
            "holdout_end": self.holdout_end.isoformat(),
            "common_grid_seconds": self.common_grid_seconds,
            "universes": self.universes,
            "policies": [
                {
                    "mode": p.mode.value,
                    "fixed_seconds": p.fixed_seconds,
                    "coalesce_seconds": p.coalesce_seconds,
                    "max_idle_seconds": p.max_idle_seconds,
                }
                for p in self.policies
            ],
            "practical_equivalence": str(self.practical_equivalence),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(slots=True, frozen=True)
class TimedPrediction:
    emitted_at: datetime
    available_at: datetime
    value: Decimal | None
    evidence_id: str


@dataclass(slots=True, frozen=True)
class GridPrediction:
    at: datetime
    value: Decimal | None
    evidence_id: str | None
    age_seconds: int | None


@dataclass(slots=True, frozen=True)
class Action:
    at: datetime
    target: Decimal
    turnover: Decimal
    cost: Decimal
    evidence_id: str


def default_er5_protocol(
    *,
    representation_id: str,
    frozen_at: datetime,
    er4_holdout_end: datetime,
    holdout_start: datetime,
    holdout_end: datetime,
    practical_equivalence: Decimal = Decimal("0.0001"),
) -> Stage2Protocol:
    if holdout_start <= er4_holdout_end:
        raise ValueError("ER5 holdout must be strictly after the ER4 holdout")
    if frozen_at >= holdout_start:
        raise ValueError("ER5 protocol must freeze before its holdout")
    return Stage2Protocol(
        protocol_id="er5:cadence-universe:v1",
        representation_id=representation_id,
        frozen_at=frozen_at,
        er4_holdout_end=er4_holdout_end,
        holdout_start=holdout_start,
        holdout_end=holdout_end,
        common_grid_seconds=60,
        universes=("original_50", "prospective_120"),
        policies=(
            CadencePolicy(CadenceMode.FIXED_60S, fixed_seconds=60),
            CadencePolicy(CadenceMode.FIXED_300S, fixed_seconds=300),
            CadencePolicy(
                CadenceMode.HYBRID_INTERRUPT,
                fixed_seconds=60,
                coalesce_seconds=3,
            ),
            CadencePolicy(
                CadenceMode.EXPOSURE_EVENT,
                max_idle_seconds=300,
            ),
        ),
        practical_equivalence=practical_equivalence,
    )


def common_grid_latest(
    predictions: tuple[TimedPrediction, ...],
    *,
    start: datetime,
    end: datetime,
    step: timedelta = timedelta(minutes=1),
    max_age: timedelta | None = None,
) -> tuple[GridPrediction, ...]:
    ordered = sorted(predictions, key=lambda p: (p.available_at, p.emitted_at, p.evidence_id))
    result: list[GridPrediction] = []
    at = start
    while at < end:
        visible = [p for p in ordered if p.available_at <= at]
        latest = visible[-1] if visible else None
        if latest is None:
            result.append(GridPrediction(at, None, None, None))
        else:
            age = int((at - latest.available_at).total_seconds())
            if max_age is not None and at - latest.available_at > max_age:
                result.append(GridPrediction(at, None, None, age))
            else:
                result.append(GridPrediction(at, latest.value, latest.evidence_id, age))
        at += step
    return tuple(result)


def replay_actions(
    predictions: tuple[TimedPrediction, ...],
    *,
    cost_rate: Decimal,
) -> tuple[Action, ...]:
    """Chronological target changes only; unchanged snapshots incur no trading cost."""
    current = Decimal(0)
    actions: list[Action] = []
    for prediction in sorted(
        predictions, key=lambda p: (p.available_at, p.emitted_at, p.evidence_id)
    ):
        if prediction.value is None:
            continue
        target = prediction.value
        if target == current:
            continue
        turnover = abs(target - current)
        actions.append(
            Action(
                at=prediction.available_at,
                target=target,
                turnover=turnover,
                cost=turnover * cost_rate,
                evidence_id=prediction.evidence_id,
            )
        )
        current = target
    return tuple(actions)


def assess_stage2_readiness(
    *,
    protocol: Stage2Protocol,
    representation_supported: bool,
    new_holdout_rows: int,
    outcome_mature_rows: int,
    minimum_rows: int,
) -> tuple[Stage2Status, str]:
    if not representation_supported:
        return Stage2Status.BLOCKED, "ER4_REPRESENTATION_NOT_SUPPORTED"
    if new_holdout_rows <= 0:
        return Stage2Status.BLOCKED, "NO_NEW_UNTOUCHED_HOLDOUT_ROWS"
    if outcome_mature_rows < minimum_rows:
        return Stage2Status.BLOCKED, "INSUFFICIENT_OUTCOME_MATURITY"
    return Stage2Status.READY, "NEW_UNTOUCHED_HOLDOUT_READY"
