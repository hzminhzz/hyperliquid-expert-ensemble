"""V24 qualification: canonical EnsembleEvidence (Q11 + agent explanation)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ensemble.conviction import ConvictionStatus, RelativeConviction
from ensemble.ensemble_evidence import build_ensemble_evidence, explain_ensemble_evidence
from ensemble.independence import (
    IndependenceArtifact,
    IndependenceCluster,
)
from ensemble.wallet_evidence import EvidenceCause, build_wallet_evidence

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def wallet(expert: str, qty: str, *, revision: int, equity: str | None = "100"):
    return build_wallet_evidence(
        expert_id=expert,
        coin="BTC",
        quantity=Decimal(qty),
        valuation_price=Decimal(100),
        equity=None if equity is None else Decimal(equity),
        observation_time=T0,
        as_of=T0,
        knowledge_time=T0,
        input_revision=revision,
        current_cause=EvidenceCause.ECONOMIC_CHANGE,
    )


def conviction(expert: str, signed: str | None, revision: str = "conv:v1"):
    return RelativeConviction(
        expert_id=expert,
        coin="BTC",
        raw_bias=None if signed is None else Decimal(signed),
        magnitude_percentile=None if signed is None else abs(Decimal(signed)),
        signed_percentile=None if signed is None else Decimal(signed),
        status=(
            ConvictionStatus.BIAS_UNAVAILABLE if signed is None else ConvictionStatus.VALID
        ),
        support=30,
        artifact_revision=revision,
    )


def artifact(*, unknown: tuple[str, ...] = ()) -> IndependenceArtifact:
    return IndependenceArtifact(
        revision="ind:v2",
        feature_schema_version="intent-state:v1",
        training_cutoff=T0 - timedelta(days=1),
        effective_from=T0 - timedelta(hours=1),
        update_schedule="weekly",
        threshold=Decimal("0.20"),
        clusters=(
            IndependenceCluster(
                cluster_id="cluster:A,A2",
                members=("A", "A2"),
                budget=Decimal("0.5"),
                status="INFERRED",
            ),
            IndependenceCluster(
                cluster_id="cluster:B",
                members=("B",),
                budget=Decimal("0.5"),
                status="INFERRED",
            ),
        ),
        unknown_experts=unknown,
        effective_breadth=Decimal(2),
        pair_metrics=(),
    )


def test_q11_clone_adjusted_state_preserves_simpler_baselines():
    evidence = build_ensemble_evidence(
        evidence_id="ens:1",
        coin="BTC",
        wallets=[
            wallet("A", "1", revision=1),
            wallet("A2", "1", revision=2),
            wallet("B", "-1", revision=3),
        ],
        independence=artifact(),
        convictions={
            "A": conviction("A", "0.9"),
            "A2": conviction("A2", "0.8"),
            "B": conviction("B", "-0.7"),
        },
        feature_revision="features:v24",
        universe_revision="universe:1",
        as_of=T0,
        knowledge_time=T0,
    )

    assert evidence.baseline_comparators.b0_bounded_equal_wallet == Decimal(1) / Decimal(3)
    assert evidence.baseline_comparators.b1_raw_equal_wallet == Decimal(1) / Decimal(3)
    assert evidence.state_evidence == Decimal(0)
    assert evidence.baseline_comparators.b2_independent_state == Decimal(0)
    assert evidence.independent_breadth == Decimal(2)
    assert evidence.supporting_clusters == ("cluster:A,A2",)
    assert evidence.opposing_clusters == ("cluster:B",)


def test_q11_missing_cluster_mass_is_not_reallocated():
    evidence = build_ensemble_evidence(
        evidence_id="ens:missing",
        coin="BTC",
        wallets=[
            wallet("A", "1", revision=1),
            wallet("A2", "1", revision=2),
            wallet("B", "-1", revision=3, equity=None),
        ],
        independence=artifact(),
        convictions={
            "A": conviction("A", "0.9"),
            "A2": conviction("A2", "0.8"),
            "B": conviction("B", None),
        },
        feature_revision="features:v24",
        universe_revision="universe:1",
        as_of=T0,
        knowledge_time=T0,
    )

    assert evidence.state_evidence == Decimal("0.5")
    assert evidence.state_missing_mass == Decimal("0.5")
    assert evidence.support.reliability_mass == Decimal("0.5")
    assert any(item.startswith("wallet:B:") for item in evidence.missing_information)


def test_q11_flow_and_state_remain_separate_dimensions():
    from ensemble.wallet_evidence import derive_intent_event

    event = derive_intent_event(
        event_id="reduce",
        expert_id="A",
        coin="BTC",
        quantity_before=Decimal(2),
        quantity_after=Decimal(1),
        valuation_price=Decimal(100),
        equity=Decimal(100),
        event_time=T0 - timedelta(seconds=30),
        known_at=T0 - timedelta(seconds=30),
    )
    assert event is not None
    a = build_wallet_evidence(
        expert_id="A",
        coin="BTC",
        quantity=Decimal(1),
        valuation_price=Decimal(100),
        equity=Decimal(100),
        observation_time=T0,
        as_of=T0,
        knowledge_time=T0,
        input_revision=1,
        current_cause=EvidenceCause.ECONOMIC_CHANGE,
        intent_events=[event],
    )

    evidence = build_ensemble_evidence(
        evidence_id="ens:flow",
        coin="BTC",
        wallets=[a, wallet("A2", "1", revision=2), wallet("B", "-1", revision=3)],
        independence=artifact(),
        convictions={
            "A": conviction("A", "0.9"),
            "A2": conviction("A2", "0.8"),
            "B": conviction("B", "-0.7"),
        },
        feature_revision="features:v24",
        universe_revision="universe:1",
        as_of=T0,
        knowledge_time=T0,
    )

    flow_1m = next(value for value in evidence.flow_evidence if value.horizon == "1m")
    assert evidence.state_evidence == Decimal(0)
    assert flow_1m.observed == Decimal("-0.25")


def test_agent_explanation_contains_lineage_support_and_baselines():
    evidence = build_ensemble_evidence(
        evidence_id="ens:explain",
        coin="BTC",
        wallets=[
            wallet("A", "1", revision=11),
            wallet("A2", "1", revision=12),
            wallet("B", "-1", revision=13),
        ],
        independence=artifact(unknown=("A2",)),
        convictions={
            "A": conviction("A", "0.9", "conv:A"),
            "A2": conviction("A2", None, "conv:A2"),
            "B": conviction("B", "-0.7", "conv:B"),
        },
        feature_revision="features:v24",
        universe_revision="universe:7",
        as_of=T0,
        knowledge_time=T0,
    )
    explanation = explain_ensemble_evidence(evidence)

    assert explanation["coin"] == "BTC"
    assert explanation["revisions"]["feature"] == "features:v24"
    assert explanation["revisions"]["universe"] == "universe:7"
    assert explanation["revisions"]["wallets"]["A"] == 11
    assert explanation["baselines"]["b0_bounded_equal_wallet"] != explanation["baselines"][
        "b2_independent_state"
    ]
    assert explanation["support"]["unknown_similarity_experts"] == 1
    assert explanation["extensions"]["crowding_risk"] is None


def test_future_wallet_or_not_yet_effective_independence_is_rejected():
    future_wallet = build_wallet_evidence(
        expert_id="A",
        coin="BTC",
        quantity=Decimal(1),
        valuation_price=Decimal(100),
        equity=Decimal(100),
        observation_time=T0,
        as_of=T0 + timedelta(minutes=1),
        knowledge_time=T0 + timedelta(minutes=1),
        input_revision=1,
        current_cause=EvidenceCause.ECONOMIC_CHANGE,
    )

    import pytest

    with pytest.raises(ValueError, match="newer than ensemble"):
        build_ensemble_evidence(
            evidence_id="ens:future",
            coin="BTC",
            wallets=[future_wallet],
            independence=artifact(),
            convictions={},
            feature_revision="features:v24",
            universe_revision="universe:1",
            as_of=T0,
            knowledge_time=T0,
        )

    not_effective = artifact()
    not_effective = IndependenceArtifact(
        revision=not_effective.revision,
        feature_schema_version=not_effective.feature_schema_version,
        training_cutoff=not_effective.training_cutoff,
        effective_from=T0 + timedelta(minutes=1),
        update_schedule=not_effective.update_schedule,
        threshold=not_effective.threshold,
        clusters=not_effective.clusters,
        unknown_experts=not_effective.unknown_experts,
        effective_breadth=not_effective.effective_breadth,
        pair_metrics=not_effective.pair_metrics,
    )
    with pytest.raises(ValueError, match="not effective"):
        build_ensemble_evidence(
            evidence_id="ens:not-effective",
            coin="BTC",
            wallets=[wallet("A", "1", revision=1)],
            independence=not_effective,
            convictions={},
            feature_revision="features:v24",
            universe_revision="universe:1",
            as_of=T0,
            knowledge_time=T0,
        )
