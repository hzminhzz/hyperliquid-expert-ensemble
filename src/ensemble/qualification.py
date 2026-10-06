"""Fail-closed qualification decision for ER6."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class GateStatus(str, Enum):
    PASS = "PASS"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"


@dataclass(slots=True, frozen=True)
class QualificationGate:
    gate_id: str
    status: GateStatus
    evidence_ref: str
    reason: str


@dataclass(slots=True, frozen=True)
class QualificationDecision:
    revision: str
    engineering: GateStatus
    descriptive: GateStatus
    historical_predictive: GateStatus
    live_forward: GateStatus
    production_advisory: GateStatus
    predictive_emission_enabled: bool
    financial_execution_enabled: bool
    promoted_policy_revision: str | None
    expected_return_calibrated: bool
    next_frontier: str
    gates: tuple[QualificationGate, ...]


def decide_qualification(
    *,
    revision: str,
    er0: QualificationGate,
    er1: QualificationGate,
    er2: QualificationGate,
    er3: QualificationGate,
    er4: QualificationGate,
    er5: QualificationGate,
    live_forward: QualificationGate | None = None,
    production_approval: bool = False,
    promoted_policy_revision: str | None = None,
    expected_return_calibrated: bool = False,
) -> QualificationDecision:
    """Separate engineering/descriptive success from predictive/live/production authority."""
    gates = (er0, er1, er2, er3, er4, er5) + (
        (live_forward,) if live_forward is not None else ()
    )

    engineering = (
        GateStatus.PASS
        if all(
            gate.status in (GateStatus.PASS, GateStatus.BLOCKED)
            for gate in (er0, er1, er2, er3, er4, er5)
        )
        else GateStatus.REJECTED
    )
    descriptive = (
        GateStatus.PASS
        if er1.status is GateStatus.PASS and er3.status in (GateStatus.PASS, GateStatus.BLOCKED)
        else GateStatus.INCONCLUSIVE
    )

    if er4.status is GateStatus.PASS and er5.status is GateStatus.PASS:
        historical = GateStatus.PASS
    elif er4.status is GateStatus.REJECTED or er5.status is GateStatus.REJECTED:
        historical = GateStatus.REJECTED
    elif er4.status is GateStatus.BLOCKED or er5.status is GateStatus.BLOCKED:
        historical = GateStatus.BLOCKED
    else:
        historical = GateStatus.INCONCLUSIVE

    live_status = live_forward.status if live_forward is not None else GateStatus.NOT_RUN
    predictive_enabled = (
        historical is GateStatus.PASS
        and live_status is GateStatus.PASS
        and promoted_policy_revision is not None
    )
    production_status = (
        GateStatus.PASS
        if predictive_enabled and production_approval
        else GateStatus.NOT_RUN
    )

    if historical is GateStatus.BLOCKED:
        next_frontier = "collect prospective outcome-mature ER4 evidence"
    elif historical is not GateStatus.PASS:
        next_frontier = "resolve historical predictive qualification"
    elif live_status is not GateStatus.PASS:
        next_frontier = "run frozen V30 live-forward shadow gate"
    elif not production_approval:
        next_frontier = "seek separate V31 production-advisory approval"
    else:
        next_frontier = "monitor qualified advisory policy"

    return QualificationDecision(
        revision=revision,
        engineering=engineering,
        descriptive=descriptive,
        historical_predictive=historical,
        live_forward=live_status,
        production_advisory=production_status,
        predictive_emission_enabled=predictive_enabled,
        financial_execution_enabled=False,
        promoted_policy_revision=(
            promoted_policy_revision if predictive_enabled else None
        ),
        expected_return_calibrated=(
            expected_return_calibrated if predictive_enabled else False
        ),
        next_frontier=next_frontier,
        gates=gates,
    )
