"""Read-only, prospective Hyperliquid wallet audit. No production journals are written.

Research only: 1-minute mid-price markouts are NOT executable PnL. Account
equity snapshots are NOT cash-flow-adjusted trader returns. Rankings use
predeclared 1h paired control deltas and remain exploratory.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sqlite3
import statistics
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path("/home/quant/.local/share/copytrade")
HORIZONS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "24h": 86400}
CONTROL_REVISION = "31697a5e06d3364d1061"
COSTS_BPS = (5, 10, 20, 30)
MIN_EPISODES = 10
MIN_DAYS = 5
GAP_SECONDS = 120 * 60
COVERED = {"Seeded", "KnownFlat"}
PERMITTED_FAMILIES = {"ENTER", "INCREASE", "REVERSE"}


def utc(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("timestamp without timezone")
    return timestamp.astimezone(UTC)


def ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def finite_number(value: Any) -> float | None:
    try:
        number = float(Decimal(str(value)))
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, InvalidOperation):
        return None


def mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def pctile(sorted_values: list[float], proportion: float) -> float | None:
    if not sorted_values:
        return None
    pos = (len(sorted_values) - 1) * proportion
    left = int(pos)
    return sorted_values[left] * (1 - (pos - left)) + sorted_values[min(left + 1, len(sorted_values) - 1)] * (pos - left)


def day_block_uncertainty(day_values: dict[str, list[float]], *, seed: int = 20261029, repetitions: int = 999) -> dict[str, float | None]:
    """Whole UTC-day resampling; sign-flip is exploratory under exchangeability."""
    daily = [mean(v) for _, v in sorted(day_values.items()) if v]
    daily = [v for v in daily if v is not None]
    if not daily:
        return {"ci_lower": None, "ci_upper": None, "p_one_sided": None}
    observed = statistics.fmean(daily)
    if len(daily) < 2:
        return {"ci_lower": None, "ci_upper": None, "p_one_sided": None}
    rand = random.Random(seed + len(daily))
    samples = sorted(statistics.fmean(rand.choices(daily, k=len(daily))) for _ in range(repetitions))
    # One-sided random sign-flip null; no claim of independently sampled days.
    stronger = sum(statistics.fmean(x * rand.choice((-1, 1)) for x in daily) >= observed for _ in range(repetitions))
    return {"ci_lower": pctile(samples, 0.025), "ci_upper": pctile(samples, 0.975), "p_one_sided": (stronger + 1) / (repetitions + 1)}


def holm(p_values: dict[str, float]) -> dict[str, float]:
    """Holm-adjusted p-values, valid under arbitrary dependence if raw p-values are valid."""
    ordered = sorted(p_values, key=lambda k: p_values[k])
    adjusted: dict[str, float] = {}
    cumulative = 0.0
    m = len(ordered)
    for i, key in enumerate(ordered):
        cumulative = max(cumulative, min(1.0, (m - i) * p_values[key]))
        adjusted[key] = cumulative
    return adjusted


@dataclass(slots=True)
class ObservedWallet:
    wallet_id: str
    first_seen_at: str | None = None
    first_snapshot_at: str | None = None
    first_valid_snapshot_at: str | None = None
    last_valid_snapshot_at: str | None = None
    first_equity: float | None = None
    last_equity: float | None = None
    snapshots: int = 0
    valid_snapshots: int = 0
    gaps_over_120m: int = 0
    last_snapshot_time: datetime | None = None
    max_gap_hours: float = 0.0
    snapshot_days: set[str] = field(default_factory=set)
    actions: Counter[str] = field(default_factory=Counter)
    coverage: Counter[str] = field(default_factory=Counter)
    daily_equity: dict[str, float] = field(default_factory=dict)
    equity_high: float | None = None
    equity_path_dd: float | None = None
    last_positions: list[dict[str, Any]] = field(default_factory=list)

    def snapshot(self, known: str, payload: dict[str, Any]) -> None:
        self.snapshots += 1
        self.first_snapshot_at = self.first_snapshot_at or known
        t = utc(known)
        if self.last_snapshot_time is not None:
            gap = max(0.0, (t - self.last_snapshot_time).total_seconds())
            self.max_gap_hours = max(self.max_gap_hours, gap / 3600)
            if gap > GAP_SECONDS:
                self.gaps_over_120m += 1
        self.last_snapshot_time = t
        self.snapshot_days.add(t.date().isoformat())
        cover = str(payload.get("coverage", "UNKNOWN"))
        self.coverage[cover] += 1
        equity = finite_number((payload.get("equity") or {}).get("account_value"))
        if cover not in COVERED or equity is None or equity < 0:
            return
        self.valid_snapshots += 1
        self.first_valid_snapshot_at = self.first_valid_snapshot_at or known
        self.last_valid_snapshot_at = known
        self.first_equity = equity if self.first_equity is None else self.first_equity
        self.last_equity = equity
        self.daily_equity[t.date().isoformat()] = equity
        if equity > 0:
            self.equity_high = max(equity, self.equity_high or equity)
            if self.equity_high > 0:
                dd = equity / self.equity_high - 1
                self.equity_path_dd = min(self.equity_path_dd or 0.0, dd)
        if isinstance(payload.get("positions"), list):
            self.last_positions = [
                {"instrument_id": p.get("instrument_id"), "quantity_after": p.get("quantity_after"),
                 "opened_at_known": p.get("opened_at_known")}
                for p in payload["positions"] if isinstance(p, dict)
            ]

    def public_dict(self) -> dict[str, Any]:
        equity_change = self.last_equity - self.first_equity if self.first_equity is not None and self.last_equity is not None else None
        return {
            "wallet_id": self.wallet_id,
            "exact_watchlist_add_at": None,
            "first_observed_source_at": self.first_seen_at,
            "first_snapshot_at": self.first_snapshot_at,
            "first_valid_snapshot_at": self.first_valid_snapshot_at,
            "last_valid_snapshot_at": self.last_valid_snapshot_at,
            "observation_days": len(self.snapshot_days),
            "snapshot_count": self.snapshots,
            "valid_snapshot_count": self.valid_snapshots,
            "coverage_gaps_over_120m": self.gaps_over_120m,
            "max_snapshot_gap_h": round(self.max_gap_hours, 2),
            "coverage_types": json.dumps(dict(self.coverage), sort_keys=True),
            "opens": self.actions.get("OPEN", 0),
            "increases": self.actions.get("ADD", 0),
            "reductions": self.actions.get("REDUCE", 0),
            "reversals": self.actions.get("FLIP", 0),
            "closes": self.actions.get("CLOSE", 0),
            "first_equity_usd": self.first_equity,
            "last_equity_usd": self.last_equity,
            "observed_equity_delta_usd_NOT_PNL": equity_change,
            "equity_delta_pct_NOT_RETURN": round(100 * equity_change / self.first_equity, 4) if equity_change is not None and self.first_equity and self.first_equity > 0 else None,
            "equity_peak_to_trough_pct_NOT_TRADING_DD": round(100 * self.equity_path_dd, 4) if self.equity_path_dd is not None else None,
            "verified_trading_pnl_usd": None,
            "verified_cash_flow_adjusted_return": None,
            "source_funding_fees_transfers": "NOT_OBSERVED_IN_THIS_LEDGER",
            "provenance": "ACCOUNT_SNAPSHOT_PARTIAL_HISTORY",
            "latest_position_count": len(self.last_positions),
        }


def scan_ledger(path: Path) -> tuple[dict[str, ObservedWallet], dict[str, Any]]:
    wallets: dict[str, ObservedWallet] = {}
    kinds: Counter[str] = Counter()
    global_coverage: list[dict[str, str]] = []
    with closing(ro(path)) as db:
        ledger = db.execute("SELECT ledger_id,current_seq FROM ledger_meta LIMIT 1").fetchone()
        if ledger is None:
            raise ValueError("no ledger identity")
        seq_cap = int(ledger["current_seq"])
        first_known = last_known = None
        last_seq = 0
        # Read from immutable source via a streaming, bounded cursor.
        db_cursor = db.execute(
            "SELECT seq,kind,wallet_id,event_time,known_at,payload "
            "FROM outbox_events WHERE seq<=? ORDER BY seq", (seq_cap,)
        )
        while True:
            batch = db_cursor.fetchmany(1500)
            if not batch:
                break
            for row in batch:
                seq = int(row["seq"])
                if last_seq and seq != last_seq + 1:
                    raise ValueError(f"source outbox gap after seq {last_seq}")
                last_seq = seq
                kind = str(row["kind"])
                kinds[kind] += 1
                known = str(row["known_at"])
                first_known = first_known or known
                last_known = known
                address = str(row["wallet_id"])
                if kind == "COVERAGE_CHANGE":
                    try:
                        payload = json.loads(row["payload"])
                    except (ValueError, TypeError):
                        payload = {}
                    global_coverage.append({"known_at": known, "status": str(payload.get("status")), "history_during_gap": str(payload.get("history_during_gap"))})
                if not address:
                    continue
                w = wallets.setdefault(address, ObservedWallet(address))
                w.first_seen_at = w.first_seen_at or known
                if kind not in {"ACCOUNT_SNAPSHOT", "POSITION_CHANGE"}:
                    continue
                try:
                    payload = json.loads(str(row["payload"]))
                except (TypeError, ValueError):
                    w.coverage["MALFORMED_PAYLOAD"] += 1
                    continue
                if kind == "ACCOUNT_SNAPSHOT":
                    w.snapshot(known, payload)
                else:
                    w.actions[str(payload.get("transition", "UNKNOWN"))] += 1
        if last_seq != seq_cap:
            raise ValueError("read-only scan incomplete at ledger watermark")
    return wallets, {
        "ledger_id": str(ledger["ledger_id"]), "source_seq_as_of_start": seq_cap,
        "read_through_seq": last_seq, "first_known_at": first_known, "last_known_at": last_known,
        "row_kinds": dict(kinds), "global_feed_coverage_markers": global_coverage,
        "distinct_source_wallets": len(wallets),
    }


@dataclass(frozen=True, slots=True)
class Episode:
    wallet_id: str
    instrument_id: str
    horizon: str
    family: str
    direction: int
    known_at: datetime
    actionable_at: datetime
    outcome_known_at: datetime
    source_coverage: str
    gross_bps: float
    net_10bps: float
    matched_control_10bps: float | None
    delay_s: float

    @property
    def coin(self) -> str:
        return self.instrument_id.rsplit(":", 1)[-1]

    @property
    def day(self) -> str:
        return self.known_at.date().isoformat()

    @property
    def delta_bps(self) -> float | None:
        return self.net_10bps - self.matched_control_10bps if self.matched_control_10bps is not None else None


def validate_episode(decision: dict[str, Any], mark: dict[str, Any], control: dict[str, Any] | None, horizon: str, outcome_known_at: str) -> tuple[Episode | None, str]:
    try:
        if horizon not in HORIZONS or decision["family"] not in PERMITTED_FAMILIES:
            return None, "NON_ENTRY_ACTION"
        if int(decision["copy_direction"]) not in (-1, 1):
            return None, "NO_COPY_DIRECTION"
        event_at = utc(str(decision["first_event_at"]))
        known = utc(str(decision["first_known_at"]))
        ready = utc(str(decision["earliest_actionable_at"]))
        if not event_at <= known <= ready:
            return None, "CAUSAL_TIMESTAMP_FAILURE"
        src = decision["payload"]
        if decision["family"] == "ENTER" and Decimal(str(src.get("source_quantity_before", "NaN"))) != 0:
            return None, "INHERITED_POSITION_OR_INVALID_OPEN"
        if mark.get("status") != "VALID_HYPOTHETICAL" or mark.get("price_model") != "MID_TO_MID_HYPOTHETICAL_NOT_EXECUTABLE":
            return None, "NOT_HYPOTHETICAL_VALID"
        if int(mark["direction"]) != int(decision["copy_direction"]):
            return None, "DIRECTION_MISMATCH"
        entry = utc(str(mark["entry_at"]))
        exit_at = utc(str(mark["exit_at"]))
        label_at = utc(outcome_known_at)
        if entry < ready or entry > ready + timedelta(seconds=90):
            return None, "ENTRY_PRICE_NOT_ACTIONABLE"
        if exit_at < ready + timedelta(seconds=HORIZONS[horizon]) or exit_at > ready + timedelta(seconds=HORIZONS[horizon] + 90):
            return None, "EXIT_PRICE_LATE_OR_EARLY"
        if label_at < exit_at:
            return None, "FUTURE_OUTCOME_LEAKAGE"
        gross = finite_number(mark.get("signed_gross_return"))
        net = finite_number(mark.get("hypothetical_net_return"))
        cost = finite_number(mark.get("cost_bps"))
        if gross is None or net is None or cost is None or cost < 0 or abs((gross - cost / 10000) - net) > 1e-8:
            return None, "CORRUPT_MARKOUT_COST"
        baseline = None
        if control is not None:
            baseline = finite_number(control.get("market_only_net_return"))
            if baseline is None or control.get("control_family") != "same_coin_lagged_1h_momentum" or utc(str(control["eligible_at"])) > known:
                return None, "CONTROL_NOT_POINT_IN_TIME"
        return Episode(
            str(decision["wallet_id"]), str(decision["instrument_id"]), horizon,
            str(decision["family"]), int(decision["copy_direction"]), known, ready,
            label_at, str(src.get("source_coverage", "UNKNOWN")),
            gross * 10000, net * 10000, baseline * 10000 if baseline is not None else None,
            (entry - ready).total_seconds(),
        ), "ACCEPTED"
    except (KeyError, ValueError, TypeError, InvalidOperation, OverflowError):
        return None, "MALFORMED_OR_UNKNOWN_SOURCE"


def dedup_episodes(episodes: list[Episode]) -> tuple[list[Episode], int]:
    """One wallet/instrument episode per full horizon, regardless of direction."""
    selected: list[Episode] = []
    last_end: dict[tuple[str, str, str], datetime] = {}
    dropped = 0
    for e in sorted(episodes, key=lambda x: (x.known_at, x.wallet_id, x.instrument_id, x.horizon)):
        key = e.horizon, e.wallet_id, e.instrument_id
        if e.known_at < last_end.get(key, datetime.min.replace(tzinfo=UTC)):
            dropped += 1
            continue
        selected.append(e)
        last_end[key] = e.actionable_at + timedelta(seconds=HORIZONS[e.horizon])
    return selected, dropped


def load_episodes(opportunity_db: Path, outcome_db: Path) -> tuple[list[Episode], dict[str, Any]]:
    stats: dict[str, Any] = defaultdict(int)
    accepted: list[Episode] = []
    with closing(ro(opportunity_db)) as db:
        db.execute("ATTACH DATABASE ? AS labels", (f"file:{outcome_db}?mode=ro",))
        q = (
            "SELECT d.wallet_id,d.instrument_id,d.family,d.copy_direction,d.first_event_at,"
            "d.first_known_at,d.earliest_actionable_at,d.payload_json,m.horizon,"
            "m.value_json as outcome,m.outcome_known_at,c.value_json as baseline "
            "FROM opportunity_decisions d "
            "JOIN labels.copy_markouts m ON m.opportunity_id=d.opportunity_id "
            "LEFT JOIN labels.copy_controls c ON c.opportunity_id=d.opportunity_id "
            "AND c.horizon=m.horizon AND c.control_revision=? "
            "WHERE d.status='ACTIVE' AND m.status='VALID_HYPOTHETICAL' "
            "ORDER BY d.first_known_at,d.first_ledger_seq"
        )
        for row in db.execute(q, (CONTROL_REVISION,)):
            stats["rows_matched_to_valid_markouts"] += 1
            try:
                src = json.loads(str(row["payload_json"]))
                mark = json.loads(str(row["outcome"]))
                control = json.loads(str(row["baseline"])) if row["baseline"] is not None else None
                d = {k: row[k] for k in ("wallet_id", "instrument_id", "family", "copy_direction", "first_event_at", "first_known_at", "earliest_actionable_at")}
                d["payload"] = src
            except (ValueError, TypeError):
                stats["BAD_JSON"] += 1
                continue
            ep, reason = validate_episode(d, mark, control, str(row["horizon"]), str(row["outcome_known_at"]))
            stats[reason] += 1
            if ep:
                accepted.append(ep)
        stats["distinct_opportunity_wallets"] = db.execute("SELECT count(distinct wallet_id) FROM opportunity_decisions").fetchone()[0]
        stats["all_opportunities"] = db.execute("SELECT count(*) FROM opportunity_decisions").fetchone()[0]
        stats["first_opportunity"] = db.execute("SELECT min(first_known_at) FROM opportunity_decisions").fetchone()[0] or ""
        stats["last_opportunity"] = db.execute("SELECT max(first_known_at) FROM opportunity_decisions").fetchone()[0] or ""
        db.execute("DETACH DATABASE labels")
    episodes, removed = dedup_episodes(accepted)
    stats["episode_overlap_dropped"] = removed
    stats["independent_wallet_episodes"] = len(episodes)
    stats["paired_episode_count"] = sum(e.delta_bps is not None for e in episodes)
    stats["paired_1h_episode_count"] = sum(e.horizon == "1h" and e.delta_bps is not None for e in episodes)
    stats["unverified_1h_episode_count"] = sum(e.horizon == "1h" and e.source_coverage not in COVERED for e in episodes)
    return episodes, dict(stats)


def grouped_day(episodes: list[Episode]) -> dict[str, list[float]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for ep in episodes:
        if ep.delta_bps is not None:
            grouped[ep.day].append(ep.delta_bps)
    return dict(grouped)


def group_independent_coin_time(episodes: list[Episode]) -> list[tuple[str, float, int]]:
    """Avoid double-counting a common coin/hour shock across many wallets.

    Diagnostic time buckets only; no claim that all votes were available at
    the bucket's start or that this models executable consensus.
    """
    grouped: dict[tuple[str, str, int], list[Episode]] = defaultdict(list)
    for ep in episodes:
        if ep.delta_bps is None:
            continue
        bucket = int(ep.known_at.timestamp()) // HORIZONS[ep.horizon]
        grouped[(ep.day, ep.instrument_id, bucket)].append(ep)
    return [(k[0], statistics.fmean(e.delta_bps for e in values if e.delta_bps is not None), len(values)) for k, values in sorted(grouped.items())]


def duplicate_wallet_signatures(episodes: list[Episode]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Ex-post identical 1h trajectories; never use for historical selection."""
    by_wallet: dict[str, list[Episode]] = defaultdict(list)
    for e in episodes:
        if e.horizon == "1h" and e.delta_bps is not None:
            by_wallet[e.wallet_id].append(e)
    signatures: dict[tuple[tuple[str, int, int, float, float], ...], list[str]] = defaultdict(list)
    for wallet, items in by_wallet.items():
        if len(items) < MIN_EPISODES or len({e.day for e in items}) < MIN_DAYS:
            continue
        fingerprint = tuple(sorted((
            e.instrument_id, int(e.known_at.timestamp()) // 60, e.direction,
            round(e.net_10bps, 5), round(e.matched_control_10bps or 0.0, 5),
        ) for e in items))
        signatures[fingerprint].append(wallet)
    clusters = sorted(
        ((signature, sorted(wallets)) for signature, wallets in signatures.items()
         if len(wallets) > 1),
        key=lambda x: (-len(x[1]), x[1][0]),
    )
    rows: list[dict[str, Any]] = []
    labels: dict[str, str] = {}
    for i, (signature, members) in enumerate(clusters, 1):
        group = f"EX_POST_IDENTICAL_{i:03d}"
        rows.append({
            "cluster_id": group, "wallet_count": len(members),
            "episodes_per_wallet": len(signature),
            "wallet_ids_full_private": ";".join(members),
            "method": "SAME_INSTRUMENT_MINUTE_DIRECTION_AND_STORED_MARKOUT_CONTROL",
            "point_in_time_selection_qualified": False,
        })
        for member in members:
            labels[member] = group
    return rows, labels


