"""Deterministic account-independent TradeSignal policy for V2."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class SignalState(str, Enum):
    FLAT = "FLAT"
    LONG = "LONG"
    SHORT = "SHORT"


class SignalEvent(str, Enum):
    ENTER = "ENTER"
    INCREASE = "INCREASE"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    REVERSE = "REVERSE"
    NONE = "NONE"


@dataclass(slots=True, frozen=True)
class ConfidenceComponents:
    reliability_mass: Decimal
    independent_breadth: Decimal
    sample_support: int
    calibration_support: int | None


@dataclass(slots=True, frozen=True)
class PredictiveEvidence:
    evidence_id: str
    coin: str
    horizon: str
    signal_strength: Decimal
    confidence: ConfidenceComponents
    promoted_features: tuple[str, ...]
    baseline_comparators: tuple[tuple[str, Decimal], ...]
    supporting_clusters: tuple[str, ...]
    opposing_clusters: tuple[str, ...]
    missing_information: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    feature_revision: str
    model_revision: str
    universe_revision: str
    as_of: datetime
    knowledge_time: datetime
    crowding_risk: Decimal | None = None
    expected_return: Decimal | None = None
    calibration_revision: str | None = None


@dataclass(slots=True, frozen=True)
class SignalPolicy:
    policy_revision: str
    model_revision: str
    promoted_features: tuple[str, ...]
    entry_threshold: Decimal
    exit_threshold: Decimal
    change_threshold: Decimal
    enabled: bool


@dataclass(slots=True, frozen=True)
class TradeSignal:
    signal_id: str
    evidence_id: str
    coin: str
    horizon: str
    state: SignalState
    event: SignalEvent
    signal_strength: Decimal
    confidence: ConfidenceComponents
    crowding_risk: Decimal | None
    expected_return: Decimal | None
    supporting_clusters: tuple[str, ...]
    opposing_clusters: tuple[str, ...]
    missing_information: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    baseline_comparators: tuple[tuple[str, Decimal], ...]
    evidence_refs: tuple[str, ...]
    feature_revision: str
    model_revision: str
    policy_revision: str
    universe_revision: str
    blocker_code: str | None
    as_of: datetime
    knowledge_time: datetime


def _signal_id(evidence: PredictiveEvidence, policy: SignalPolicy) -> str:
    payload = {
        "evidence_id": evidence.evidence_id,
        "policy_revision": policy.policy_revision,
        "model_revision": policy.model_revision,
        "knowledge_time": evidence.knowledge_time.isoformat(),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"signal:{digest[:20]}"


def _blocked_signal(
    evidence: PredictiveEvidence,
    policy: SignalPolicy,
    blocker: str,
) -> TradeSignal:
    return TradeSignal(
        signal_id=_signal_id(evidence, policy),
        evidence_id=evidence.evidence_id,
        coin=evidence.coin,
        horizon=evidence.horizon,
        state=SignalState.FLAT,
        event=SignalEvent.NONE,
        signal_strength=evidence.signal_strength,
        confidence=evidence.confidence,
        crowding_risk=evidence.crowding_risk,
        expected_return=evidence.expected_return,
        supporting_clusters=evidence.supporting_clusters,
        opposing_clusters=evidence.opposing_clusters,
        missing_information=evidence.missing_information,
        invalidation_conditions=evidence.invalidation_conditions,
        baseline_comparators=evidence.baseline_comparators,
        evidence_refs=evidence.evidence_refs,
        feature_revision=evidence.feature_revision,
        model_revision=policy.model_revision,
        policy_revision=policy.policy_revision,
        universe_revision=evidence.universe_revision,
        blocker_code=blocker,
        as_of=evidence.as_of,
        knowledge_time=evidence.knowledge_time,
    )


def _desired_state(
    strength: Decimal,
    prior_state: SignalState,
    policy: SignalPolicy,
) -> SignalState:
    if prior_state is SignalState.FLAT:
        if strength >= policy.entry_threshold:
            return SignalState.LONG
        if strength <= -policy.entry_threshold:
            return SignalState.SHORT
        return SignalState.FLAT

    if prior_state is SignalState.LONG:
        if strength <= -policy.entry_threshold:
            return SignalState.SHORT
        if strength < policy.exit_threshold:
            return SignalState.FLAT
        return SignalState.LONG

    if strength >= policy.entry_threshold:
        return SignalState.LONG
    if strength > -policy.exit_threshold:
        return SignalState.FLAT
    return SignalState.SHORT


def _same_direction_event(
    *,
    state: SignalState,
    strength: Decimal,
    prior_strength: Decimal,
    change_threshold: Decimal,
) -> SignalEvent:
    if state is SignalState.FLAT:
        return SignalEvent.NONE
    current_magnitude = abs(strength)
    prior_magnitude = abs(prior_strength)
    if current_magnitude - prior_magnitude >= change_threshold:
        return SignalEvent.INCREASE
    if prior_magnitude - current_magnitude >= change_threshold:
        return SignalEvent.REDUCE
    return SignalEvent.NONE


def apply_signal_policy(
    evidence: PredictiveEvidence,
    policy: SignalPolicy,
    *,
    prior_signal: TradeSignal | None = None,
) -> TradeSignal:
    """Apply a versioned hysteretic state machine to promoted predictive evidence."""
    if evidence.expected_return is not None and evidence.calibration_revision is None:
        raise ValueError("expected_return requires a calibration_revision")
    if policy.exit_threshold < 0 or policy.entry_threshold <= policy.exit_threshold:
        raise ValueError("policy thresholds must satisfy 0 <= exit < entry")
    if policy.change_threshold < 0:
        raise ValueError("change_threshold must be non-negative")

    if not policy.enabled or not policy.promoted_features:
        return _blocked_signal(evidence, policy, "NO_PROMOTED_PREDICTIVE_EVIDENCE")

    required = set(policy.promoted_features)
    provided = set(evidence.promoted_features)
    if not required.issubset(provided):
        return _blocked_signal(evidence, policy, "PREDICTIVE_EVIDENCE_INCOMPLETE")

    prior_state = prior_signal.state if prior_signal is not None else SignalState.FLAT
    prior_strength = prior_signal.signal_strength if prior_signal is not None else Decimal(0)
    state = _desired_state(evidence.signal_strength, prior_state, policy)

    if state is not prior_state:
        if prior_state is SignalState.FLAT:
            event = SignalEvent.ENTER
        elif state is SignalState.FLAT:
            event = SignalEvent.EXIT
        else:
            event = SignalEvent.REVERSE
    else:
        event = _same_direction_event(
            state=state,
            strength=evidence.signal_strength,
            prior_strength=prior_strength,
            change_threshold=policy.change_threshold,
        )

    return TradeSignal(
        signal_id=_signal_id(evidence, policy),
        evidence_id=evidence.evidence_id,
        coin=evidence.coin,
        horizon=evidence.horizon,
        state=state,
        event=event,
        signal_strength=evidence.signal_strength,
        confidence=evidence.confidence,
        crowding_risk=evidence.crowding_risk,
        expected_return=evidence.expected_return,
        supporting_clusters=evidence.supporting_clusters,
        opposing_clusters=evidence.opposing_clusters,
        missing_information=evidence.missing_information,
        invalidation_conditions=evidence.invalidation_conditions,
        baseline_comparators=evidence.baseline_comparators,
        evidence_refs=evidence.evidence_refs,
        feature_revision=evidence.feature_revision,
        model_revision=policy.model_revision,
        policy_revision=policy.policy_revision,
        universe_revision=evidence.universe_revision,
        blocker_code=None,
        as_of=evidence.as_of,
        knowledge_time=evidence.knowledge_time,
    )
