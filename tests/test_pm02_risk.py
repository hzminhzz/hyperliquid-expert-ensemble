"""Behavioral portfolio-risk tests use only synthetic, matched research paths."""

from datetime import UTC, datetime, timedelta
from math import sin

from ensemble.pm02_risk import allocate, daily_path, dependence_pair, portfolio_path

START = datetime(2026, 5, 1, tzinfo=UTC)
BTC = "hyperliquid:mainnet:perp:default:BTC"
ETH = "hyperliquid:mainnet:perp:default:ETH"


def episodes(wallet: str, instrument: str, n: int = 80, offset: int = 0) -> list[dict]:
    return [
        {
            "episode_id": f"{wallet}{i}",
            "event_at": (START + timedelta(days=i + offset)).isoformat(),
            "follower_net_r": 1.4 + 0.4 * sin((i + 1) * (1.0 if instrument == BTC else 2.3)),
            "control_net_r": 0.3,
            "turnover_r": 0.15,
            "signed_exposures_r": {instrument: 0.6},
        }
        for i in range(n)
    ]


def score(lcb: float = 0.5) -> dict:
    return {"effective_n": 65.0, "incremental_lcb_r": lcb}


def test_absent_dates_are_not_assumed_zero():
    a = daily_path(episodes("A", BTC, offset=0))
    b = daily_path(episodes("B", ETH, offset=60))
    assert len(a) == len(b) == 80
    assert portfolio_path({"A": 0.15, "B": 0.15}, {"A": a, "B": b}) is None
    assert dependence_pair(a, b)["status"] == "UNKNOWN_INSUFFICIENT_PAIRED_DAYS"


def test_identical_managers_and_exposures_are_dependence_cluster():
    a = daily_path(episodes("A", BTC))
    b = daily_path(episodes("A", BTC))
    detail = dependence_pair(a, b)
    assert detail["status"] == "CLONE_OR_DEPENDENT"
    assert detail["clone"]
    result = allocate(
        ["A", "B"],
        {"A": score(), "B": score()},
        {"A": episodes("A", BTC), "B": episodes("A", BTC)},
        {"A": "ACTIVE", "B": "ACTIVE"},
    )
    assert result["dependence_clusters"] == [["A", "B"]]
    assert sum(s["wallet"] is not None for s in result["five_seats"]) == 1


def test_independent_managers_get_marginal_allocation_and_matched_comparators():
    result = allocate(
        ["A", "B"],
        {"A": score(2.0), "B": score(2.1)},
        {"A": episodes("A", BTC), "B": episodes("B", ETH)},
        {"A": "ACTIVE", "B": "ACTIVE"},
    )
    assert len([s for s in result["five_seats"] if s["wallet"]]) == 2
    assert result["risk_allocated_r"] <= 0.5
    assert result["reserve_r"] >= 0.5
    assert result["comparators"]["equal_risk"]["risk"]["observed_days"] == 80
    assert result["comparators"]["shrinkage_inverse_vol"]["risk"]["observed_days"] == 80
    assert all(x["marginal_utility"] > 0 for x in result["ranked_marginal_candidates"])
    assert result["central_risk_authority"] == "PM03_ACCOUNT_SUPERVISOR_REQUIRED"


def test_unverified_lifecycle_state_prevents_budget():
    result = allocate(
        ["A"],
        {"A": score()},
        {"A": episodes("A", BTC)},
        {"A": "SHADOW"},
    )
    assert all(s["state"] == "EMPTY" for s in result["five_seats"])
    assert result["blocked_candidates"]["A"] == "LIFECYCLE_NOT_ACTIVE_RESEARCH"


def test_unknown_exposures_never_infer_diversification():
    a = episodes("A", BTC)
    a[5].pop("signed_exposures_r")
    result = allocate(["A"], {"A": score()}, {"A": a}, {"A": "ACTIVE"})
    assert result["five_seats"][0]["state"] == "EMPTY"
    assert result["blocked_candidates"]["A"] == "INSUFFICIENT_PAIRED_RISK_OR_TAIL_LIMIT"


def test_tail_coincidence_identified_independent_of_average_returns():
    x = episodes("A", BTC)
    y = episodes("B", ETH)
    for i in range(len(x)):
        if i % 10 == 0:
            x[i]["follower_net_r"] = -1.0
            y[i]["follower_net_r"] = -1.0
    detail = dependence_pair(daily_path(x), daily_path(y))
    assert detail["correlated_tails"]
    assert detail["status"] == "CLONE_OR_DEPENDENT"


def test_risk_budget_never_exceeds_seats_or_uncertainty_reserve():
    histories = {str(i): episodes(str(i), BTC if i % 2 else ETH) for i in range(12)}
    scores = {name: score(1.0) for name in histories}
    states = {name: "ACTIVE" for name in histories}
    result = allocate(list(histories), scores, histories, states)
    assert len(result["five_seats"]) == 5
    assert result["risk_allocated_r"] <= 0.5
    assert result["reserve_r"] >= 0.5
    assert all(s["risk_budget_r"] <= 0.2 for s in result["five_seats"])
