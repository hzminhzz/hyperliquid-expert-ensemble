"""Evaluate immutable hourly forward decisions with sampled mids only.

Labels are determined after maturity and HTTP Telegram acknowledgement,
never from reconstructed historical forecasts. Missing source/price/delivery
stays censored. No executable price or prop-broker fill is claimed.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .copy_economics import BASELINES, baseline_net
from .copy_forward import (
    DATA,
    EVAL_REVISION,
    FORWARD_DB,
    HOLD,
    PROTOCOL,
    QUOTE_LATENESS,
    ForwardStore,
    FrozenEvidence,
    parse_utc,
)
from .copy_research import RecordedMid


def read_quotes(
    *, price_db: sqlite3.Connection, instrument: str, start: datetime,
    end: datetime, now: datetime,
) -> list[RecordedMid]:
    if not start <= end or any(x.tzinfo is None for x in (start, end, now)):
        raise ValueError("invalid bounded price window")
    earliest = (start - timedelta(minutes=2)).isoformat()
    latest = (end + timedelta(minutes=2)).isoformat()
    rows = price_db.execute(
        "SELECT known_at,mid FROM reference_mids "
        "WHERE instrument_id=? AND minute_utc BETWEEN ? AND ? "
        "AND known_at<=? ORDER BY minute_utc LIMIT 320",
        (instrument, earliest, latest, now.isoformat()),
    ).fetchall()
    return [
        RecordedMid(instrument, parse_utc(str(r[0])), Decimal(str(r[1])))
        for r in rows if start - timedelta(minutes=2) <= parse_utc(str(r[0])) <=
        end + timedelta(minutes=2)
    ]


def first_after(
    quotes: list[RecordedMid], *, target: datetime,
) -> RecordedMid | None:
    eligible = [
        q for q in quotes
        if target <= q.known_at <= target + QUOTE_LATENESS
    ]
    return min(eligible, key=lambda q: q.known_at) if eligible else None


def source_gap(
    *, journal_db: sqlite3.Connection, ledger_id: str,
    begin: datetime, end: datetime,
) -> bool:
    return journal_db.execute(
        "SELECT 1 FROM source_coverage_barriers "
        "WHERE ledger_id=? AND known_at>? AND known_at<=? LIMIT 1",
        (ledger_id, begin.isoformat(), end.isoformat()),
    ).fetchone() is not None


def _terminal(status: str, *, period: str, instrument: str, horizon: str,
              reason: str, diagnostic: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema": "copy-forward-evaluation-v1",
        "period": period, "instrument_id": instrument, "horizon": horizon,
        "status": status, "reason": reason,
        "reference_price": "MINUTE_SAMPLED_MID_NOT_EXECUTABLE",
        "actual_user_delivery_verified": False,
        "observed_quote_depth": False,
        "model_costs_only": True,
        "qualified": False, "financial_execution": False,
        "detail": diagnostic or {},
    }


def evaluate_one(
    *, arm: dict[str, Any], cut: dict[str, Any],
    deliveries: list[sqlite3.Row], prices: sqlite3.Connection,
    journal: sqlite3.Connection, now: datetime, horizon: str,
) -> dict[str, Any] | None:
    if horizon not in ("15m", "1h", "4h"):
        raise ValueError("unregistered horizon")
    if now.tzinfo is None:
        raise ValueError("outcome clock not timezone aware")
    period = str(cut["period"])
    instrument = str(arm["instrument_id"])
    decision_at = parse_utc(str(cut["decision_at"]))
    if now < decision_at:
        raise ValueError("future decision not evaluable")
    if arm["direction"] == 0:
        return _terminal("ABSTAIN",period=period,instrument=instrument,horizon=horizon,
                         reason=str(arm["reason"]))
    if arm.get("disposition") != "CANDIDATE_RESEARCH_ONLY":
        return _terminal("CENSORED",period=period,instrument=instrument,horizon=horizon,
                         reason="UNRECOGNIZED_FORECAST_DISPOSITION")
    # An ambiguous prior send could have delivered earlier than our verified
    # acknowledgement. Never assign an entry timestamp from a later retry.
    if any(d["status"] == "UNCONFIRMED" for d in deliveries):
        if now < decision_at + timedelta(minutes=15):
            return None
        return _terminal("CENSORED",period=period,instrument=instrument,horizon=horizon,
                         reason="TELEGRAM_DELIVERY_AMBIGUOUS")
    good = [
        parse_utc(str(d["acknowledged_at"])) for d in deliveries
        if d["status"] == "ACKNOWLEDGED" and d["acknowledged_at"]
    ]
    if not good:
        if now < decision_at + timedelta(minutes=15):
            return None
        return _terminal("CENSORED",period=period,instrument=instrument,horizon=horizon,
                         reason="NO_CONFIRMED_TELEGRAM_HTTP_ACK")
    ack_at = min(good)
    if ack_at - decision_at > timedelta(minutes=3):
        return _terminal("CENSORED",period=period,instrument=instrument,
                         horizon=horizon,reason="TELEGRAM_ACK_OUTSIDE_HOURLY_WINDOW")
    # HTTP acknowledgement is a measurable lower bound on delivery, NOT a
    # server-verified instant the person read or could act on the message.
    ready = max(decision_at, ack_at) + timedelta(seconds=5)
    controls = arm.get("frozen_baseline_directions")
    if (not isinstance(controls, dict)
            or set(controls) != set(BASELINES)
            or arm.get("baseline_frozen_at") != decision_at.isoformat()):
        return _terminal("CENSORED",period=period,instrument=instrument,
                         horizon=horizon,reason="BASELINE_NOT_FROZEN_AT_DECISION")
    entry_prices = read_quotes(
        price_db=prices, instrument=instrument,
        start=ready, end=ready + QUOTE_LATENESS, now=now,
    )
    entry = first_after(entry_prices, target=ready)
    if entry is None:
        if now < ready + QUOTE_LATENESS:
            return None
        return _terminal("CENSORED",period=period,instrument=instrument,horizon=horizon,
                         reason="NO_DELAYED_ENTRY_PRICE",
                         diagnostic={"ready_at":ready.isoformat(),"ack_at":ack_at.isoformat()})
    horizon_delay = {"15m":timedelta(minutes=15),"1h":HOLD,
                     "4h":timedelta(hours=4)}[horizon]
    target_exit = entry.known_at + horizon_delay
    if now < target_exit + QUOTE_LATENESS:
        return None
    if source_gap(
        journal_db=journal, ledger_id=str(cut["source_ledger_id"]),
        begin=decision_at, end=target_exit,
    ):
        return _terminal("CENSORED",period=period,instrument=instrument,horizon=horizon,
                         reason="SOURCE_COVERAGE_GAP_DURING_FOLLOW_WINDOW",
                         diagnostic={"entry_at":entry.known_at.isoformat()})
    exit_prices = read_quotes(
        price_db=prices, instrument=instrument,
        start=target_exit, end=target_exit + QUOTE_LATENESS, now=now,
    )
    exit_quote = first_after(exit_prices, target=target_exit)
    if exit_quote is None:
        return _terminal("CENSORED",period=period,instrument=instrument,horizon=horizon,
                         reason="NO_MATURED_EXIT_PRICE",
                         diagnostic={"entry_at":entry.known_at.isoformat(),
                                     "exit_target":target_exit.isoformat()})
    gross = Decimal(int(arm["direction"])) * (exit_quote.price / entry.price - 1)
    modeled_cost = Decimal(10) / Decimal(10_000)
    wallet_net = gross - modeled_cost
    control_values: dict[str, str | None] = {}
    for family in BASELINES:
        direction = controls[family]
        control_values[family] = (
            str(baseline_net(
                direction=direction, entry_mid=entry.price,
                exit_mid=exit_quote.price, cost_bps=10,
            )) if direction is not None else None
        )
    return {
        **_terminal("VALID_HYPOTHETICAL",period=period,instrument=instrument,
                    horizon=horizon,reason="MEASUREMENT_NOT_EXECUTION"),
        "first_decision_at": decision_at.isoformat(),
        "telegram_api_ack_at": ack_at.isoformat(),
        "entry_ready_at": ready.isoformat(),
        "entry_quote_known_at": entry.known_at.isoformat(),
        "exit_target": target_exit.isoformat(),
        "exit_quote_known_at": exit_quote.known_at.isoformat(),
        "assumed_reaction_seconds": 5,
        "sampled_entry_quote_delay_seconds": (
            entry.known_at - ready).total_seconds(),
        "sampled_exit_quote_delay_seconds": (
            exit_quote.known_at - target_exit).total_seconds(),
        "position_direction": arm["direction"],
        "entry_mid": str(entry.price), "exit_mid": str(exit_quote.price),
        "gross_return": str(gross),
        "modeled_roundtrip_cost_bps": 10,
        "net_return": str(wallet_net),
        "cost_sensitivity_wallet_net": {
            str(bps): str(gross - Decimal(bps)/Decimal(10_000))
            for bps in (5,10,20,30)
        },
        "market_baseline_nets": control_values,
        "baseline_frozen_at": arm["baseline_frozen_at"],
        "baseline_direction_vintage": arm["baseline_vintage"],
        "market_baseline_missing": [
            k for k, value in control_values.items() if value is None
        ],
        "source_history_verified": False,
        "follower_execution_fill_proven": False,
        "interpretation": "HYPOTHETICAL_PIT_FORWARD_SAMPLE_NOT_QUALIFIED",
    }


def evaluate_due(
    *, frozen: FrozenEvidence, forward_db: Path, journal_db: Path,
    price_db: Path, now: datetime | None = None, limit: int = 250,
    revision: str = EVAL_REVISION, supersede: str | None = None,
) -> dict[str, Any]:
    if limit < 1 or limit > 500 or revision != EVAL_REVISION:
        raise ValueError("evaluation budget or frozen evaluator revision changed")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("evaluation requires timezone-aware cut")
    result = {
        "status": "RESEARCH_DIAGNOSTIC_ONLY",
        "inspected": 0, "written": 0, "pending": 0,
        "censored": 0, "valid_hypothetical": 0, "abstain": 0,
        "protocol_id": frozen.protocol_id,
        "executable_pnl_qualified": False,
        "financial_execution": False,
    }
    if not forward_db.exists():
        return {**result,"status":"FORWARD_COLLECTION_NOT_STARTED"}
    if not price_db.exists() or not journal_db.exists():
        raise FileNotFoundError("maturity source stores missing")
    with ForwardStore(forward_db) as store, closing(sqlite3.connect(
        f"file:{price_db}?mode=ro", uri=True, timeout=20
    )) as prices, closing(sqlite3.connect(
        f"file:{journal_db}?mode=ro", uri=True, timeout=20
    )) as journal:
        # Fair round-robin between new and old *due* arms/horizons. Never
        # paginate a giant table from the start and starve recent maturities.
        queues = []
        for horizon,delay in (("1h",timedelta(hours=1)),
                              ("15m",timedelta(minutes=15)),
                              ("4h",timedelta(hours=4))):
            due=(now-max(delay,timedelta(minutes=15))
                 -timedelta(minutes=3)).isoformat()
            for newest in (True,False):
                order = "DESC" if newest else "ASC"
                rows = store.db.execute(
                    "SELECT a.arm_id,a.payload_json arm,c.payload_json cut "
                    "FROM forward_arms a JOIN forward_cuts c ON c.cut_id=a.cut_id "
                    "WHERE c.protocol_id=? AND c.decision_at<=? "
                    "AND NOT EXISTS (SELECT 1 FROM forward_evaluations e "
                    "WHERE e.arm_id=a.arm_id AND e.evaluation_revision=?) "
                    + f"ORDER BY c.decision_at {order},a.instrument_id LIMIT ?",
                    (frozen.protocol_id,due,revision+":"+horizon,limit),
                ).fetchall()
                queues.append((horizon,rows))
        selected: set[tuple[str,str]] = set()
        from itertools import zip_longest

        for batch in zip_longest(*(rows for _,rows in queues)):
            for idx,row in enumerate(batch):
                if row is None:
                    continue
                horizon=queues[idx][0]
                arm_id=str(row["arm_id"])
                identity=(arm_id,horizon)
                if identity in selected:
                    continue
                selected.add(identity)
                if result["inspected"] >= limit:
                    return result
                arm=json.loads(row["arm"])
                cut=json.loads(row["cut"])
                deliveries=store.db.execute(
                    "SELECT * FROM forward_delivery WHERE cut_id=? ORDER BY delivery_id",
                    (cut["cut_id"],),
                ).fetchall()
                result["inspected"]+=1
                observation=evaluate_one(
                    arm=arm,cut=cut,deliveries=deliveries,
                    prices=prices,journal=journal,now=now,horizon=horizon,
                )
                if observation is None:
                    result["pending"]+=1
                    continue
                if store.record_evaluation(
                    arm_id=arm_id,revision=revision+":"+horizon,
                    result=observation,created_at=now,supersedes=supersede,
                ):
                    result["written"]+=1
                    if observation["status"]=="CENSORED":
                        result["censored"]+=1
                    elif observation["status"]=="ABSTAIN":
                        result["abstain"]+=1
                    else:
                        result["valid_hypothetical"]+=1
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("evaluate",))
    parser.add_argument("--protocol",type=Path,default=PROTOCOL)
    parser.add_argument("--forward",type=Path,default=FORWARD_DB)
    parser.add_argument("--journal",type=Path,default=DATA/"copy-opportunities-live.db")
    parser.add_argument("--prices",type=Path,default=DATA/"market-reference.db")
    parser.add_argument("--limit",type=int,default=250)
    args=parser.parse_args()
    out=evaluate_due(
        frozen=FrozenEvidence(protocol_path=args.protocol),
        forward_db=args.forward,journal_db=args.journal,price_db=args.prices,
        now=datetime.now(UTC),limit=args.limit,
    )
    print(json.dumps(out,sort_keys=True,indent=2))


if __name__=="__main__":
    main()
