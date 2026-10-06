"""Frozen ER4 representation experiment protocol."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class ExperimentStatus(str, Enum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"
    REJECTED = "REJECTED"
    PASS = "PASS"


@dataclass(slots=True, frozen=True)
class RepresentationArm:
    arm_id: str
    features: tuple[str, ...]
    requires_annotations: bool = False


@dataclass(slots=True, frozen=True)
class RepresentationProtocol:
    protocol_id: str
    frozen_at: datetime
    holdout_start: datetime
    holdout_end: datetime
    primary_horizon: str
    secondary_horizon: str
    attached_horizons: tuple[str, ...]
    primary_metric: str
    min_useful_effect: Decimal
    max_search_count: int
    wallet_universes: tuple[str, ...]
    arms: tuple[RepresentationArm, ...]
    market_control: tuple[str, ...]

    @property
    def protocol_hash(self) -> str:
        payload = {
            "protocol_id": self.protocol_id,
            "frozen_at": self.frozen_at.isoformat(),
            "holdout_start": self.holdout_start.isoformat(),
            "holdout_end": self.holdout_end.isoformat(),
            "primary_horizon": self.primary_horizon,
            "secondary_horizon": self.secondary_horizon,
            "attached_horizons": self.attached_horizons,
            "primary_metric": self.primary_metric,
            "min_useful_effect": str(self.min_useful_effect),
            "max_search_count": self.max_search_count,
            "wallet_universes": self.wallet_universes,
            "arms": [
                {
                    "arm_id": arm.arm_id,
                    "features": arm.features,
                    "requires_annotations": arm.requires_annotations,
                }
                for arm in self.arms
            ],
            "market_control": self.market_control,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(slots=True, frozen=True)
class ExperimentReadiness:
    status: ExperimentStatus
    reason: str
    available_rows: int
    mature_rows: int
    annotation_available: bool
    runnable_arms: tuple[str, ...]


def default_er4_protocol(
    *,
    frozen_at: datetime,
    holdout_start: datetime,
    holdout_end: datetime,
    min_useful_effect: Decimal = Decimal("0.0001"),
    max_search_count: int = 3,
) -> RepresentationProtocol:
    if frozen_at >= holdout_start:
        raise ValueError("protocol must freeze before holdout starts")
    return RepresentationProtocol(
        protocol_id="er4:r0-r1-r2:v1",
        frozen_at=frozen_at,
        holdout_start=holdout_start,
        holdout_end=holdout_end,
        primary_horizon="15m",
        secondary_horizon="1h",
        attached_horizons=("1m", "5m", "15m", "1h", "4h", "24h"),
        primary_metric="heldout_squared_error_improvement",
        min_useful_effect=min_useful_effect,
        max_search_count=max_search_count,
        wallet_universes=("original_50", "prospective_120"),
        arms=(
            RepresentationArm("R0", ("state",)),
            RepresentationArm(
                "R1",
                (
                    "state",
                    "net_flow",
                    "gross_flow",
                    "recency",
                    "build_rate",
                ),
            ),
            RepresentationArm(
                "R2",
                (
                    "state",
                    "net_flow",
                    "gross_flow",
                    "recency",
                    "build_rate",
                    "segment_elapsed",
                    "pause_state",
                    "execution_label",
                    "supported_taker_fraction",
                ),
                requires_annotations=True,
            ),
        ),
        market_control=("lagged_return", "realized_volatility"),
    )


def assess_readiness(
    protocol: RepresentationProtocol,
    *,
    available_rows: int,
    mature_rows: int,
    annotation_available: bool,
    minimum_mature_rows: int,
) -> ExperimentReadiness:
    runnable = tuple(
        arm.arm_id
        for arm in protocol.arms
        if not arm.requires_annotations or annotation_available
    )
    if available_rows <= 0:
        return ExperimentReadiness(
            ExperimentStatus.BLOCKED,
            "NO_RECORDED_EVIDENCE_ROWS",
            available_rows,
            mature_rows,
            annotation_available,
            runnable,
        )
    if mature_rows < minimum_mature_rows:
        return ExperimentReadiness(
            ExperimentStatus.BLOCKED,
            "INSUFFICIENT_OUTCOME_MATURITY",
            available_rows,
            mature_rows,
            annotation_available,
            runnable,
        )
    return ExperimentReadiness(
        ExperimentStatus.READY,
        "OUTCOME_MATURE_INPUTS_AVAILABLE",
        available_rows,
        mature_rows,
        annotation_available,
        runnable,
    )
