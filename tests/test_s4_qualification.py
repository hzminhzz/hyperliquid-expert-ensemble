"""S4 Qualification Suite: Q10 Baseline Signal Semantics, Fixed Normalization, and Causal Explanations."""

from decimal import Decimal

from ensemble.consensus import (
    CausalChangeCategory,
    compute_equal_budget_consensus,
)
from ensemble.explain import explain_blocker, explain_target
from ensemble.posture import (
    EligibilityState,
    compute_posture,
)


def test_q10_exact_fixed_scale_normalization_arithmetic():
    """Q10: Exact arithmetic matches declared normalization: e = q * px / equity; s = clip(e/k, -1, 1)."""
    # Expert 1: 100% long exposure -> posture = 1.0
    p1 = compute_posture(
        expert_id="0xexp1",
        coin="BTC",
        quantity=Decimal("2.0"),
        valuation_price=Decimal(50000),
        equity=Decimal(100000),
    )
    assert p1.state == EligibilityState.ELIGIBLE
    assert p1.raw_exposure == Decimal("1.0")
    assert p1.clipped_posture == Decimal("1.0")

    # Expert 2: -75% short exposure -> posture = -0.75
    p2 = compute_posture(
        expert_id="0xexp2",
        coin="BTC",
        quantity=Decimal("-1.5"),
        valuation_price=Decimal(50000),
        equity=Decimal(100000),
    )
    assert p2.state == EligibilityState.ELIGIBLE
    assert p2.raw_exposure == Decimal("-0.75")
    assert p2.clipped_posture == Decimal("-0.75")

    # Expert 3: 250% leveraged long exposure -> raw exposure 2.5, clipped to 1.0
    p3 = compute_posture(
        expert_id="0xexp3",
        coin="BTC",
        quantity=Decimal("5.0"),
        valuation_price=Decimal(50000),
        equity=Decimal(100000),
    )
    assert p3.state == EligibilityState.ELIGIBLE
    assert p3.raw_exposure == Decimal("2.5")
    assert p3.clipped_posture == Decimal("1.0")


def test_q10_distinct_effects_of_eligibility_states():
    """Q10: Known flat, unavailable, abstention, and removal have distinct semantic effects."""
    # 1. Known flat: contributes exactly 0.0 without adding to missing mass
    p_flat = compute_posture(
        expert_id="0xflat",
        coin="BTC",
        quantity=Decimal("0.0"),
        valuation_price=Decimal(50000),
        equity=Decimal(100000),
    )
    assert p_flat.state == EligibilityState.KNOWN_FLAT
    assert p_flat.clipped_posture == Decimal("0.0")

    # 2. Abstaining: out of scope
    p_abstain = compute_posture(
        expert_id="0xabstain",
        coin="BTC",
        quantity=Decimal("1.0"),
        valuation_price=Decimal(50000),
        equity=Decimal(100000),
        is_in_scope=False,
    )
    assert p_abstain.state == EligibilityState.ABSTAINING

    # 3. Unavailable: stale equity
    p_stale = compute_posture(
        expert_id="0xstale",
        coin="BTC",
        quantity=Decimal("1.0"),
        valuation_price=Decimal(50000),
        equity=Decimal(100000),
        equity_age_minutes=20.0,  # >15m
    )
    assert p_stale.state == EligibilityState.UNAVAILABLE
    assert "stale" in (p_stale.reason or "")

    # 4. Unavailable: missing equity
    p_missing = compute_posture(
        expert_id="0xmissing",
        coin="BTC",
        quantity=Decimal("1.0"),
        valuation_price=Decimal(50000),
        equity=None,
    )
    assert p_missing.state == EligibilityState.UNAVAILABLE

    # 5. Unavailable: unseeded
    p_unseeded = compute_posture(
        expert_id="0xunseeded",
        coin="BTC",
        quantity=Decimal("1.0"),
        valuation_price=Decimal(50000),
        equity=Decimal(100000),
        is_unseeded=True,
    )
    assert p_unseeded.state == EligibilityState.UNAVAILABLE


