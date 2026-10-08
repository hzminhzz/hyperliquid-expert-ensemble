"""Advisory message qualification: compact, cluster-adjusted, and fail-closed."""

from datetime import UTC, datetime
from decimal import Decimal

from ensemble.consensus import compute_hierarchical_cluster_consensus
from ensemble.posture import compute_posture
from ensemble.runtime import (
    TELEGRAM_SAFE_MAX_CHARS,
    _assess_target,
    _format_target,
)

NOW = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
COIN = "hyperliquid:mainnet:perp:default:BTC"


def posture(expert: str, exposure: str, *, equity: Decimal | None = Decimal(100)):
    qty = Decimal(exposure)
    return compute_posture(
        expert_id=expert,
        coin=COIN,
        quantity=qty,
        valuation_price=Decimal(100),
        equity=equity,
    )


def test_strong_cluster_consensus_formats_as_compact_shadow_follow():
    experts = ["A", "B", "C", "D"]
    target = compute_hierarchical_cluster_consensus(
        COIN,
        [
            posture("A", "0.4"),
            posture("B", "0.4"),
            posture("C", "0.4"),
            posture("D", "-0.1"),
        ],
        [[expert] for expert in experts],
        as_of=NOW,
    )
    assessment = _assess_target(target, [[expert] for expert in experts])
    message = _format_target(
        target,
        [[expert] for expert in experts],
        Decimal(63000),
    )

    assert assessment.direction == "LONG"
    assert assessment.grade == "B"
    assert assessment.follow_state == "PAPER-FOLLOW"
    assert assessment.shadow_risk_cap == Decimal("0.25")
    assert "Shadow risk cap: 0.25R" in message
    assert "Live size: NOT QUALIFIED" in message
    assert "prospective predictive edge is not yet qualified" in message
    assert "A=" not in message
    assert len(message) < TELEGRAM_SAFE_MAX_CHARS


def test_missing_cluster_mass_blocks_follow_and_size():
    clusters = [["A"], ["B"]]
    target = compute_hierarchical_cluster_consensus(
        COIN,
        [
            posture("A", "0.2"),
            posture("B", "1", equity=None),
        ],
        clusters,
        as_of=NOW,
    )
    assessment = _assess_target(target, clusters)
    message = _format_target(target, clusters, Decimal(63000))

    assert assessment.grade == "BLOCKED"
    assert assessment.follow_state == "NO-FOLLOW"
    assert assessment.shadow_risk_cap == 0
    assert "0R" in message
    assert "too much cluster weight is unavailable" in message


def test_fresh_zero_equity_flat_does_not_create_missing_mass():
    clusters = [["A"], ["B"]]
    target = compute_hierarchical_cluster_consensus(
        COIN,
        [
            posture("A", "0.2"),
            posture("B", "0", equity=Decimal(0)),
        ],
        clusters,
        as_of=NOW,
    )

    assert target.missing_mass == 0
    assert target.is_actionable is True
