"""WP3 read-only economic attribution; NOT a predictive or executable PnL engine.

Historical markouts are immutable 1-minute sampled mids. Recompute controls on
exactly the same eligible decision/entry/exit, selecting directions strictly from
prices known before the decision. No wallet rankings, orders or live DB writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .copy_research import RecordedMid

BASELINES = (
    "cash",
    "long_only",
    "lagged_15m_trend",
    "lagged_1h_trend",
    "lagged_1h_reversal",
)
COST_SCENARIOS_BPS = (5, 10, 20, 30)
MAX_QUOTE_AGE = timedelta(seconds=90)
HORIZON = timedelta(hours=1)
MIN_INDEPENDENT_DAYS = 20
DAY_BOOTSTRAP_SEED = 20261030
DAY_BOOTSTRAP_DRAWS = 2000
VALID_COVERAGE = {"SUPPORTED", "Seeded", "KnownFlat"}
KNOWN_TIME_CUTOFF = "2026-10-31T00:00:00+00:00"


def control_directions(
    *, known_at: datetime, quotes: list[RecordedMid],
) -> dict[str, int | None]:
    """Only look backward in as-known time; unavailable history stays missing."""
    if known_at.tzinfo is None:
        raise ValueError("decision clock required")
    if any(q.known_at.tzinfo is None for q in quotes):
        raise ValueError("unqualified input price")
    prior = [q for q in quotes if q.known_at <= known_at]
    latest = max(prior, key=lambda q: q.known_at, default=None)
    directions: dict[str, int | None] = {
        "cash": 0,
        "long_only": 1,
        "lagged_15m_trend": None,
        "lagged_1h_trend": None,
        "lagged_1h_reversal": None,
    }
    if latest is None or known_at - latest.known_at > MAX_QUOTE_AGE:
        return directions
    for name, lookback in (("lagged_15m_trend", timedelta(minutes=15)),
                           ("lagged_1h_trend", timedelta(hours=1))):
        target = known_at - lookback
        anchor = max(
            (q for q in prior if target - MAX_QUOTE_AGE <= q.known_at <= target),
            key=lambda q: q.known_at, default=None,
        )
        if anchor is not None:
            direction = (latest.price > anchor.price) - (latest.price < anchor.price)
            directions[name] = direction
            if name == "lagged_1h_trend":
                directions["lagged_1h_reversal"] = -direction
    return directions


def baseline_net(
    *, direction: int, entry_mid: Decimal, exit_mid: Decimal, cost_bps: int,
) -> Decimal:
    """A flat control pays no round-trip costs; all other arms pay once."""
    if direction not in (-1, 0, 1) or entry_mid <= 0 or exit_mid <= 0:
        raise ValueError("invalid benchmark")
    if cost_bps < 0:
        raise ValueError("negative modeled cost")
    return (
        Decimal(direction) * (exit_mid / entry_mid - 1)
        - Decimal(cost_bps if direction else 0) / Decimal(10_000)
    )


def _quotes(
    db: sqlite3.Connection, *, instrument: str, cutoff: datetime,
) -> list[RecordedMid]:
    start = (cutoff - timedelta(hours=1, minutes=3)).isoformat()
    end = cutoff.isoformat()
    rows = db.execute(
        "SELECT known_at,mid FROM reference_mids "
        "WHERE instrument_id=? AND minute_utc>=? AND minute_utc<=? "
        "ORDER BY minute_utc LIMIT 70",
        (instrument, start, end),
    ).fetchall()
    return [
        RecordedMid(instrument, datetime.fromisoformat(str(known)), Decimal(str(px)))
        for known, px in rows
        if datetime.fromisoformat(str(known)) <= cutoff
    ]


def equal_day_estimate(
    pairs: list[tuple[datetime, Decimal]],
    *, draws: int = DAY_BOOTSTRAP_DRAWS, seed: int = DAY_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Point estimate and interval use the identical equal-UTC-day estimand."""
    if draws < 100 or draws > 5000:
        raise ValueError("bootstrap research budget")
    day_map: dict[str, list[float]] = defaultdict(list)
    for when, v in pairs:
        if when.tzinfo is None or not v.is_finite():
            raise ValueError("invalid per-day effect")
        day_map[when.astimezone(UTC).date().isoformat()].append(float(v * 10_000))
    daily_means = [sum(v) / len(v) for _, v in sorted(day_map.items())]
    n = len(daily_means)
    if n == 0:
        return {"days": 0, "equal_day_mean_bps": None, "ci95_bps": None}
    point = sum(daily_means) / n
    if n < 5:
        return {"days": n, "equal_day_mean_bps": round(point, 6),
                "ci95_bps": None, "interval_reason": "FEWER_THAN_FIVE_DAYS"}
    rng = random.Random(seed)
    trials = sorted(
        sum(rng.choices(daily_means, k=n)) / n for _ in range(draws)
    )
    return {
        "days": n,
        "equal_day_mean_bps": round(point, 6),
        "ci95_bps": [round(trials[int(draws * .025)], 6),
                     round(trials[min(draws - 1, int(draws * .975))], 6)],
        "bootstrap_unit": "UTC_DAY_NOT_INDEPENDENT_WALLETS",
        "bootstrap_seed": seed,
    }