def test_q10_no_renormalization_missing_experts_cannot_amplify_survivors():
    """Q10 / R3 CRITICAL INVARIANT:

    Missing mass 'm' is NOT reallocated to surviving experts.
    Missing experts cannot amplify surviving votes through renormalization.
    """
    # Universe of 4 experts: each has fixed weight w_i = 1/4 = 0.25
    p1 = compute_posture(
        "0xexp1", "BTC", Decimal("2.0"), Decimal(50000), Decimal(100000)
    )  # +1.0 posture
    p2 = compute_posture(
        "0xexp2", "BTC", Decimal("-2.0"), Decimal(50000), Decimal(100000)
    )  # -1.0 posture
    p3 = compute_posture(
        "0xexp3", "BTC", Decimal("0.0"), Decimal(50000), Decimal(100000)
    )  # 0.0 known flat
    p4_unavail = compute_posture(
        "0xexp4", "BTC", Decimal("1.0"), Decimal(50000), None
    )  # UNAVAILABLE

    # Target calculation
    target = compute_equal_budget_consensus("BTC", [p1, p2, p3, p4_unavail])

    # Expert 1 contributes: 0.25 * 1.0 = 0.25
    # Expert 2 contributes: 0.25 * -1.0 = -0.25
    # Expert 3 contributes: 0.25 * 0.0 = 0.0
    # Expert 4 (unavailable): missing mass = 0.25
    assert target.observed_target == Decimal("0.0")
    assert target.missing_mass == Decimal("0.25")
    assert target.lower_bound == Decimal("-0.25")
    assert target.upper_bound == Decimal("0.25")

    # Now simulate Expert 2 also becoming UNAVAILABLE (e.g. stale equity)
    p2_unavail = compute_posture(
        "0xexp2", "BTC", Decimal("-2.0"), Decimal(50000), Decimal(100000), equity_age_minutes=30.0
    )

    target_2 = compute_equal_budget_consensus("BTC", [p1, p2_unavail, p3, p4_unavail])

    # INVARIANT CHECK:
    # If renormalization occurred, Expert 1's weight would be re-divided by 2 survivors = 0.50,
    # giving C_observed = 0.50 * 1.0 = 0.50.
    # WITHOUT renormalization (the mandatory rule), Expert 1's weight stays 0.25:
    assert target_2.contributions[0].weight == Decimal("0.25")
    assert target_2.contributions[0].weighted_contribution == Decimal("0.25")
    assert target_2.observed_target == Decimal("0.25")  # NOT amplified!
    assert target_2.missing_mass == Decimal("0.50")  # 2 missing experts = 0.50 mass
    assert target_2.lower_bound == Decimal("-0.25")
    assert target_2.upper_bound == Decimal("0.75")
    # Actionability blocked because missing mass >= 0.50
    assert target_2.is_actionable is False
    assert target_2.blocker_code == "INSUFFICIENT_COVERAGE"


def test_causal_explanations_and_contribution_ledger():
    """Verify explain_target and explain_blocker outputs."""
    p1 = compute_posture("0xexp1", "BTC", Decimal("1.0"), Decimal(60000), Decimal(100000))
    p2 = compute_posture("0xexp2", "BTC", Decimal("0.0"), Decimal(60000), Decimal(100000))

    prior = compute_equal_budget_consensus("BTC", [p2, p2])
    current = compute_equal_budget_consensus(
        "BTC", [p1, p2], prior_target=prior, cause=CausalChangeCategory.ECONOMIC_CHANGE
    )

    explanation = explain_target(current, prior_target=prior)

    assert explanation["coin"] == "BTC"
    assert explanation["cause_category"] == "ECONOMIC_CHANGE"
    assert explanation["change_from_prior"]["delta"] is not None
    assert len(explanation["contributions"]) == 2
    assert explanation["contributions"][0]["expert_id"] == "0xexp1"

    # Blocker explanations
    stale_info = explain_blocker("EQUITY_STALE")
    assert stale_info["status"] == "EXPLAINED"
    assert "Refresh" in stale_info["remediation"]

    unseeded_info = explain_blocker("STATE_UNSEEDED")
    assert unseeded_info["status"] == "EXPLAINED"