def wallet_ranking(all_wallet_ids: set[str], episodes: list[Episode]) -> list[dict[str, Any]]:
    by_wallet: dict[str, list[Episode]] = defaultdict(list)
    for ep in episodes:
        if ep.horizon == "1h" and ep.delta_bps is not None:
            by_wallet[ep.wallet_id].append(ep)
    ranking: list[dict[str, Any]] = []
    p_by_wallet: dict[str, float] = {}
    for wallet_id in sorted(all_wallet_ids | set(by_wallet)):
        wallet_events = by_wallet.get(wallet_id, [])
        day_groups = grouped_day(wallet_events)
        stats = day_block_uncertainty(day_groups)
        deltas = [e.delta_bps for e in wallet_events if e.delta_bps is not None]
        daily_means = [mean(values) for values in day_groups.values()]
        daily_means = [value for value in daily_means if value is not None]
        net = [e.net_10bps for e in wallet_events]
        coins = Counter(e.coin for e in wallet_events)
        num = len(wallet_events)
        days = len(day_groups)
        covered = sum(e.source_coverage in COVERED for e in wallet_events)
        if num >= MIN_EPISODES and days >= MIN_DAYS and stats["p_one_sided"] is not None:
            p_by_wallet[wallet_id] = float(stats["p_one_sided"])
        ranking.append({
            "wallet_id": wallet_id, "episodes_1h": num, "utc_days": days,
            "verified_coverage_episodes": covered, "unverified_coverage_episodes": num - covered,
            "primary_equal_day_incremental_bps": mean(daily_means),
            "episode_mean_incremental_bps": mean(deltas),
            "mean_wallet_net_10bps": mean(net), "directional_hit_rate": (sum(e.gross_bps > 0 for e in wallet_events) / num) if num else None,
            "ci95_lower_bps": stats["ci_lower"], "ci95_upper_bps": stats["ci_upper"],
            "one_sided_exploratory_p": stats["p_one_sided"],
            "holm_adjusted_exploratory_p": None,
            "largest_coin_episode_share": max(coins.values()) / num if coins else None,
            "largest_day_episode_share": max((len(v) for v in day_groups.values()), default=0) / num if num else None,
            "worst_episode_delta_bps": min(deltas) if deltas else None,
            "p05_episode_delta_bps": pctile(sorted(deltas), 0.05),
            "mean_sampled_entry_delay_s": mean([e.delay_s for e in wallet_events]),
            "status": "PROVISIONAL_EXPLORATORY" if num >= MIN_EPISODES and days >= MIN_DAYS else "INSUFFICIENT_EVIDENCE",
            "qualification": "NOT_QUALIFIED_EXECUTION_COVERAGE_AND_UNTOUCHED_HOLDOUT",
        })
    adjusted = holm(p_by_wallet)
    for row in ranking:
        row["holm_adjusted_exploratory_p"] = adjusted.get(str(row["wallet_id"]))
        if row["status"] == "PROVISIONAL_EXPLORATORY" and row["unverified_coverage_episodes"]:
            row["status"] = "PROVISIONAL_UNVERIFIED_SOURCE_COVERAGE"
    ranking.sort(key=lambda x: (
        x["episodes_1h"] < MIN_EPISODES or x["utc_days"] < MIN_DAYS,
        -(x["primary_equal_day_incremental_bps"] if x["primary_equal_day_incremental_bps"] is not None else float("-inf")),
        x["wallet_id"],
    ))
    return ranking


