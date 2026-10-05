"""Read-only explanation handlers (Contracts O3, R3, Q10).

Translates consensus targets and blockers into structured, causal accounting explanations.
"""

from __future__ import annotations

from typing import Any

from .consensus import ConsensusTarget

KNOWN_BLOCKERS = {
    "EQUITY_STALE": {
        "title": "Account Equity Stale",
        "description": "Account equity observation exceeds freshness threshold (>15m).",
        "remediation": "Refresh clearinghouseState equity observation from venue.",
    },
    "STATE_UNSEEDED": {
        "title": "Unseeded Observation State",
        "description": "Expert has not completed on-chain startup seeding.",
        "remediation": "Wait for background startup seed or trigger manual seed sweep.",
    },
    "STATE_QUARANTINED": {
        "title": "Quarantined State",
        "description": "Snapshot payload failed validation or returned malformed data.",
        "remediation": "Investigate source API error and re-reconcile account.",
    },
    "AMBIGUOUS_BOUNDS": {
        "title": "Ambiguous Missing-Mass Bounds",
        "description": "Missing expert mass creates uncertainty interval spanning zero.",
        "remediation": "Observe until missing experts resolve before taking action.",
    },
    "INSUFFICIENT_COVERAGE": {
        "title": "Insufficient Universe Coverage",
        "description": "More than 50% of expert weight mass is currently unavailable.",
        "remediation": "Restore feed continuity to restore consensus authority.",
    },
}


def explain_target(
    target: ConsensusTarget,
    prior_target: ConsensusTarget | None = None,
) -> dict[str, Any]:
    """Produce a structured causal explanation of a consensus target."""
    delta = None
    if prior_target:
        delta = str(target.observed_target - prior_target.observed_target)

    explanation = {
        "coin": target.coin,
        "as_of": target.as_of.isoformat(),
        "observed_target": str(target.observed_target),
        "posture_interval": {
            "lower_bound": str(target.lower_bound),
            "upper_bound": str(target.upper_bound),
            "missing_mass": str(target.missing_mass),
        },
        "is_actionable": target.is_actionable,
        "blocker_code": target.blocker_code,
        "cause_category": target.cause.value,
        "change_from_prior": {
            "prior_target": str(prior_target.observed_target) if prior_target else None,
            "delta": delta,
            "cause": target.cause.value if prior_target else "INITIAL_TARGET",
        },
        "contributions": [
            {
                "expert_id": c.expert_id,
                "raw_posture": str(c.raw_posture),
                "weight": str(c.weight),
                "contribution": str(c.weighted_contribution),
                "state": c.state.value,
                "reason": c.reason,
            }
            for c in target.contributions
        ],
    }

    return explanation


def explain_blocker(code: str) -> dict[str, Any]:
    """Return root-cause explanation and remediation advice for a blocker code."""
    info = KNOWN_BLOCKERS.get(code)
    if not info:
        return {
            "code": code,
            "status": "UNKNOWN_BLOCKER",
            "title": "Unrecognized Blocker Code",
            "description": f"No registered documentation for blocker '{code}'.",
            "remediation": "Inspect system logs and source stream health.",
        }

    return {
        "code": code,
        "status": "EXPLAINED",
        "title": info["title"],
        "description": info["description"],
        "remediation": info["remediation"],
    }
