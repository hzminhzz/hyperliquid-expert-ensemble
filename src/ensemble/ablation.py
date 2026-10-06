"""Frozen paired-ablation research protocol for V2."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path


class CandidateStatus(str, Enum):
    PROMOTE = "PROMOTE"
    REJECT = "REJECT"
    INCONCLUSIVE = "INCONCLUSIVE"


class AblationFamily(str, Enum):
    LADDER = "LADDER"
    RAW_VS_CLIPPED = "RAW_VS_CLIPPED"
    STATE_VS_FLOW = "STATE_VS_FLOW"
    STATE_PLUS_FLOW = "STATE_PLUS_FLOW"
    EQUAL_VS_CLUSTER = "EQUAL_VS_CLUSTER"
    ABSOLUTE_VS_RELATIVE = "ABSOLUTE_VS_RELATIVE"
    LATENCY_COST = "LATENCY_COST"
    EXPERT_DROPOUT = "EXPERT_DROPOUT"


@dataclass(slots=True, frozen=True)
class AblationRegistration:
    registration_id: str
    family: AblationFamily
    baseline_feature: str
    candidate_feature: str
    holdout_start: datetime
    holdout_end: datetime
    primary_metric: str
    min_samples: int
    min_delta: Decimal
    max_search_count: int
    latency_ms: int
    cost_bps: Decimal
    frozen_at: datetime

    @property
    def registration_hash(self) -> str:
        payload = {
            "registration_id": self.registration_id,
            "family": self.family.value,
            "baseline_feature": self.baseline_feature,
            "candidate_feature": self.candidate_feature,
            "holdout_start": self.holdout_start.isoformat(),
            "holdout_end": self.holdout_end.isoformat(),
            "primary_metric": self.primary_metric,
            "min_samples": self.min_samples,
            "min_delta": str(self.min_delta),
            "max_search_count": self.max_search_count,
            "latency_ms": self.latency_ms,
            "cost_bps": str(self.cost_bps),
            "frozen_at": self.frozen_at.isoformat(),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(slots=True, frozen=True)
class AblationObservation:
    source_id: str
    known_at: datetime
    forward_net_return: Decimal
    features: tuple[tuple[str, Decimal | None], ...]

    def feature(self, name: str) -> Decimal | None:
        return dict(self.features).get(name)


@dataclass(slots=True, frozen=True)
class AblationResult:
    registration_id: str
    registration_hash: str
    status: CandidateStatus
    paired_samples: int
    baseline_metric: Decimal | None
    candidate_metric: Decimal | None
    metric_delta: Decimal | None
    search_count: int
    reason: str
    evaluated_source_ids: tuple[str, ...]


def _directional_return(feature: Decimal, forward_return: Decimal) -> Decimal:
    if feature > 0:
        return forward_return
    if feature < 0:
        return -forward_return
    return Decimal(0)


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("cannot calculate mean of empty sequence")
    return sum(values, start=Decimal(0)) / Decimal(len(values))


def evaluate_ablation(
    registration: AblationRegistration,
    observations: Sequence[AblationObservation],
    *,
    search_count: int,
) -> AblationResult:
    """Evaluate one frozen candidate on paired complete-case holdout observations."""
    if registration.frozen_at > registration.holdout_start:
        raise ValueError("registration must be frozen before holdout starts")
    if registration.primary_metric != "mean_directional_return":
        raise ValueError("unsupported primary metric")

    paired = []
    for observation in observations:
        if not (registration.holdout_start <= observation.known_at < registration.holdout_end):
            continue
        baseline = observation.feature(registration.baseline_feature)
        candidate = observation.feature(registration.candidate_feature)
        if baseline is None or candidate is None:
            continue
        paired.append((observation, baseline, candidate))

    ids = tuple(observation.source_id for observation, _baseline, _candidate in paired)
    if search_count > registration.max_search_count:
        return AblationResult(
            registration_id=registration.registration_id,
            registration_hash=registration.registration_hash,
            status=CandidateStatus.REJECT,
            paired_samples=len(paired),
            baseline_metric=None,
            candidate_metric=None,
            metric_delta=None,
            search_count=search_count,
            reason="SEARCH_BUDGET_EXCEEDED",
            evaluated_source_ids=ids,
        )

    if len(paired) < registration.min_samples:
        return AblationResult(
            registration_id=registration.registration_id,
            registration_hash=registration.registration_hash,
            status=CandidateStatus.INCONCLUSIVE,
            paired_samples=len(paired),
            baseline_metric=None,
            candidate_metric=None,
            metric_delta=None,
            search_count=search_count,
            reason="INSUFFICIENT_HOLDOUT_SUPPORT",
            evaluated_source_ids=ids,
        )

    baseline_metric = _mean(
        [
            _directional_return(baseline, observation.forward_net_return)
            for observation, baseline, _candidate in paired
        ]
    )
    candidate_metric = _mean(
        [
            _directional_return(candidate, observation.forward_net_return)
            for observation, _baseline, candidate in paired
        ]
    )
    delta = candidate_metric - baseline_metric
    if delta >= registration.min_delta:
        status = CandidateStatus.PROMOTE
        reason = "PREDECLARED_DELTA_CLEARED"
    else:
        status = CandidateStatus.REJECT
        reason = "NO_INCREMENTAL_HOLDOUT_VALUE"

    return AblationResult(
        registration_id=registration.registration_id,
        registration_hash=registration.registration_hash,
        status=status,
        paired_samples=len(paired),
        baseline_metric=baseline_metric,
        candidate_metric=candidate_metric,
        metric_delta=delta,
        search_count=search_count,
        reason=reason,
        evaluated_source_ids=ids,
    )


def default_v26_registrations(
    *,
    holdout_start: datetime,
    holdout_end: datetime,
    frozen_at: datetime,
    min_samples: int,
    min_delta: Decimal,
    max_search_count: int,
    latency_ms: int,
    cost_bps: Decimal,
) -> tuple[AblationRegistration, ...]:
    """Register the mandatory V26 ladder and sensitivity families."""
    specs = (
        ("B0_to_B1", AblationFamily.RAW_VS_CLIPPED, "b0", "b1"),
        ("B1_to_B2", AblationFamily.EQUAL_VS_CLUSTER, "b1", "b2"),
        ("B2_to_B3", AblationFamily.STATE_VS_FLOW, "b2", "b3_flow"),
        ("B3_to_B4", AblationFamily.ABSOLUTE_VS_RELATIVE, "b3_flow", "b4_relative"),
        ("state_plus_flow", AblationFamily.STATE_PLUS_FLOW, "b2", "state_plus_flow"),
        ("latency_cost", AblationFamily.LATENCY_COST, "b4_relative", "b4_latency_cost"),
        ("expert_dropout", AblationFamily.EXPERT_DROPOUT, "b4_relative", "b4_dropout"),
    )
    return tuple(
        AblationRegistration(
            registration_id=registration_id,
            family=family,
            baseline_feature=baseline,
            candidate_feature=candidate,
            holdout_start=holdout_start,
            holdout_end=holdout_end,
            primary_metric="mean_directional_return",
            min_samples=min_samples,
            min_delta=min_delta,
            max_search_count=max_search_count,
            latency_ms=latency_ms,
            cost_bps=cost_bps,
            frozen_at=frozen_at,
        )
        for registration_id, family, baseline, candidate in specs
    )


def validate_required_families(
    registrations: Sequence[AblationRegistration],
) -> None:
    required = {
        AblationFamily.RAW_VS_CLIPPED,
        AblationFamily.STATE_VS_FLOW,
        AblationFamily.STATE_PLUS_FLOW,
        AblationFamily.EQUAL_VS_CLUSTER,
        AblationFamily.ABSOLUTE_VS_RELATIVE,
        AblationFamily.LATENCY_COST,
        AblationFamily.EXPERT_DROPOUT,
    }
    observed = {registration.family for registration in registrations}
    missing = required - observed
    if missing:
        raise ValueError(f"missing required ablation families: {sorted(item.value for item in missing)}")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ablation_results (
    registration_id TEXT PRIMARY KEY,
    registration_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


def _result_payload(result: AblationResult) -> str:
    payload = {
        "registration_id": result.registration_id,
        "registration_hash": result.registration_hash,
        "status": result.status.value,
        "paired_samples": result.paired_samples,
        "baseline_metric": (
            str(result.baseline_metric) if result.baseline_metric is not None else None
        ),
        "candidate_metric": (
            str(result.candidate_metric) if result.candidate_metric is not None else None
        ),
        "metric_delta": str(result.metric_delta) if result.metric_delta is not None else None,
        "search_count": result.search_count,
        "reason": result.reason,
        "evaluated_source_ids": list(result.evaluated_source_ids),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class AblationArchive:
    """Append-only archive retaining positive, negative, and inconclusive results."""

    def __init__(self, path: Path | str) -> None:
        self.conn = sqlite3.connect(Path(path))
        with self.conn:
            self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def append(self, result: AblationResult) -> bool:
        payload = _result_payload(result)
        row = self.conn.execute(
            "SELECT registration_hash, payload_json FROM ablation_results WHERE registration_id = ?",
            (result.registration_id,),
        ).fetchone()
        if row is not None:
            if row[0] != result.registration_hash or row[1] != payload:
                raise ValueError("ablation result conflict")
            return False
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO ablation_results (registration_id, registration_hash, payload_json)
                VALUES (?, ?, ?)
                """,
                (result.registration_id, result.registration_hash, payload),
            )
        return True

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM ablation_results").fetchone()
        return int(row[0]) if row is not None else 0