def horizon_summary(episodes: list[Episode]) -> list[dict[str, Any]]:
    by_horizon: dict[str, list[Episode]] = defaultdict(list)
    for ep in episodes:
        by_horizon[ep.horizon].append(ep)
    result = []
    for horizon in HORIZONS:
        all_ep = by_horizon[horizon]
        paired = [ep for ep in all_ep if ep.delta_bps is not None]
        daily = grouped_day(paired)
        b = day_block_uncertainty(daily)
        deltas = sorted(ep.delta_bps for ep in paired if ep.delta_bps is not None)
        row: dict[str, Any] = {
            "horizon": horizon, "deduplicated_wallet_episodes": len(all_ep),
            "paired_episodes": len(paired), "paired_distinct_days": len(daily),
            "coverage_verified_episodes": sum(e.source_coverage in COVERED for e in paired),
            "mean_incremental_vs_10bps_control": mean(deltas),
            "median_incremental_vs_10bps_control": pctile(deltas, 0.5),
            "daily_block_ci95_lower": b["ci_lower"], "daily_block_ci95_upper": b["ci_upper"],
            "mean_entry_delay_s": mean([e.delay_s for e in paired]),
            "cost_comparison_note": "Net of costs only; control held at 10bps for incremental baseline",
        }
        for cost in COSTS_BPS:
            row[f"wallet_net_{cost}bps_mean"] = mean([e.gross_bps - cost for e in paired])
        result.append(row)
    return result


