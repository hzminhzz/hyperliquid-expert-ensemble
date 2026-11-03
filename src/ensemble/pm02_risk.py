"""Pure PM-02 dependence and hypothetical CIO risk-budget calculations.

This module has no market feed, account, broker or execution dependencies.
Only time-aligned, independently observed prospective returns can contribute.
Missing dates are never replaced by zero PnL or assumed-flat positions.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from math import ceil, isfinite, sqrt
from statistics import mean, pstdev

MIN_PAIRED_DAYS = 40
CLONE_CORRELATION = 0.85
CLONE_EXPOSURE_SIMILARITY = 0.90
SHARED_EXTREME_TAIL = 0.65
MIN_TAIL_OBSERVATIONS = 5
MAX_SEATS = 5
MAX_ALLOCATED_R = 0.50
MAX_MANAGER_R = 0.20
MAX_INSTRUMENT_CONCENTRATION_R = 0.25
MAX_AGGREGATE_TAIL_LOSS_R = 0.30
DIVERSIFICATION_RESERVE_R = 0.50
TAIL_UTILITY_PENALTY = 0.25
CONCENTRATION_UTILITY_PENALTY = 0.05
UNCERTAINTY_UTILITY_PENALTY = 0.10


def daily_path(rows: list[dict]) -> dict[str, dict]:
    """Aggregate *observed* episodes by UTC day; never fill absent dates."""
    grouped: dict[str, dict] = {}
    for row in rows:
        dt = datetime.fromisoformat(str(row["event_at"]))
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError("UNTRUSTED_TIME_BUCKET")
        day = dt.astimezone(UTC).date().isoformat()
        if day not in grouped:
            grouped[day] = {
                "follower": 0.0,
                "control": 0.0,
                "turnover": 0.0,
                "exposures": defaultdict(float),
                "has_exposure": True,
            }
        bucket = grouped[day]
        for key, field in (
            ("follower_net_r", "follower"),
            ("control_net_r", "control"),
            ("turnover_r", "turnover"),
        ):
            value = float(row[key])
            if not isfinite(value):
                raise ValueError("NONFINITE_PAIRED_RETURN")
            bucket[field] += value
        positions = row.get("signed_exposures_r")
        if not isinstance(positions, dict) or not positions:
            bucket["has_exposure"] = False
        else:
            for instrument, signed_r in positions.items():
                if not isinstance(instrument, str) or not instrument.startswith("hyperliquid:"):
                    raise ValueError("UNSCOPED_EXPOSURE_INSTRUMENT")
                value = float(signed_r)
                if not isfinite(value):
                    raise ValueError("NONFINITE_EXPOSURE")
                bucket["exposures"][instrument] += value
    return dict(sorted(grouped.items()))


def correlation(a: list[float], b: list[float]) -> float | None:
    if len(a) != len(b) or len(a) < 3:
        return None
    x, y = mean(a), mean(b)
    vx = sum((v - x) ** 2 for v in a)
    vy = sum((v - y) ** 2 for v in b)
    if vx <= 0 or vy <= 0:
        return None
    return sum((u - x) * (v - y) for u, v in zip(a, b, strict=True)) / sqrt(vx * vy)


def _extreme_days(data: dict[str, dict], common: list[str]) -> set[str]:
    series = sorted((float(data[k]["follower"]), k) for k in common)
    count = max(1, ceil(0.10 * len(series)))
    return {k for value, k in series[:count] if value < 0}


def dependence_pair(a: dict[str, dict], b: dict[str, dict]) -> dict:
    common = sorted(set(a) & set(b))
    if len(common) < MIN_PAIRED_DAYS:
        return {"status": "UNKNOWN_INSUFFICIENT_PAIRED_DAYS", "paired_days": len(common)}
    if any(not a[d]["has_exposure"] or not b[d]["has_exposure"] for d in common):
        return {"status": "UNKNOWN_EXPOSURE_COVERAGE", "paired_days": len(common)}
    ra = [a[d]["follower"] for d in common]
    rb = [b[d]["follower"] for d in common]
    ia = [a[d]["follower"] - a[d]["control"] for d in common]
    ib = [b[d]["follower"] - b[d]["control"] for d in common]
    follower_rho = correlation(ra, rb)
    incremental_rho = correlation(ia, ib)
    if follower_rho is None or incremental_rho is None:
        return {"status": "UNKNOWN_DEGENERATE_RETURN_VARIANCE", "paired_days": len(common)}
    dot = 0.0
    norma = 0.0
    normb = 0.0
    common_instruments = set()
    for d in common:
        ea = a[d]["exposures"]
        eb = b[d]["exposures"]
        for instrument in ea.keys() | eb.keys():
            x, y = ea.get(instrument, 0.0), eb.get(instrument, 0.0)
            dot += x * y
            norma += x * x
            normb += y * y
            if x and y:
                common_instruments.add(instrument)
    if norma <= 0 or normb <= 0:
        return {"status": "UNKNOWN_ZERO_EXPOSURES", "paired_days": len(common)}
    similarity = dot / sqrt(norma * normb)
    ta = _extreme_days(a, common)
    tb = _extreme_days(b, common)
    shared_tail = (
        len(ta & tb) / min(len(ta), len(tb))
        if len(ta) >= MIN_TAIL_OBSERVATIONS and len(tb) >= MIN_TAIL_OBSERVATIONS
        else None
    )
    clone = (
        follower_rho >= CLONE_CORRELATION
        or incremental_rho >= CLONE_CORRELATION
        or similarity >= CLONE_EXPOSURE_SIMILARITY
    )
    correlated_tails = shared_tail is not None and shared_tail >= SHARED_EXTREME_TAIL
    return {
        "status": "CLONE_OR_DEPENDENT"
        if clone or correlated_tails
        else "PAIRED_INDEPENDENCE_SCREEN",
        "paired_days": len(common),
        "follower_correlation": round(follower_rho, 6),
        "incremental_correlation": round(incremental_rho, 6),
        "signed_exposure_similarity": round(similarity, 6),
        "same_instruments": sorted(common_instruments),
        "coincident_extreme_tail_fraction": (
            round(shared_tail, 6) if shared_tail is not None else None
        ),
        "clone": clone,
        "correlated_tails": correlated_tails,
    }


def dependence_graph(
    qualified: list[str], paths: dict[str, dict]
) -> tuple[list[list[str]], list[dict]]:
    parent = {name: name for name in qualified}

    def root(a: str) -> str:
        while parent[a] != a:
            a = parent[a]
        return a

    edges = []
    for index, a in enumerate(qualified):
        for b in qualified[index + 1 :]:
            detail = dependence_pair(paths[a], paths[b])
            edges.append({"a": a, "b": b, **detail})
            if detail["status"] == "CLONE_OR_DEPENDENT":
                parent[root(b)] = root(a)
    groups: dict[str, list[str]] = defaultdict(list)
    for a in qualified:
        groups[root(a)].append(a)
    return sorted((sorted(v) for v in groups.values()), key=lambda g: g[0]), edges


def _common_days(selected: list[str], paths: dict[str, dict]) -> list[str]:
    if not selected:
        return []
    common = set(paths[selected[0]])
    for a in selected[1:]:
        common &= set(paths[a])
    return sorted(common)


def portfolio_path(weights: dict[str, float], paths: dict[str, dict]) -> list[float] | None:
    """Same-day *observed* incremental returns on the intersection only."""
    if not weights:
        return None
    days = _common_days(sorted(weights), paths)
    if len(days) < MIN_PAIRED_DAYS:
        return None
    return [
        sum(
            weights[a] * (paths[a][day]["follower"] - paths[a][day]["control"])
            for a in sorted(weights)
        )
        for day in days
    ]


def risk_measures(values: list[float] | None) -> dict | None:
    if not values:
        return None
    equity = 0.0
    peak = 0.0
    maxdd = 0.0
    for v in values:
        equity += v
        peak = max(peak, equity)
        maxdd = max(maxdd, peak - equity)
    sorted_returns = sorted(values)
    tail_count = max(1, ceil(0.05 * len(values)))
    expected_shortfall = mean(sorted_returns[:tail_count])
    return {
        "observed_days": len(values),
        "total_incremental_r": round(sum(values), 8),
        "mean_incremental_r": round(mean(values), 8),
        "realized_vol_r": round(pstdev(values), 8),
        "max_drawdown_r": round(maxdd, 8),
        "worst_day_r": round(min(values), 8),
        "historical_expected_shortfall_r": round(expected_shortfall, 8),
    }


def _instrument_concentration(weights: dict[str, float], paths: dict[str, dict]) -> float | None:
    common = _common_days(sorted(weights), paths)
    if len(common) < MIN_PAIRED_DAYS:
        return None
    concentration: dict[str, float] = defaultdict(float)
    for day in common:
        for a, weight in weights.items():
            exposure = paths[a][day]
            if not exposure["has_exposure"]:
                return None
            for symbol, value in exposure["exposures"].items():
                concentration[symbol] += weight * value / len(common)
    return max((abs(value) for value in concentration.values()), default=None)


def _portfolio_utility(
    weights: dict[str, float], paths: dict[str, dict], scores: dict
) -> float | None:
    values = portfolio_path(weights, paths)
    stats = risk_measures(values)
    if stats is None:
        return None
    concentration = _instrument_concentration(weights, paths)
    if concentration is None or concentration > MAX_INSTRUMENT_CONCENTRATION_R:
        return None
    if -stats["historical_expected_shortfall_r"] > MAX_AGGREGATE_TAIL_LOSS_R:
        return None
    conservative_alpha = sum(
        max(0.0, scores[a]["incremental_lcb_r"]) * weight for a, weight in weights.items()
    )
    uncertainty = sum(
        weight / sqrt(max(1.0, scores[a]["effective_n"])) for a, weight in weights.items()
    )
    return (
        conservative_alpha
        - TAIL_UTILITY_PENALTY * max(0.0, -stats["historical_expected_shortfall_r"])
        - CONCENTRATION_UTILITY_PENALTY * concentration
        - UNCERTAINTY_UTILITY_PENALTY * uncertainty
    )


def _baseline(name: str, chosen: list[str], weights: dict[str, float], paths: dict) -> dict:
    if not chosen:
        return {"status": "INSUFFICIENT_PROSPECTIVE_PAIRED_PATHS"}
    budget = sum(weights.values())
    if name == "EQUAL_RISK":
        proposal = {a: budget / len(chosen) for a in chosen}
    else:
        inverse_vol = {
            a: 1 / max(0.01, pstdev([bucket["follower"] for bucket in paths[a].values()]))
            for a in chosen
        }
        normalizer = sum(inverse_vol.values())
        proposal = {a: budget * inverse_vol[a] / normalizer for a in chosen}
    measures = risk_measures(portfolio_path(proposal, paths))
    return {
        "status": "ESTIMATED_ON_MATCHED_OBSERVED_DAYS" if measures else "INSUFFICIENT_PAIRED_DAYS",
        "weights_r": {a: round(v, 8) for a, v in sorted(proposal.items())},
        "risk": measures,
    }


def allocate(
    qualified: list[str],
    scores: dict[str, dict],
    histories: dict[str, list[dict]],
    previous_states: dict[str, str],
) -> dict:
    """Greedy incremental portfolio utility. Empty is always permissible."""
    names = sorted(set(qualified))
    paths = {a: daily_path(histories.get(a, [])) for a in names}
    clusters, edges = dependence_graph(names, paths)
    blocked: dict[str, str] = {}
    portfolio: dict[str, float] = {}
    ranked: list[dict] = []
    selected_clusters: set[int] = set()
    while len(portfolio) < MAX_SEATS:
        candidates = []
        for a in names:
            if a in portfolio or a in blocked:
                continue
            if previous_states.get(a) not in {"ACTIVE", "CORE"}:
                blocked[a] = "LIFECYCLE_NOT_ACTIVE_RESEARCH"
                continue
            cluster = next(i for i, group in enumerate(clusters) if a in group)
            if cluster in selected_clusters:
                blocked[a] = "DEPENDENT_MANAGER_CLUSTER_ALREADY_SEATED"
                continue
            paired = [
                e
                for e in edges
                if (e["a"] == a and e["b"] in portfolio) or (e["b"] == a and e["a"] in portfolio)
            ]
            if any(e["status"].startswith("UNKNOWN") for e in paired):
                blocked[a] = "UNRESOLVED_PAIRED_DEPENDENCE"
                continue
            if any(e["status"] == "CLONE_OR_DEPENDENT" for e in paired):
                blocked[a] = "DEPENDENCE_CAP"
                continue
            # Reserve at least 0.50 R for unknown market/prop-firm stresses.
            remaining = MAX_ALLOCATED_R - sum(portfolio.values())
            if remaining <= 1e-10:
                break
            n_eff = float(scores[a]["effective_n"])
            weight = round(min(MAX_MANAGER_R, remaining, MAX_MANAGER_R * n_eff / (n_eff + 40)), 8)
            if weight <= 0:
                continue
            candidate = {**portfolio, a: weight}
            utility = _portfolio_utility(candidate, paths, scores)
            if utility is None:
                blocked[a] = "INSUFFICIENT_PAIRED_RISK_OR_TAIL_LIMIT"
                continue
            old_utility = _portfolio_utility(portfolio, paths, scores) if portfolio else 0.0
            if old_utility is None:
                blocked[a] = "PREVIOUS_PORTFOLIO_RISK_UNKNOWN"
                continue
            margin = utility - old_utility
            if margin <= 0:
                blocked[a] = "NONPOSITIVE_MARGINAL_PORTFOLIO_UTILITY"
                continue
            candidates.append((margin, a, weight, cluster))
        if not candidates:
            break
        best = min(candidates, key=lambda row: (-row[0], row[1]))
        margin, a, weight, cluster = best
        portfolio[a] = weight
        selected_clusters.add(cluster)
        ranked.append({"wallet": a, "marginal_utility": round(margin, 8), "risk_budget_r": weight})
    seats = [
        {
            "wallet": a,
            "risk_budget_r": portfolio[a],
            "state": previous_states[a],
            "authority": "RESEARCH_PROPOSAL_ONLY",
        }
        for a in portfolio
    ]
    while len(seats) < MAX_SEATS:
        seats.append(
            {
                "wallet": None,
                "risk_budget_r": 0.0,
                "state": "EMPTY",
                "authority": "RESEARCH_PROPOSAL_ONLY",
            }
        )
    used = round(sum(portfolio.values()), 8)
    chosen = list(portfolio)
    measures = risk_measures(portfolio_path(portfolio, paths))
    return {
        "five_seats": seats,
        "risk_allocated_r": used,
        "reserve_r": round(1.0 - used, 8),
        "minimum_reserve_r": DIVERSIFICATION_RESERVE_R,
        "ranked_marginal_candidates": ranked,
        "blocked_candidates": dict(sorted(blocked.items())),
        "dependence_clusters": clusters,
        "dependence_pairs": edges,
        "portfolio_risk": measures,
        "comparators": {
            "equal_risk": _baseline("EQUAL_RISK", chosen, portfolio, paths),
            "shrinkage_inverse_vol": _baseline("SHRUNK_VOL", chosen, portfolio, paths),
        },
        "emergency_recommendation": (
            "SUSPEND_RESEARCH_ALLOCATION"
            if measures is not None
            and -measures["historical_expected_shortfall_r"] > MAX_AGGREGATE_TAIL_LOSS_R
            else "NO_AUTOMATIC_ACTION_RESEARCH_ONLY"
        ),
        "central_risk_authority": "PM03_ACCOUNT_SUPERVISOR_REQUIRED",
        "live_authority": False,
    }
