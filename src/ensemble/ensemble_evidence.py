"""Canonical descriptive EnsembleEvidence for V2."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .conviction import ConvictionStatus, RelativeConviction
from .independence import IndependenceArtifact, IndependenceCluster
from .posture import EligibilityState
from .wallet_evidence import WalletEvidence


@dataclass(slots=True, frozen=True)
class HorizonEvidence:
    horizon: str
    observed: Decimal
    missing_mass: Decimal


@dataclass(slots=True, frozen=True)
class ClusterContribution:
    cluster_id: str
    members: tuple[str, ...]
    status: str
    budget: Decimal
    state_contribution: Decimal
    state_missing_mass: Decimal
    flow: tuple[HorizonEvidence, ...]


@dataclass(slots=True, frozen=True)
class RelativeConvictionSummary:
    observed: Decimal
    missing_mass: Decimal
    valid_experts: int
    unknown_experts: int


@dataclass(slots=True, frozen=True)
class BaselineComparators:
    b0_bounded_equal_wallet: Decimal
    b0_missing_mass: Decimal
    b1_raw_equal_wallet: Decimal
    b1_missing_mass: Decimal
    b2_independent_state: Decimal
    b2_missing_mass: Decimal


@dataclass(slots=True, frozen=True)
class EvidenceSupport:
    reliability_mass: Decimal
    independent_breadth: Decimal
    relative_conviction_mass: Decimal
    unknown_similarity_experts: int


@dataclass(slots=True, frozen=True)
class EnsembleEvidence:
    evidence_id: str
    coin: str
    state_evidence: Decimal
    state_missing_mass: Decimal
    flow_evidence: tuple[HorizonEvidence, ...]
    relative_conviction: RelativeConvictionSummary
    independent_breadth: Decimal
    cluster_contributions: tuple[ClusterContribution, ...]
    supporting_clusters: tuple[str, ...]
    opposing_clusters: tuple[str, ...]
    missing_information: tuple[str, ...]
    support: EvidenceSupport
    baseline_comparators: BaselineComparators
    wallet_revisions: tuple[tuple[str, int], ...]
    independence_revision: str
    conviction_revisions: tuple[tuple[str, str], ...]
    feature_revision: str
    universe_revision: str
    skill_divergence: Decimal | None
    market_divergence: Decimal | None
    crowding_risk: Decimal | None
    as_of: datetime
    knowledge_time: datetime


def _is_voting(wallet: WalletEvidence) -> bool:
    return wallet.state in (EligibilityState.ELIGIBLE, EligibilityState.KNOWN_FLAT)


def _horizons(wallets: Sequence[WalletEvidence]) -> tuple[str, ...]:
    preferred = ("1m", "5m", "15m", "1h", "4h", "24h")
    observed = {flow.horizon for wallet in wallets for flow in wallet.intent_flow}
    ordered = [horizon for horizon in preferred if horizon in observed]
    ordered.extend(sorted(observed.difference(ordered)))
    return tuple(ordered)


def _flow_map(wallet: WalletEvidence) -> dict[str, Decimal]:
    return {value.horizon: value.delta_bias for value in wallet.intent_flow}


def _baseline_equal_wallet(
    wallets: Sequence[WalletEvidence],
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if not wallets:
        return Decimal(0), Decimal(0), Decimal(0), Decimal(0)

    weight = Decimal(1) / Decimal(len(wallets))
    bounded = Decimal(0)
    raw = Decimal(0)
    bounded_missing = Decimal(0)
    raw_missing = Decimal(0)

    for wallet in wallets:
        if wallet.state is EligibilityState.UNAVAILABLE:
            bounded_missing += weight
            raw_missing += weight
            continue
        if not _is_voting(wallet):
            continue
        if wallet.bounded_influence is None:
            bounded_missing += weight
        else:
            bounded += weight * wallet.bounded_influence
        if wallet.raw_bias is None:
            raw_missing += weight
        else:
            raw += weight * wallet.raw_bias

    return bounded, bounded_missing, raw, raw_missing


def _cluster_contribution(
    cluster: IndependenceCluster,
    wallets_by_id: Mapping[str, WalletEvidence],
    horizons: Sequence[str],
) -> ClusterContribution:
    if not cluster.members:
        return ClusterContribution(
            cluster_id=cluster.cluster_id,
            members=cluster.members,
            status=cluster.status,
            budget=cluster.budget,
            state_contribution=Decimal(0),
            state_missing_mass=cluster.budget,
            flow=tuple(
                HorizonEvidence(horizon=h, observed=Decimal(0), missing_mass=cluster.budget)
                for h in horizons
            ),
        )

    member_weight = cluster.budget / Decimal(len(cluster.members))
    state = Decimal(0)
    state_missing = Decimal(0)
    flow_sums = {horizon: Decimal(0) for horizon in horizons}
    flow_missing = {horizon: Decimal(0) for horizon in horizons}

    for expert_id in cluster.members:
        wallet = wallets_by_id.get(expert_id)
        if wallet is None or not _is_voting(wallet):
            state_missing += member_weight
            for horizon in horizons:
                flow_missing[horizon] += member_weight
            continue

        if wallet.bounded_influence is None:
            state_missing += member_weight
        else:
            state += member_weight * wallet.bounded_influence

        wallet_flow = _flow_map(wallet)
        for horizon in horizons:
            if horizon not in wallet_flow:
                flow_missing[horizon] += member_weight
            else:
                flow_sums[horizon] += member_weight * wallet_flow[horizon]

    return ClusterContribution(
        cluster_id=cluster.cluster_id,
        members=cluster.members,
        status=cluster.status,
        budget=cluster.budget,
        state_contribution=state,
        state_missing_mass=state_missing,
        flow=tuple(
            HorizonEvidence(
                horizon=horizon,
                observed=flow_sums[horizon],
                missing_mass=flow_missing[horizon],
            )
            for horizon in horizons
        ),
    )


def build_ensemble_evidence(
    *,
    evidence_id: str,
    coin: str,
    wallets: Sequence[WalletEvidence],
    independence: IndependenceArtifact,
    convictions: Mapping[str, RelativeConviction],
    feature_revision: str,
    universe_revision: str,
    as_of: datetime,
    knowledge_time: datetime,
    skill_divergence: Decimal | None = None,
    market_divergence: Decimal | None = None,
    crowding_risk: Decimal | None = None,
) -> EnsembleEvidence:
    """Assemble descriptive ensemble evidence at one coherent knowledge cut."""
    for wallet in wallets:
        if wallet.coin != coin:
            raise ValueError("wallet evidence coin does not match ensemble coin")
        if wallet.knowledge_time > knowledge_time:
            raise ValueError("wallet evidence is newer than ensemble knowledge_time")
    if independence.effective_from > knowledge_time:
        raise ValueError("independence artifact is not effective at knowledge_time")

    wallets_by_id = {wallet.expert_id: wallet for wallet in wallets}
    horizons = _horizons(wallets)
    cluster_contributions = tuple(
        _cluster_contribution(cluster, wallets_by_id, horizons)
        for cluster in independence.clusters
    )

    state_evidence = sum(
        (cluster.state_contribution for cluster in cluster_contributions),
        start=Decimal(0),
    )
    state_missing = sum(
        (cluster.state_missing_mass for cluster in cluster_contributions),
        start=Decimal(0),
    )

    flows = tuple(
        HorizonEvidence(
            horizon=horizon,
            observed=sum(
                (
                    next(item for item in cluster.flow if item.horizon == horizon).observed
                    for cluster in cluster_contributions
                ),
                start=Decimal(0),
            ),
            missing_mass=sum(
                (
                    next(item for item in cluster.flow if item.horizon == horizon).missing_mass
                    for cluster in cluster_contributions
                ),
                start=Decimal(0),
            ),
        )
        for horizon in horizons
    )

    conviction_observed = Decimal(0)
    conviction_missing = Decimal(0)
    valid_convictions = 0
    unknown_convictions = 0
    conviction_revisions: list[tuple[str, str]] = []
    missing: list[str] = []

    for cluster in independence.clusters:
        if not cluster.members:
            continue
        member_weight = cluster.budget / Decimal(len(cluster.members))
        for expert_id in cluster.members:
            wallet = wallets_by_id.get(expert_id)
            if wallet is None:
                missing.append(f"wallet:{expert_id}:MISSING")
            elif wallet.missing_reasons:
                missing.extend(f"wallet:{expert_id}:{reason}" for reason in wallet.missing_reasons)

            score = convictions.get(expert_id)
            if score is None or score.status is not ConvictionStatus.VALID:
                conviction_missing += member_weight
                unknown_convictions += 1
                reason = score.status.value if score is not None else "MISSING"
                missing.append(f"conviction:{expert_id}:{reason}")
                continue
            if score.signed_percentile is None:
                conviction_missing += member_weight
                unknown_convictions += 1
                missing.append(f"conviction:{expert_id}:MISSING_SCORE")
                continue
            conviction_observed += member_weight * score.signed_percentile
            valid_convictions += 1
            conviction_revisions.append((expert_id, score.artifact_revision))

    b0, b0_missing, b1, b1_missing = _baseline_equal_wallet(wallets)
    b2 = state_evidence
    b2_missing = state_missing

    supporting = tuple(
        cluster.cluster_id
        for cluster in cluster_contributions
        if cluster.state_contribution > 0
    )
    opposing = tuple(
        cluster.cluster_id
        for cluster in cluster_contributions
        if cluster.state_contribution < 0
    )

    for expert_id in independence.unknown_experts:
        missing.append(f"similarity:{expert_id}:UNKNOWN")

    support = EvidenceSupport(
        reliability_mass=max(Decimal(0), Decimal(1) - state_missing),
        independent_breadth=independence.effective_breadth,
        relative_conviction_mass=max(Decimal(0), Decimal(1) - conviction_missing),
        unknown_similarity_experts=len(independence.unknown_experts),
    )

    return EnsembleEvidence(
        evidence_id=evidence_id,
        coin=coin,
        state_evidence=state_evidence,
        state_missing_mass=state_missing,
        flow_evidence=flows,
        relative_conviction=RelativeConvictionSummary(
            observed=conviction_observed,
            missing_mass=conviction_missing,
            valid_experts=valid_convictions,
            unknown_experts=unknown_convictions,
        ),
        independent_breadth=independence.effective_breadth,
        cluster_contributions=cluster_contributions,
        supporting_clusters=supporting,
        opposing_clusters=opposing,
        missing_information=tuple(sorted(set(missing))),
        support=support,
        baseline_comparators=BaselineComparators(
            b0_bounded_equal_wallet=b0,
            b0_missing_mass=b0_missing,
            b1_raw_equal_wallet=b1,
            b1_missing_mass=b1_missing,
            b2_independent_state=b2,
            b2_missing_mass=b2_missing,
        ),
        wallet_revisions=tuple(
            sorted((wallet.expert_id, wallet.input_revision) for wallet in wallets)
        ),
        independence_revision=independence.revision,
        conviction_revisions=tuple(sorted(set(conviction_revisions))),
        feature_revision=feature_revision,
        universe_revision=universe_revision,
        skill_divergence=skill_divergence,
        market_divergence=market_divergence,
        crowding_risk=crowding_risk,
        as_of=as_of,
        knowledge_time=knowledge_time,
    )


def explain_ensemble_evidence(evidence: EnsembleEvidence) -> dict[str, Any]:
    """Return a bounded machine-readable explanation without recomputation."""
    return {
        "evidence_id": evidence.evidence_id,
        "coin": evidence.coin,
        "state": {
            "observed": str(evidence.state_evidence),
            "missing_mass": str(evidence.state_missing_mass),
        },
        "flow": [
            {
                "horizon": value.horizon,
                "observed": str(value.observed),
                "missing_mass": str(value.missing_mass),
            }
            for value in evidence.flow_evidence
        ],
        "relative_conviction": {
            "observed": str(evidence.relative_conviction.observed),
            "missing_mass": str(evidence.relative_conviction.missing_mass),
            "valid_experts": evidence.relative_conviction.valid_experts,
            "unknown_experts": evidence.relative_conviction.unknown_experts,
        },
        "independent_breadth": str(evidence.independent_breadth),
        "supporting_clusters": list(evidence.supporting_clusters),
        "opposing_clusters": list(evidence.opposing_clusters),
        "missing_information": list(evidence.missing_information),
        "support": {
            "reliability_mass": str(evidence.support.reliability_mass),
            "relative_conviction_mass": str(evidence.support.relative_conviction_mass),
            "unknown_similarity_experts": evidence.support.unknown_similarity_experts,
        },
        "baselines": {
            "b0_bounded_equal_wallet": str(
                evidence.baseline_comparators.b0_bounded_equal_wallet
            ),
            "b1_raw_equal_wallet": str(evidence.baseline_comparators.b1_raw_equal_wallet),
            "b2_independent_state": str(evidence.baseline_comparators.b2_independent_state),
        },
        "extensions": {
            "skill_divergence": (
                str(evidence.skill_divergence)
                if evidence.skill_divergence is not None
                else None
            ),
            "market_divergence": (
                str(evidence.market_divergence)
                if evidence.market_divergence is not None
                else None
            ),
            "crowding_risk": (
                str(evidence.crowding_risk) if evidence.crowding_risk is not None else None
            ),
        },
        "revisions": {
            "feature": evidence.feature_revision,
            "universe": evidence.universe_revision,
            "independence": evidence.independence_revision,
            "wallets": dict(evidence.wallet_revisions),
            "conviction": dict(evidence.conviction_revisions),
        },
        "as_of": evidence.as_of.isoformat(),
        "knowledge_time": evidence.knowledge_time.isoformat(),
    }
