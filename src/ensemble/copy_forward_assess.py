"""One-shot preregistered WP4 assessment, gated until the full holdout matures.

This can never grant live trading or predictive qualification on minute mids.
It preserves failures and tests the fixed cash and market-only comparisons
only after the manifest's untouched holdout assessment time.
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
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .copy_economics import BASELINES, equal_day_estimate
from .copy_forward import (
    EVAL_REVISION,
    FORWARD_DB,
    PROTOCOL,
    FrozenEvidence,
    parse_utc,
)

DAY_RESAMPLES = 2000
SEED = 20261030


def _holm_bootstrap(deltas_by_family: dict[str, list[tuple[datetime, Decimal]]],
                    *, draws: int = DAY_RESAMPLES) -> dict[str, Any]:
    """One-sided day sign-flip null check + Holm, assuming independent signs.

    This is only exploratory until day-level exchangeability/dependence has
    been qualified. Do not mistake bootstrap tail mass for a null p-value.
    """
    raw: dict[str, float | None] = {}
    for family, items in deltas_by_family.items():
        days: dict[str, list[float]] = defaultdict(list)
        for when, delta in items:
            days[when.astimezone(UTC).date().isoformat()].append(float(delta))
        means = [sum(v)/len(v) for _,v in sorted(days.items())]
        if len(means) < 5:
            raw[family] = None
            continue
        rng = random.Random(SEED)
        observed = sum(means)/len(means)
        exceed = 0
        for _ in range(draws):
            under_null = sum(
                value * rng.choice((-1,1)) for value in means
            ) / len(means)
            if under_null >= observed:
                exceed += 1
        raw[family] = (exceed+1)/(draws+1)
    if any(raw.get(name) is None for name in BASELINES):
        return {"raw_one_sided_p":raw,"holm_adjusted_p":None,
                "status":"INCOMPLETE_CONTROL_OR_DAY_SUPPORT"}
    pvalues={name:float(v) for name,v in raw.items() if v is not None}
    ranked=sorted(BASELINES,key=lambda name:pvalues[name])
    adj:dict[str,float]={}
    last=0.0
    for ix,name in enumerate(ranked):
        last=max(last,min(1.0,pvalues[name]*(len(ranked)-ix)))
        adj[name]=round(last,6)
    return {
        "raw_one_sided_p":{k:round(v,6) for k,v in pvalues.items()},
        "holm_adjusted_p":adj,
        "status":"EXPLORATORY_DAY_SIGNFLIP_NULL_NOT_EXECUTION_QUALIFICATION",
        "assumption":"UTC_DAY_SIGN_EXCHANGEABILITY_NOT_VALIDATED",
    }


def assess(
    *, frozen: FrozenEvidence, forward_db: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("assessment clock must be timezone-aware")
    threshold = parse_utc(frozen.sealed["protocol"]["holdout"]["assessment_not_before"])
    if now < threshold:
        raise RuntimeError("HOLDOUT_LOCKED_NO_INTERIM_OUTCOME_INSPECTION")
    if not forward_db.exists():
        return {"status":"BLOCKED_NO_FORWARD_JOURNAL",
                "protocol_id":frozen.protocol_id,"qualified":False}
    low = frozen.start.isoformat()
    high = frozen.end.isoformat()
    with closing(sqlite3.connect(
        f"file:{forward_db}?mode=ro",uri=True,timeout=20
    )) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute(
            "SELECT c.period,c.decision_at,a.instrument_id,a.direction,"
            "a.disposition,a.payload_json arm,e.status,e.payload_json outcome "
            "FROM forward_arms a JOIN forward_cuts c ON c.cut_id=a.cut_id "
            "LEFT JOIN forward_evaluations e ON e.arm_id=a.arm_id "
            "AND e.evaluation_revision=? "
            "WHERE c.protocol_id=? AND c.decision_at>=? AND c.decision_at<? "
            "ORDER BY c.decision_at,a.instrument_id",
            (EVAL_REVISION+":1h",frozen.protocol_id,low,high),
        ).fetchall()
    counters:Counter[str]=Counter()
    deduped:dict[tuple[str,int],sqlite3.Row]={}
    for r in rows:
        counters["all_arm_rows"]+=1
        if r["direction"]==0:
            counters["abstained"]+=1
        else:
            counters["candidate_arms"]+=1
        t=parse_utc(r["decision_at"])
        if t.minute < 5 or t.minute > 8:
            counters["invalid_decision_cadence"]+=1
            continue
        key=(str(r["instrument_id"]),int(t.timestamp())//(4*3600))
        if key not in deduped:
            deduped[key]=r
    wallet_nets: list[tuple[datetime,Decimal]]=[]
    cost20: list[tuple[datetime,Decimal]]=[]
    incrementals:dict[str,list[tuple[datetime,Decimal]]]={
        k:[] for k in BASELINES
    }
    day_count:Counter[str]=Counter()
    instrument_count:Counter[str]=Counter()
    paired:Counter[str]=Counter()
    for r in deduped.values():
        counters["deduped_instrument_four_hour"]+=1
        if r["direction"]==0:
            counters["deduped_abstained"]+=1
            continue
        status=r["status"]
        if status is None:
            counters["missing_matured_outcome"]+=1
            continue
        if status != "VALID_HYPOTHETICAL":
            counters["censored_or_invalid"]+=1
            continue
        try:
            outcome=json.loads(str(r["outcome"]))
            arm=json.loads(str(r["arm"]))
            t=parse_utc(str(r["decision_at"]))
            if (outcome["status"]!="VALID_HYPOTHETICAL"
                    or outcome["qualified"] is not False
                    or arm["direction"]!=r["direction"]
                    or outcome["horizon"]!="1h"):
                raise ValueError("invalid outcome authority")
            gross=Decimal(outcome["gross_return"])
            net=Decimal(outcome["net_return"])
            cost=Decimal(outcome["modeled_roundtrip_cost_bps"])
            if not (gross.is_finite() and net.is_finite()
                    and abs(gross-net-cost/Decimal(10000)) < Decimal("0.0000000001")):
                raise ValueError("invalid cost identity")
            if (outcome["actual_user_delivery_verified"] is not False
                    or outcome["observed_quote_depth"] is not False):
                raise ValueError("unknown executable-cost authority")
            wallet_nets.append((t,net))
            cost20.append((t,gross-Decimal(20)/Decimal(10000)))
            day_count[t.date().isoformat()]+=1
            instrument_count[str(r["instrument_id"])]+=1
            baseline_nets=outcome["market_baseline_nets"]
            for family in BASELINES:
                value=baseline_nets.get(family)
                if value is not None:
                    v=Decimal(str(value))
                    if not v.is_finite():
                        raise ValueError("invalid baseline return")
                    incrementals[family].append((t,net-v))
                    paired[family]+=1
        except (KeyError,ValueError,TypeError,ArithmeticError,IndexError):
            counters["malformed_outcome"]+=1
    spec=frozen.sealed["protocol"]["holdout"]
    day_support=len(day_count)
    retained=len(wallet_nets)
    max_coin=max(instrument_count.values(),default=0)/retained if retained else None
    means={
        "wallet_hypothetical_net_equal_day":equal_day_estimate(wallet_nets),
        "wallet_net_20bps_equal_day":equal_day_estimate(cost20),
    }
    comparison={
        family:{
            "paired":paired[family],
            "equal_day_incremental":equal_day_estimate(incrementals[family]),
        } for family in BASELINES
    }
    statistics=_holm_bootstrap(incrementals)
    blockers=[
        "UNVERIFIED_SOURCE_HISTORY",
        "NO_VERIFIED_SIDE_SPECIFIC_EXECUTION_OR_FILL_COSTS",
        "TELEGRAM_HTTP_ACK_NOT_CLIENT_DELIVERY",
        "UNVERIFIED_WALLET_CLUSTER_INDEPENDENCE",
        "DAY_SIGN_EXCHANGEABILITY_NOT_VALIDATED",
        "WALLET_CONCENTRATION_NOT_MEASURABLE_FROM_AGGREGATE_CAPSULE",
    ]
    if day_support < int(spec["min_utc_days"]):
        blockers.append("INSUFFICIENT_INDEPENDENT_DAYS")
    if retained < int(spec["min_instrument_4h_buckets"]):
        blockers.append("INSUFFICIENT_DEDUPED_MARKETS")
    if max_coin is not None and max_coin > float(spec["max_single_instrument_bucket_share"]):
        blockers.append("EXCESS_COIN_CONCENTRATION")
    if counters["missing_matured_outcome"] or counters["invalid_decision_cadence"]:
        blockers.append("MISSING_OR_INVALID_FORWARD_EVIDENCE")
    net_ci=means["wallet_hypothetical_net_equal_day"].get("ci95_bps")
    if not net_ci or net_ci[0]<=0:
        blockers.append("NO_POSITIVE_CASH_NET_BOOTSTRAP_LOWER_BOUND")
    if means["wallet_net_20bps_equal_day"].get("equal_day_mean_bps") is None or (
        means["wallet_net_20bps_equal_day"]["equal_day_mean_bps"] <= 0
    ):
        blockers.append("NET_LOSES_AT_20BPS_MODELED_COST")
    holm=statistics.get("holm_adjusted_p")
    if holm is None or any(holm.get(family,1)>0.05 for family in BASELINES):
        blockers.append("MATCHED_MARKET_CONTROLS_NOT_BEATEN_AFTER_MULTIPLE_TESTING")
    return {
        "schema":"copytrade-WP4-locked-assessment-v1",
        "assessed_at":now.isoformat(),
        "protocol_id":frozen.protocol_id,
        "holdout_start":low,"holdout_end_exclusive":high,
        "status":"BLOCKED_NOT_PREDICTIVE_OR_EXECUTION_QUALIFIED",
        "denominators":dict(counters),
        "independent_utc_days":day_support,
        "valid_hypothetical_buckets":retained,
        "max_single_instrument_bucket_share":max_coin,
        "max_single_wallet_bucket_share":None,
        "source_coverage_qualified":False,
        "independent_wallets_qualified":False,
        "end_user_delivery_qualified":False,
        "execution_prices_qualified":False,
        "financial_execution":False,
        "net_diagnostics":means,
        "market_controls":comparison,
        "multiple_testing":statistics,
        "blockers":blockers,
        "note":"NO_TRADING_AUTHORITY; prior unqualified samples remain failures",
    }


def save_report(*,report:dict[str,Any],output:Path)->dict[str,Any]:
    payload=(json.dumps(report,indent=2,sort_keys=True)+"\n").encode()
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    output.parent.chmod(0o700)
    fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"wb") as f:
        f.write(payload)
    return {
        "status":report["status"],
        "report":str(output),
        "sha256":hashlib.sha256(payload).hexdigest(),
        "protocol_id":report["protocol_id"],
        "financial_execution":False,
    }


def main() -> None:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=("assess","status"))
    p.add_argument("--protocol",type=Path,default=PROTOCOL)
    p.add_argument("--forward",type=Path,default=FORWARD_DB)
    p.add_argument("--output",type=Path)
    args=p.parse_args()
    frozen=FrozenEvidence(protocol_path=args.protocol)
    if args.command=="status":
        # Do not inspect realized returns before the protected end/embargo.
        from .copy_forward import verify_forward

        print(json.dumps(verify_forward(frozen=frozen,forward_db=args.forward),
                         sort_keys=True,indent=2))
        return
    if args.output is None:
        p.error("--output required for one-shot immutable final assessment")
    report=assess(frozen=frozen,forward_db=args.forward,now=datetime.now(UTC))
    print(json.dumps(save_report(report=report,output=args.output),sort_keys=True))


if __name__=="__main__":
    main()
