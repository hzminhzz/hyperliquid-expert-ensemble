"""ER6 qualification: engineering success cannot silently become predictive authority."""

from ensemble.qualification import (
    GateStatus,
    QualificationGate,
    decide_qualification,
)


def gate(gate_id: str, status: GateStatus) -> QualificationGate:
    return QualificationGate(
        gate_id=gate_id,
        status=status,
        evidence_ref=f"evidence:{gate_id}",
        reason=status.value,
    )


def test_current_er0_er5_state_is_fail_closed_for_prediction():
    decision = decide_qualification(
        revision="er6:v1",
        er0=gate("ER0", GateStatus.BLOCKED),
        er1=gate("ER1", GateStatus.PASS),
        er2=gate("ER2", GateStatus.BLOCKED),
        er3=gate("ER3", GateStatus.BLOCKED),
        er4=gate("ER4", GateStatus.BLOCKED),
        er5=gate("ER5", GateStatus.BLOCKED),
    )
    assert decision.engineering is GateStatus.PASS
    assert decision.descriptive is GateStatus.PASS
    assert decision.historical_predictive is GateStatus.BLOCKED
    assert decision.live_forward is GateStatus.NOT_RUN
    assert decision.predictive_emission_enabled is False
    assert decision.production_advisory is GateStatus.NOT_RUN
    assert decision.financial_execution_enabled is False
    assert decision.promoted_policy_revision is None
    assert decision.expected_return_calibrated is False


def test_historical_pass_alone_still_cannot_emit_without_live_forward_gate():
    decision = decide_qualification(
        revision="er6:v2",
        er0=gate("ER0", GateStatus.PASS),
        er1=gate("ER1", GateStatus.PASS),
        er2=gate("ER2", GateStatus.PASS),
        er3=gate("ER3", GateStatus.PASS),
        er4=gate("ER4", GateStatus.PASS),
        er5=gate("ER5", GateStatus.PASS),
        promoted_policy_revision="policy:v1",
        expected_return_calibrated=True,
    )
    assert decision.historical_predictive is GateStatus.PASS
    assert decision.live_forward is GateStatus.NOT_RUN
    assert decision.predictive_emission_enabled is False
    assert decision.promoted_policy_revision is None
    assert decision.next_frontier == "run frozen V30 live-forward shadow gate"


def test_live_forward_pass_requires_separate_production_advisory_approval():
    live = gate("V30", GateStatus.PASS)
    decision = decide_qualification(
        revision="er6:v3",
        er0=gate("ER0", GateStatus.PASS),
        er1=gate("ER1", GateStatus.PASS),
        er2=gate("ER2", GateStatus.PASS),
        er3=gate("ER3", GateStatus.PASS),
        er4=gate("ER4", GateStatus.PASS),
        er5=gate("ER5", GateStatus.PASS),
        live_forward=live,
        promoted_policy_revision="policy:v1",
        expected_return_calibrated=True,
        production_approval=False,
    )
    assert decision.predictive_emission_enabled is True
    assert decision.production_advisory is GateStatus.NOT_RUN
    assert decision.financial_execution_enabled is False
    assert decision.next_frontier == "seek separate V31 production-advisory approval"


def test_upstream_failed_gate_vetoes_prediction_and_production_even_if_downstream_pass():
    for source_status in (GateStatus.BLOCKED, GateStatus.REJECTED, GateStatus.NOT_RUN):
        decision = decide_qualification(
            revision="er6:fail-closed",
            er0=gate("ER0", source_status),
            er1=gate("ER1", GateStatus.PASS),
            er2=gate("ER2", GateStatus.PASS),
            er3=gate("ER3", GateStatus.PASS),
            er4=gate("ER4", GateStatus.PASS),
            er5=gate("ER5", GateStatus.PASS),
            live_forward=gate("V30", GateStatus.PASS),
            promoted_policy_revision="policy:unqualified",
            expected_return_calibrated=True,
            production_approval=True,
        )
        assert decision.predictive_emission_enabled is False
        assert decision.production_advisory is GateStatus.NOT_RUN
        assert decision.promoted_policy_revision is None
        assert decision.expected_return_calibrated is False
        assert decision.financial_execution_enabled is False
        assert decision.next_frontier == "resolve upstream source and engineering qualification"


def test_rejected_experiment_is_preserved_as_rejection():
    decision = decide_qualification(
        revision="er6:v4",
        er0=gate("ER0", GateStatus.PASS),
        er1=gate("ER1", GateStatus.PASS),
        er2=gate("ER2", GateStatus.PASS),
        er3=gate("ER3", GateStatus.PASS),
        er4=gate("ER4", GateStatus.REJECTED),
        er5=gate("ER5", GateStatus.NOT_RUN),
    )
    assert decision.historical_predictive is GateStatus.REJECTED
    assert decision.predictive_emission_enabled is False