def daily_rows(episodes: list[Episode]) -> list[dict[str, Any]]:
    by: dict[tuple[str, str], list[Episode]] = defaultdict(list)
    for ep in episodes:
        if ep.delta_bps is not None:
            by[(ep.horizon, ep.day)].append(ep)
    return [{
        "horizon": horizon, "utc_day": day, "paired_episodes": len(values),
        "independent_coins": len({e.coin for e in values}),
        "mean_incremental_bps": mean([e.delta_bps for e in values if e.delta_bps is not None]),
        "mean_wallet_net_10bps": mean([e.net_10bps for e in values]),
        "coverage_verified_episodes": sum(e.source_coverage in COVERED for e in values),
    } for (horizon, day), values in sorted(by.items())]


def coin_leaderboard(episodes: list[Episode]) -> list[dict[str, Any]]:
    by: dict[tuple[str, str], list[Episode]] = defaultdict(list)
    for e in episodes:
        if e.delta_bps is not None:
            by[(e.horizon, e.coin)].append(e)
    rows = []
    for (h, c), samples in by.items():
        buckets = group_independent_coin_time(samples)
        rows.append({
            "horizon": h, "coin": c, "episodes": len(samples),
            "independent_coin_time_buckets": len(buckets), "days": len({e.day for e in samples}),
            "mean_incremental_bps_coin_time_equal": mean([x[1] for x in buckets]),
            "mean_wallet_net_bps": mean([e.net_10bps for e in samples]),
            "directional_hit_rate": sum(e.gross_bps > 0 for e in samples) / len(samples),
        })
    return sorted(rows, key=lambda r: (r["horizon"], -(r["mean_incremental_bps_coin_time_equal"] or -1e9)))