def analyze_historical(
    *, opportunity_db: Path, outcome_db: Path, price_db: Path,
    cutoff: datetime, max_rows: int = 30_000,
) -> dict[str, Any]:
    if cutoff.tzinfo is None or max_rows < 1 or max_rows > 100_000:
        raise ValueError("invalid immutable historical analysis cut or budget")
    if not all(p.exists() for p in (opportunity_db, outcome_db, price_db)):
        raise FileNotFoundError("all three verified read-only data stores required")
    report: dict[str, Any] = {
        "schema": "copy-wp3-attribution-v1",
        "scope": "EXPLORATORY_PRE_FREEZE_DATA_ONLY",
        "horizon": "1h",
        "as_known_before": cutoff.isoformat(),
        "unit": "basis_points",
        "price_source": "MINUTE_SAMPLED_HYPERLIQUID_MID_NOT_EXECUTABLE",
        "observed_execution_costs": False,
        "costs": "MODELED_ROUND_TRIP_ONLY_NO_FUNDING_OR_IMPACT",
        "fixed_holding_period": "ONE_HOUR_FROM_ASSUMED_EARLIEST_ACTIONABLE_NOT_FROM_ENTRY",
        "baseline_directions_known_before": True,
        "predictive_qualified": False,
        "financial_execution": False,
        "allowed_baselines": list(BASELINES),
    }
    with closing(sqlite3.connect(
        f"file:{opportunity_db}?mode=ro", uri=True, timeout=20
    )) as op, closing(sqlite3.connect(
        f"file:{price_db}?mode=ro", uri=True, timeout=20
    )) as prices:
        op.row_factory = sqlite3.Row
        op.execute(
            "ATTACH DATABASE ? AS outcomes",
            (f"file:{outcome_db}?mode=ro",),
        )
        denominators = {
            str(status): int(count) for status, count in op.execute(
                "SELECT m.status,COUNT(*) FROM outcomes.copy_markouts m "
                "JOIN opportunity_decisions d ON d.opportunity_id=m.opportunity_id "
                "WHERE m.horizon='1h' AND d.first_known_at<? "
                "GROUP BY m.status", (cutoff.isoformat(),),
            )
        }
        rows = op.execute(
            "SELECT d.wallet_id,d.instrument_id,d.family,d.first_known_at,"
            "d.earliest_actionable_at,d.first_ledger_seq,d.payload_json,"
            "m.value_json,m.outcome_known_at "
            "FROM opportunity_decisions d "
            "JOIN outcomes.copy_markouts m ON d.opportunity_id=m.opportunity_id "
            "WHERE m.horizon='1h' AND m.status='VALID_HYPOTHETICAL' "
            "AND d.first_known_at<? ORDER BY d.first_known_at,d.first_ledger_seq LIMIT ?",
            (cutoff.isoformat(), max_rows + 1),
        ).fetchall()
        if len(rows) > max_rows:
            raise ValueError("historical row budget exceeded; no truncated selection")
        raw_coverage: Counter[str] = Counter()
        rejects: Counter[str] = Counter()
        chosen: dict[tuple[str, int], dict[str, Any]] = {}
        all_valid = 0
        for row in rows:
            try:
                decision = json.loads(str(row["payload_json"]))
                label = json.loads(str(row["value_json"]))
                first = datetime.fromisoformat(str(row["first_known_at"]))
                ready = datetime.fromisoformat(str(row["earliest_actionable_at"]))
                entry_at = datetime.fromisoformat(label["entry_at"])
                exit_at = datetime.fromisoformat(label["exit_at"])
                outcome_at = datetime.fromisoformat(str(row["outcome_known_at"]))
                gross = Decimal(label["signed_gross_return"])
                net = Decimal(label["hypothetical_net_return"])
                input_cost = Decimal(label["cost_bps"])
                entry_mid = Decimal(label["entry_mid"])
                exit_mid = Decimal(label["exit_mid"])
                direction = int(label["direction"])
                coverage = str(decision.get("source_coverage", "UNSPECIFIED"))
                if not (first.tzinfo and ready.tzinfo and entry_at.tzinfo
                        and exit_at.tzinfo and outcome_at.tzinfo):
                    raise ValueError("NAIVE_CLOCK")
                if not (first <= ready <= entry_at <= exit_at <= outcome_at):
                    raise ValueError("CLOCK_ORDER")
                if (entry_at - ready > MAX_QUOTE_AGE
                        or not HORIZON <= exit_at - ready <= HORIZON + MAX_QUOTE_AGE):
                    raise ValueError("ENTRY_EXIT_CLOCK")
                if (direction not in (-1, 1) or
                        label["price_model"] != "MID_TO_MID_HYPOTHETICAL_NOT_EXECUTABLE"):
                    raise ValueError("WRONG_PRICE_AUTHORITY")
                if (not all(x.is_finite() for x in (gross, net, input_cost, entry_mid, exit_mid))
                        or input_cost < 0 or entry_mid <= 0 or exit_mid <= 0):
                    raise ValueError("NONFINITE_PRICE_OR_RETURN")
                if abs(gross - Decimal(direction) * (exit_mid / entry_mid - 1)) > Decimal("0.00000001"):
                    raise ValueError("GROSS_PRICE_MISMATCH")
                if abs((gross - input_cost / Decimal(10000)) - net) > Decimal("0.00000001"):
                    raise ValueError("DOUBLE_COUNT_OR_INVALID_COST")
                if str(row["family"]) not in {"ENTER", "INCREASE", "REVERSE"}:
                    raise ValueError("NON_ENTRY")
                all_valid += 1
                raw_coverage[coverage] += 1
                key = (
                    str(row["instrument_id"]),
                    int(first.timestamp()) // (4 * 3600),
                )
                # Earliest regardless of later price success; avoid selecting winners
                # based on whether stronger comparator becomes available.
                if key in chosen:
                    continue
                chosen[key] = {
                    "known_at": first,
                    "wallet": str(row["wallet_id"]),
                    "coin": str(row["instrument_id"]).rsplit(":", 1)[-1],
                    "coverage": coverage,
                    "gross": gross,
                    "net": net,
                    "cost": input_cost,
                    "entry_delay_s": (entry_at - ready).total_seconds(),
                    "observed_lag_s": (first - datetime.fromisoformat(decision["first_event_at"])).total_seconds(),
                    "entry_mid": entry_mid,
                    "exit_mid": exit_mid,
                    "instrument": str(row["instrument_id"]),
                }
            except (KeyError, ValueError, TypeError, InvalidOperation, OverflowError) as err:
                rejects[str(err)[:80]] += 1

        comparisons: dict[str, list[tuple[datetime, Decimal, Decimal, Decimal]]] = {
            name: [] for name in BASELINES
        }
        peer_count: Counter[str] = Counter()
        coin_count: Counter[str] = Counter()
        entry_delays: list[float] = []
        observed_lags: list[float] = []
        missing_control: Counter[str] = Counter()
        eligible_sample = 0
        sample_costs: dict[int, list[tuple[datetime, Decimal]]] = {
            c: [] for c in COST_SCENARIOS_BPS
        }
        for row in chosen.values():
            if row["coverage"] not in VALID_COVERAGE:
                continue
            eligible_sample += 1
            peer_count[row["wallet"]] += 1
            coin_count[row["coin"]] += 1
            entry_delays.append(row["entry_delay_s"])
            observed_lags.append(row["observed_lag_s"])
            for cost in COST_SCENARIOS_BPS:
                sample_costs[cost].append((
                    row["known_at"], row["gross"] - Decimal(cost) / Decimal(10000)
                ))
            quotes = _quotes(
                prices, instrument=row["instrument"], cutoff=row["known_at"]
            )
            directions = control_directions(known_at=row["known_at"], quotes=quotes)
            for family, direction in directions.items():
                if direction is None:
                    missing_control[family] += 1
                    continue
                # The modeled cost is applied once per independently taking arm;
                # wallet net comes from the immutable source markout, not a second subtraction.
                control = baseline_net(
                    direction=direction, entry_mid=row["entry_mid"],
                    exit_mid=row["exit_mid"], cost_bps=int(row["cost"])
                )
                comparisons[family].append((
                    row["known_at"], row["gross"], row["net"], control
                ))
    report["denominator_1h"] = {
        "status_counts": denominators,
        "valid_rows_inspected": len(rows),
        "valid_cost_and_causality": all_valid,
        "malformed_or_ineligible": dict(rejects),
        "raw_source_coverage": dict(raw_coverage),
        "independent_by_instrument_4h_bucket": len(chosen),
        "coverage_labeled_buckets": eligible_sample,
        "source_coverage_warning": (
            "Seeded/KnownFlat tag does not prove uninterrupted upstream trades; "
            "external stream history remains unverified"
        ),
    }
    report["sample"] = {
        "utc_days": len({
            row["known_at"].astimezone(UTC).date().isoformat()
            for row in chosen.values() if row["coverage"] in VALID_COVERAGE
        }),
        "wallet_count": len(peer_count),
        "instrument_count": len(coin_count),
        "max_wallet_bucket_share": round(max(peer_count.values()) / eligible_sample, 4)
        if eligible_sample else None,
        "max_instrument_bucket_share": round(max(coin_count.values()) / eligible_sample, 4)
        if eligible_sample else None,
        "entry_delay_seconds_mean": round(sum(entry_delays) / len(entry_delays), 3)
        if entry_delays else None,
        "source_event_to_knowledge_seconds_mean": round(sum(observed_lags) / len(observed_lags), 3)
        if observed_lags else None,
        "dedup_unit": "ONE_INSTRUMENT_PER_FIXED_FOUR_HOUR_UTC_BUCKET",
        "independence": "NOT_PROVEN; COMMON_COIN_SHOCKS_AND_CLONED_WALLETS_POSSIBLE",
    }
    report["modeled_cost_sensitivity"] = {
        str(c): equal_day_estimate(sample_costs[c]) for c in COST_SCENARIOS_BPS
    }
    report["comparators"] = {}
    for family, samples in comparisons.items():
        wallet_gross = [(t, g) for t, g, _, _ in samples]
        wallet_net = [(t, n) for t, _, n, _ in samples]
        market_net = [(t, c) for t, _, _, c in samples]
        incremental = [(t, n - c) for t, _, n, c in samples]
        report["comparators"][family] = {
            "matched_buckets": len(samples),
            "missing_or_stale_feature_buckets": missing_control[family],
            "entry_times_prices_and_costs_paired": True,
            "episode_weighted_wallet_net_bps": round(
                sum(float(x * 10000) for _, x in wallet_net) / len(wallet_net), 6
            ) if wallet_net else None,
            "equal_day_wallet_gross": equal_day_estimate(wallet_gross),
            "equal_day_wallet_net": equal_day_estimate(wallet_net),
            "equal_day_control_net": equal_day_estimate(market_net),
            "equal_day_incremental": equal_day_estimate(incremental),
            "interpretation": "HYPOTHETICAL_DIAGNOSTIC_ONLY",
        }
    report["verdict"] = "INCONCLUSIVE_UNQUALIFIED_HISTORY"
    report["qualifying_test_pending"] = [
        "FROZEN_NEW_PROSPECTIVE_HOLDOUT",
        "ACTUAL_HOURLY_DECISION_EMISSION",
        "SOURCE_COVERAGE_COMPLETENESS",
        "EXECUTABLE_SIDE_SPECIFIC_FILL_AND_COST_MODEL",
        "INDEPENDENT_CLUSTER_AND_EFFECTIVE_DAY_SUPPORT",
        "MULTIPLE_COMPARISON_ADJUSTMENT",
    ]
    return report


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--journal", type=Path, required=True)
    p.add_argument("--outcomes", type=Path, required=True)
    p.add_argument("--prices", type=Path, required=True)
    p.add_argument("--cutoff", type=str, required=True)
    p.add_argument("--max-rows", type=int, default=30_000)
    p.add_argument("--output", type=Path,
                   help="write private immutable JSON report instead of stdout")
    args = p.parse_args()
    result = analyze_historical(
        opportunity_db=args.journal, outcome_db=args.outcomes,
        price_db=args.prices, cutoff=datetime.fromisoformat(args.cutoff),
        max_rows=args.max_rows,
    )
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.output.parent.chmod(0o700)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(rendered.encode())
        print(json.dumps({
            "status": result["verdict"],
            "output": str(args.output),
            "sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            "eligible_buckets": result["denominator_1h"]["coverage_labeled_buckets"],
            "days": result["sample"]["utc_days"],
        }, sort_keys=True))
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