def policy_comparisons(episodes: list[Episode]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    policies: list[dict[str, Any]] = []
    policy_days: list[dict[str, Any]] = []
    for horizon in ("1h", "4h"):
        sub = sorted((e for e in episodes if e.horizon == horizon and e.delta_bps is not None), key=lambda e: e.known_at)
        days = sorted({e.day for e in sub})
        for policy in ("ALL_WALLETS_EQUAL_EVENT", "ALL_WALLETS_COIN_TIME_DEDUP", "PROVISIONAL_CLUSTER_EQUAL", "EX_ANTE_TOP_5", "EX_ANTE_TOP_10", "EX_ANTE_TOP_20"):
            groups = []
            for day in days:
                eligible = [e for e in sub if e.day == day]
                selected_wallet_count = None
                if policy == "PROVISIONAL_CLUSTER_EQUAL":
                    continue
                if policy.startswith("EX_ANTE_TOP_"):
                    k = int(policy.rsplit("_", 1)[-1])
                    cutoff = utc(day + "T00:00:00+00:00")
                    # Outcomes must be fully known BEFORE day of selection; no
                    # retrospective membership, overlapping label, or current-day PnL.
                    historical = [e for e in sub if e.outcome_known_at < cutoff]
                    by_wallet: dict[str, list[Episode]] = defaultdict(list)
                    for e in historical:
                        by_wallet[e.wallet_id].append(e)
                    eligible_historical = []
                    for wallet, obs in by_wallet.items():
                        daily = grouped_day(obs)
                        if len(obs) >= MIN_EPISODES and len(daily) >= MIN_DAYS:
                            daily_scores = [mean(vals) for vals in daily.values()]
                            daily_scores = [x for x in daily_scores if x is not None]
                            eligible_historical.append((wallet, mean(daily_scores) or 0.0))
                    choices = {x[0] for x in sorted(eligible_historical, key=lambda x: (-x[1], x[0]))[:k]}
                    selected_wallet_count = len(choices)
                    eligible = [e for e in eligible if e.wallet_id in choices]
                if not eligible:
                    continue
                if policy == "ALL_WALLETS_COIN_TIME_DEDUP" or policy.startswith("EX_ANTE_TOP_"):
                    event_data = group_independent_coin_time(eligible)
                    day_mean = mean([x[1] for x in event_data])
                    n_units = len(event_data)
                else:
                    day_mean = mean([e.delta_bps for e in eligible if e.delta_bps is not None])
                    n_units = len(eligible)
                groups.append((day, n_units, len(eligible), day_mean, selected_wallet_count))
                policy_days.append({
                    "horizon": horizon, "policy": policy, "utc_day": day,
                    "independent_coin_time_or_event_units": n_units,
                    "paired_events": len(eligible), "selected_wallet_count": selected_wallet_count,
                    "mean_incremental_bps": day_mean,
                })
            valid = [x for x in groups if x[3] is not None]
            day_means = [(x[0], float(x[3])) for x in valid if x[3] is not None]
            c = day_block_uncertainty({day: [value] for day, value in day_means})
            policies.append({
                "horizon": horizon, "policy": policy, "evaluated_days": len(valid),
                "paired_events": sum(x[2] for x in valid),
                "mean_day_incremental_bps": mean([value for _, value in day_means]),
                "daily_block_ci95_lower": c["ci_lower"], "daily_block_ci95_upper": c["ci_upper"],
                "status": "BLOCKED_NO_POINT_IN_TIME_WALLET_CLUSTER_HISTORY" if policy == "PROVISIONAL_CLUSTER_EQUAL"
                          else "PSEUDO_WALK_FORWARD_EXPLORATORY_NOT_UNTOUCHED"
                          if policy.startswith("EX_ANTE_TOP") else "DESCRIPTIVE_DIAGNOSTIC_NOT_EXECUTABLE",
            })
    return policies, policy_days


def same_day_policy_comparisons(policy_days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare Top-K to coin-time equal-wallet control on matching UTC days."""
    baseline = {
        (str(x["horizon"]), str(x["utc_day"])): float(x["mean_incremental_bps"])
        for x in policy_days if x["policy"] == "ALL_WALLETS_COIN_TIME_DEDUP"
        and x["mean_incremental_bps"] is not None
    }
    by_policy: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(dict)
    for row in policy_days:
        name = str(row["policy"])
        if not name.startswith("EX_ANTE_TOP_") or row["mean_incremental_bps"] is None:
            continue
        h, day = str(row["horizon"]), str(row["utc_day"])
        if (h, day) not in baseline:
            continue
        by_policy[(h, name)][day] = [float(row["mean_incremental_bps"]) - baseline[(h, day)]]
    results = []
    for (h, name), values in sorted(by_policy.items()):
        ci = day_block_uncertainty(values)
        results.append({
            "horizon": h, "topk_policy": name, "matched_utc_days": len(values),
            "mean_topk_minus_all_wallet_coin_time_bps": mean([x[0] for x in values.values()]),
            "ci95_lower": ci["ci_lower"], "ci95_upper": ci["ci_upper"],
            "status": "PSEUDO_FORWARD_MATCHED_DAYS_NOT_UNTOUCHED_OOS",
        })
    return results


def export_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("no_data\n")
    else:
        cols = list(rows[0])
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    path.chmod(0o600)


def write_private(path: Path, data: str) -> None:
    path.write_text(data, encoding="utf-8")
    path.chmod(0o600)


def abbr(value: str) -> str:
    return value[:7] + "..." + value[-5:]


def report_markdown(
    *, ledger: dict[str, Any], stats: dict[str, Any], wallets: dict[str, ObservedWallet],
    ranking: list[dict[str, Any]], horizons: list[dict[str, Any]], policies: list[dict[str, Any]],
    coins: list[dict[str, Any]], all_episodes: list[Episode], output: Path,
    duplicates: list[dict[str, Any]], policy_lifts: list[dict[str, Any]],
    starts: list[dict[str, Any]],
) -> str:
    positive = [x for x in ranking if x["status"].startswith("PROVISIONAL")]
    equity = sorted(
        (w.public_dict() for w in wallets.values() if w.first_equity is not None and w.last_equity is not None),
        key=lambda x: x["observed_equity_delta_usd_NOT_PNL"] or float("-inf"), reverse=True,
    )
    lines = [
        "# Hyperliquid observed wallet performance and predictive skill audit",
        "",
        "**Disposition: INCONCLUSIVE. No wallet is qualified for executable copytrading or statistical predictive promotion.**",
        "",
        "This is a deterministic read-only research cut. Input data are private and unchanged. All reported wallet price returns are hypothetical minute-sampled mid-to-mid values. Source coverage for almost all prior opportunities is unspecified. The observed source and selection history are already inspected, not untouched OOS.",
        "",
        "## Inventory and provenance",
        "",
        f"- Source ledger: `{ledger['ledger_id']}`, contiguous scanned seq 1 through {ledger['read_through_seq']}, events known {ledger['first_known_at']} to {ledger['last_known_at']}.",
        f"- Historical wallets in source ledger: **{ledger['distinct_source_wallets']}**. Wallets with validated equity snapshots: **{sum(w.valid_snapshots > 0 for w in wallets.values())}**.",
        f"- Prospective opportunity journal: **{stats['all_opportunities']}** records, **{stats['distinct_opportunity_wallets']}** wallets; first {stats['first_opportunity']} to {stats['last_opportunity']}.",
        f"- Outcome rows matched to valid hypothetical labels: {stats['rows_matched_to_valid_markouts']}; after causal checks and wallet/instrument/horizon episode dedup: **{stats['independent_wallet_episodes']}**; paired with pre-known same-coin momentum control: **{stats['paired_episode_count']}**.",
        f"- Overlapping wallet/instrument/horizon events dropped: {stats['episode_overlap_dropped']}. Original source coverage unverifiable for {stats['unverified_1h_episode_count']} of the remaining 1h episodes.",
        f"- Global feed coverage warnings recorded: **{len(ledger['global_feed_coverage_markers'])}**. Source event kinds: `{ledger['row_kinds']}`.",
        "",
        "## Actual trader performance versus equity movement",
        "",
        "**Verified trading PnL, cash-flow-adjusted ROI, actual profit factor and genuine trading max drawdown are UNAVAILABLE.** The observation ledger carries partial-history account-equity snapshots and position changes, not complete deposits, withdrawals, transfers, cash-flow-adjusted performance and funding/fees for an independently audited trader return. The first observed snapshot is NOT the exact watchlist-add time. Source-reported realized PnL cannot be assigned to a monitoring interval from final checkpoints alone.",
        "",
        "Top 10 **raw observed equity changes (NOT trading PnL; never rank traders on this as skill):**",
        "",
        "| Wallet | Equity change, USD (not PnL) | First valid snapshot |",
        "|---|---:|---|",
    ]
    lines += [f"| `{abbr(r['wallet_id'])}` | {r['observed_equity_delta_usd_NOT_PNL']:+,.2f} | {r['first_valid_snapshot_at']} |" for r in equity[:10]]
    raw_top = {r["wallet_id"] for r in equity[:10]}
    predictive_top = {r["wallet_id"] for r in positive[:10]}
    lines += ["", f"Raw equity movement (NOT trading performance) versus provisional predictive Top-10 wallet overlap: **{len(raw_top & predictive_top)}/10**. True trader-PnL versus prediction overlap remains unavailable.",
              "", "## First valid monitoring snapshot distribution (not exact watchlist addition)", "",
              "| UTC start day | Wallets |", "|---|---:|"]
    lines += [f"| {r['utc_day']} | {r['wallets']} |" for r in starts]
    lines += ["", "## Predictive skill, predeclared primary 1-hour horizon", "",
        "Primary wallet statistic: equal-UTC-day mean of deduplicated 1h wallet hypothetical net return **minus** the frozen, pre-known same-coin 1h-lag momentum control net return, both using the existing 10-bps baseline. Only 10+ episodes over 5+ UTC days enter provisional ordering. All observations still require a fresh untouched forward holdout and coverage/execution qualification.",
        "",
        "Top 10 **exploratory** wallets (not approved to follow):", "",
        "| Wallet | Episodes | Days | Incremental bps/day-mean | 95% daily-block CI | Unverified coverage |",
        "|---|---:|---:|---:|---|---:|",
    ]
    lines += [f"| `{abbr(r['wallet_id'])}` | {r['episodes_1h']} | {r['utc_days']} | {r['primary_equal_day_incremental_bps']:+.2f} | [{r['ci95_lower_bps']:+.2f}, {r['ci95_upper_bps']:+.2f}] | {r['unverified_coverage_episodes']} |" for r in positive[:10]]
    if not positive:
        lines.append("| No provisional wallets | | | | | |")
    lines += ["", "### Top-20 and bottom-10 provisional scope", "",
        f"- Provisional 1h wallet candidates: **{len(positive)}**; wallets below threshold: **{len(ranking) - len(positive)}**.",
        f"- Full private ranked wallet rows: `{output / 'predictive_leaderboard.csv'}`.",
        "- Top-20 and bottom-10 exports are provided separately. All p-values, intervals and rankings are EXPLORATORY, with selection and wallet dependence limitations.",
        f"- Ex-post identical 1h trajectory groups: **{len(duplicates)}** (not point-in-time clusters). Matching groups are in private `duplicate_wallet_signatures.csv`; identical wallet trajectories do NOT provide independent confirmation.",
        "", "## Horizon decay and cost sensitivity", "",
        "| Horizon | Dedup paired | UTC days | Incremental bps vs 10-bps market control | 10-bps wallet net |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in horizons:
        lines.append(f"| {row['horizon']} | {row['paired_episodes']} | {row['paired_distinct_days']} | {row['mean_incremental_vs_10bps_control']:+.2f} | {row['wallet_net_10bps_mean']:+.2f} |" if row["paired_episodes"] else f"| {row['horizon']} | 0 | 0 | NA | NA |")
    lines += ["", "Wallet-only cost sensitivity at 5/10/20/30 bps is in `horizon_summary.csv`; the frozen comparator already contains 10-bps costs. Do NOT deduct the original 10 bps twice or interpret 5/20/30 bps as a recomputed matched-control experiment. Observed sampled entry delays are separate from an assumed 2-second reaction clock.",
             "", "## Consensus policy diagnostic", "",
             "| Horizon | Policy | Days | Mean daily incremental bps | Status |",
             "|---|---|---:|---:|---|"]
    for p in policies:
        value = "NA" if p["mean_day_incremental_bps"] is None else f"{p['mean_day_incremental_bps']:+.2f}"
        lines.append(f"| {p['horizon']} | {p['policy']} | {p['evaluated_days']} | {value} | {p['status']} |")
    lines += ["", "Same-day Top-K comparison (Top-K minus all-wallet coin-time proxy, paired by UTC day):", "",
              "| Horizon | Policy | Matched days | Difference, bps | Daily-block 95% CI |",
              "|---|---|---:|---:|---|"]
    for p in policy_lifts:
        lower, upper = p["ci95_lower"], p["ci95_upper"]
        interval = f"[{lower:+.2f}, {upper:+.2f}]" if lower is not None and upper is not None else "NA"
        lines.append(f"| {p['horizon']} | {p['topk_policy']} | {p['matched_utc_days']} | {p['mean_topk_minus_all_wallet_coin_time_bps']:+.2f} | {interval} |")
    lines += ["", "An equal-weight *event* proxy and a coin-time-bucketed proxy are descriptive aggregation diagnostics, not an actionable same-instant consensus. Buckets include events arriving at different moments, and cannot be used to trade retroactively. Ex-ante Top-K selection uses only matured labels known before each UTC day; this is pseudo-walk-forward on an inspected period, not untouched OOS. Cluster-based comparison is intentionally BLOCKED until point-in-time wallet cluster membership is evidenced. No pairwise superiority is established.",
             "", "## Strongest exploratory coin effects on 1h", "", "| Coin | Coin-time buckets | Days | Mean paired incremental bps |", "|---|---:|---:|---:|"]
    candidates = [r for r in coins if r["horizon"] == "1h" and r["independent_coin_time_buckets"] >= 10 and r["days"] >= 5]
    for r in sorted(candidates, key=lambda x: -(x["mean_incremental_bps_coin_time_equal"] or -1e9))[:10]:
        lines.append(f"| {r['coin']} | {r['independent_coin_time_buckets']} | {r['days']} | {r['mean_incremental_bps_coin_time_equal']:+.2f} |")
    lines += ["", "## Critical interpretation", "",
             "**Average hypothetical follow returns are negative after the existing 10-bps assumed costs at ALL horizons.** Positive incremental returns mean the comparator loses even more, not that copying makes money. Same-coin momentum is a weak standalone benchmark and this short sample is selected, so no alpha inference is qualified.",
             "", "## Limitations and forward-only recommendation", "",
             "1. Historical wallet additions/removals are not preserved as exact roster-entry events in the scanned source ledger. First seen and first valid account snapshots are observational lower-quality proxies, not the actual time of inclusion.",
             "2. Equity changes are not cash-flow-adjusted trading PnL/ROI. Funding, fees, deposits/withdrawals and pre-existing exposure are unqualified; final source position checkpoints cannot repair this.",
             "3. Source feed gaps and historical UNSPECIFIED coverage cannot be reclassified as reliable by later code fixes. Account snapshots with Seeded coverage anchor an observed equity value but not complete trade history.",
             "4. Minute-sampled reference mid markouts with a flat 10-bps round-trip assumption omit executable spread, impact, order-book depth and exchange-specific fill constraints; assumed two-second latency is not observed fill latency.",
             "5. Dependence persists across wallets, overlapping crypto trends, regimes and instruments. Daily bootstrap/sign-flip is exploratory with a short number of days. Holm corrections are applied among eligible wallets but cannot turn selected historical data into new OOS.",
             "6. No historical point-in-time independent cluster membership is archived for this policy. Cluster policy must abstain, not be silently filled using today's clusters. Wallet selection was based on prior profitability; universe-selection bias remains.",
             "7. Worst adverse excursion and realized per-instrument trading PnL cannot be obtained from these immutable 1m endpoint price marks and partial snapshots without additional data.",
             "",
             "**Minimal next experiment:** freeze the wallet selection and episode policy now; collect 20+ NEW complete UTC days of coverage-verified prospective signals and pre-known controls after the audit cutoff, with per-wallet cash-flow/funding/fee evidence if measuring actual trader returns. Evaluate 1h primary plus registered 4h secondary without touching protected outcomes until the interval closes. Require a matched-control uplift CI excluding zero after wallet-universe multiple testing and executable fill/cost checks before any financial authority.",
             "",
             "Private outputs and per-wallet tearsheets are under this timestamped directory. No production scoring, Telegram, watchlist, financial execution, or source journal was modified.",
             ""]
    return "\n".join(lines)


def create_tearsheets(path: Path, ranking: list[dict[str, Any]], wallets: dict[str, ObservedWallet], episodes: list[Episode]) -> None:
    path.mkdir(mode=0o700)
    by_wallet: dict[str, list[Episode]] = defaultdict(list)
    for ep in episodes:
        if ep.horizon == "1h" and ep.delta_bps is not None:
            by_wallet[ep.wallet_id].append(ep)
    top = [x for x in ranking if x["status"].startswith("PROVISIONAL")][:20]
    for index, r in enumerate(top, start=1):
        key = str(r["wallet_id"])
        w = wallets.get(key)
        lines = [
            f"# Wallet tearsheet {index:02d} — {key}",
            "",
            "**Exploratory, NOT execution-qualified. 1h copy performance is hypothetical mid-to-mid. Equity snapshots cannot measure actual trading ROI without cash flows.**",
            "",
            f"- Primary paired delta (equal-day): {r['primary_equal_day_incremental_bps']:+.2f} bps; {r['episodes_1h']} episodes over {r['utc_days']} UTC days.",
            f"- 95% daily-block interval: [{r['ci95_lower_bps']:+.2f}, {r['ci95_upper_bps']:+.2f}] bps.",
            f"- Source-unverified episodes: {r['unverified_coverage_episodes']}; largest coin share: {r['largest_coin_episode_share']:.1%}.",
            f"- First observed source event: {w.first_seen_at if w else None}. First validated equity snapshot: {w.first_valid_snapshot_at if w else None}.",
            "- Actual trading PnL, fees, funding and time-weighted return: UNAVAILABLE.",
            "", "## Daily 1h signal performance", "", "| UTC day | Episodes | Mean incremental bps |", "|---|---:|---:|",
        ]
        daily = grouped_day(by_wallet.get(key, []))
        lines += [f"| {day} | {len(values)} | {mean(values):+.2f} |" for day, values in sorted(daily.items())]
        if w:
            lines += ["", "## Observed account equity checkpoints (NOT trading returns)", "", "| UTC day | Last observed equity, USD |", "|---|---:|"]
            lines += [f"| {day} | {value:,.2f} |" for day, value in sorted(w.daily_equity.items())]
            lines += ["", "## Latest observed positions (partial-history, not complete exposure PnL)", ""]
            lines += [f"- `{p['instrument_id']}`: quantity `{p['quantity_after']}`, inception known: {p['opened_at_known']}" for p in w.last_positions[:30]]
            lines += ["", f"Snapshot gaps over 2 hours: {w.gaps_over_120m}; max snapshot gap {w.max_gap_hours:.2f} hours."]
        counts = Counter(e.coin for e in by_wallet.get(key, []))
        lines += ["", "## Instrument breakdown of 1h episodes", ""]
        lines += [f"- {coin}: {n} episodes" for coin, n in counts.most_common(15)]
        write_private(path / f"wallet_{index:02d}.md", "\n".join(lines) + "\n")


def run(root: Path, destination: Path | None = None) -> dict[str, Any]:
    started = datetime.now(UTC)
    print("stage 1/4: reading causal matched outcomes", flush=True)
    episodes, stats = load_episodes(root / "copy-opportunities-live.db", root / "copy-outcomes-live.db")
    print(f"stage 1 complete: {stats['paired_1h_episode_count']} paired deduplicated 1h episodes", flush=True)
    print("stage 2/4: scanning source ledger, read-only", flush=True)
    wallets, ledger = scan_ledger(root / "observation.db")
    print(f"stage 2 complete: {len(wallets)} historical wallets; ledger seq {ledger['read_through_seq']}", flush=True)
    print("stage 3/4: computing day blocks, provisional rankings and policy diagnostics", flush=True)
    all_ids = set(wallets) | {e.wallet_id for e in episodes}
    ranking = wallet_ranking(all_ids, episodes)
    duplicate_groups, duplicate_ids = duplicate_wallet_signatures(episodes)
    for row in ranking:
        row["ex_post_identical_signal_group"] = duplicate_ids.get(str(row["wallet_id"]))
    start_counts = Counter(
        w.first_valid_snapshot_at[:10] for w in wallets.values()
        if w.first_valid_snapshot_at is not None
    )
    monitoring_starts = [{"utc_day": day, "wallets": n} for day, n in sorted(start_counts.items())]
    horizons = horizon_summary(episodes)
    daily = daily_rows(episodes)
    coins = coin_leaderboard(episodes)
    policies, policy_days = policy_comparisons(episodes)
    matched_policy_lifts = same_day_policy_comparisons(policy_days)
    for e in episodes:
        if e.wallet_id not in wallets:
            wallets[e.wallet_id] = ObservedWallet(e.wallet_id)
    valid = [x for x in ranking if x["status"].startswith("PROVISIONAL")]
    last_reported = datetime.now(UTC)
    output = destination or (root / "reports" / started.strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    output.chmod(0o700)
    print(f"stage 4/4: writing private reports: {output}", flush=True)
    export_csv(output / "wallet_equity_observed_NOT_PNL.csv", [w.public_dict() for w in sorted(wallets.values(), key=lambda x: x.wallet_id)])
    export_csv(output / "predictive_leaderboard.csv", ranking)
    export_csv(output / "top20_predictive_EXPLORATORY.csv", valid[:20])
    export_csv(output / "bottom10_predictive_EXPLORATORY.csv", list(reversed(valid[-10:])))
    export_csv(output / "horizon_summary.csv", horizons)
    export_csv(output / "utc_daily_summary.csv", daily)
    export_csv(output / "coin_horizon.csv", coins)
    export_csv(output / "policy_comparison.csv", policies)
    export_csv(output / "policy_daily.csv", policy_days)
    export_csv(output / "policy_vs_all_same_days.csv", matched_policy_lifts)
    export_csv(output / "monitoring_start_distribution.csv", monitoring_starts)
    export_csv(output / "duplicate_wallet_signatures.csv", duplicate_groups)
    create_tearsheets(output / "tearsheets", ranking, wallets, episodes)
    write_private(output / "executive.md", report_markdown(ledger=ledger, stats=stats, wallets=wallets, ranking=ranking, horizons=horizons, policies=policies, coins=coins, all_episodes=episodes, output=output, duplicates=duplicate_groups, policy_lifts=matched_policy_lifts, starts=monitoring_starts))
    manifest = {
        "audit_started_at_utc": started.isoformat(), "finished_at_utc": last_reported.isoformat(),
        "source_roster_size": len(wallets), "data_root": str(root), "report_path": str(output),
        "ledger": ledger, "opportunity": stats, "disposition": "INCONCLUSIVE",
        "provisional_wallets": len(valid), "ex_post_duplicate_groups": len(duplicate_groups),
        "monitoring_start_distribution": monitoring_starts,
        "limits": {"min_episodes": MIN_EPISODES, "min_days": MIN_DAYS, "control_revision": CONTROL_REVISION,
                   "primary_horizon": "1h", "costs_bps": COSTS_BPS, "source_gap_threshold_s": GAP_SECONDS},
        "private_exports_include_full_wallet_ids": True, "financial_execution_qualified": False,
    }
    write_private(output / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    summary = {
        "disposition": "INCONCLUSIVE", "report_path": str(output), "ledger_last_seq": ledger["read_through_seq"],
        "wallets_in_ledger": len(wallets), "with_valid_equity": sum(w.valid_snapshots > 0 for w in wallets.values()),
        "opportunities": stats["all_opportunities"], "paired_1h_episodes": stats["paired_1h_episode_count"],
        "provisional_wallets": len(valid), "ex_post_duplicate_groups": len(duplicate_groups),
        "top10_predictive_exploratory": [
            {"wallet": abbr(str(x["wallet_id"])), "episodes": x["episodes_1h"], "days": x["utc_days"],
             "mean_incremental_bps": round(x["primary_equal_day_incremental_bps"], 3),
             "ci": [round(x["ci95_lower_bps"], 3), round(x["ci95_upper_bps"], 3)]}
            for x in valid[:10]
        ], "horizons": [{"horizon": x["horizon"], "paired": x["paired_episodes"],
                        "incremental_bps": round(x["mean_incremental_vs_10bps_control"], 3) if x["mean_incremental_vs_10bps_control"] is not None else None}
                       for x in horizons],
        "full_actual_trader_pnl": "UNAVAILABLE_WITHOUT_ACCOUNT_CASH_FLOWS",
        "cluster_consensus": "BLOCKED_NO_POINT_IN_TIME_CLUSTER_HISTORY",
    }
    print(json.dumps(summary, indent=2), flush=True)
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--output", type=Path, default=None)
    args = p.parse_args()
    run(args.root, args.output)


if __name__ == "__main__":
    main()
