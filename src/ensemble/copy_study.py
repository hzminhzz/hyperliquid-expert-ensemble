"""DI4 exploratory paired evaluation with overlap and market-regime controls.

Do not use this report for predictive promotion: the observation window and
candidate definitions have already been inspected. Requires an untouched
future holdout and qualified execution cost model.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def _bucket(when: datetime, horizon: str) -> tuple[str,int]:
    seconds=86400 if horizon=="24h" else 14400
    utc=when.astimezone(UTC)
    return (utc.date().isoformat(),int(utc.timestamp())//seconds)


def evaluate_incremental_edge(
    *,
    opportunity_db:Path,
    outcome_db:Path,
    horizon:str,
    control_revision:str,
    max_rows:int=100_000,
    min_independent_days:int=20,
) -> dict[str,Any]:
    """Paired same-coin momentum comparator and conservative time dedup.

    In a 4h or 24h bucket of the same instrument, keep the earliest known
    opportunity. This is diagnostic dependence reduction, not true cluster
    independence or a significance test.
    """
    if not 1<=max_rows<=200_000 or min_independent_days<1:
        raise ValueError("invalid research budget/support")
    if horizon not in {"5m","15m","1h","4h","24h"}:
        raise ValueError("unknown registered horizon")
    with closing(sqlite3.connect(f"file:{opportunity_db}?mode=ro",uri=True,timeout=15)) as o:  # noqa: SIM117
        with closing(sqlite3.connect(
            f"file:{outcome_db}?mode=ro",uri=True,timeout=15
        )) as labels:
            o.row_factory=sqlite3.Row
            labels.row_factory=sqlite3.Row
            decisions={}
            for r in o.execute(
                "SELECT opportunity_id,instrument_id,family,first_known_at,"
                "wallet_id FROM opportunity_decisions ORDER BY first_ledger_seq LIMIT ?",
                (max_rows,),
            ):
                decisions[str(r["opportunity_id"])]=dict(r)
            pairs=[]
            for row in labels.execute(
                "SELECT m.opportunity_id,m.value_json outcome,c.value_json comparator "
                "FROM copy_markouts m JOIN copy_controls c "
                "ON c.opportunity_id=m.opportunity_id AND c.horizon=m.horizon "
                "WHERE m.horizon=? AND m.status='VALID_HYPOTHETICAL' "
                "AND c.control_revision=? LIMIT ?",
                (horizon,control_revision,max_rows),
            ):
                decision=decisions.get(str(row["opportunity_id"]))
                if decision is None:
                    continue
                obs=json.loads(str(row["outcome"]))
                baseline=json.loads(str(row["comparator"]))
                if obs["hypothetical_net_return"] is None:
                    continue
                t=datetime.fromisoformat(str(decision["first_known_at"]))
                if t.tzinfo is None:
                    continue
                market=Decimal(baseline["market_only_net_return"])
                signal=Decimal(obs["hypothetical_net_return"])
                pairs.append((
                    t,str(decision["instrument_id"]),str(decision["family"]),
                    str(decision["wallet_id"]),signal-market,
                ))
    # Keep the earliest occurrence per instrument/bucket, across all wallets.
    # Prevent 50 wallets in one coin shock manufacturing 50 independent profits.
    pairs.sort(key=lambda x:x[0])
    independent=[]
    seen=set()
    for p in pairs:
        key=(p[1],_bucket(p[0],horizon))
        if key in seen:
            continue
        seen.add(key)
        independent.append(p)
    by_day:dict[str,list[Decimal]]=defaultdict(list)
    by_coin:dict[str,list[Decimal]]=defaultdict(list)
    by_family:dict[str,list[Decimal]]=defaultdict(list)
    for when,coin,family,_wallet,lift in independent:
        by_day[when.astimezone(UTC).date().isoformat()].append(lift)
        by_coin[coin.rsplit(":",1)[-1]].append(lift)
        by_family[family].append(lift)
    days=len(by_day)
    n=len(independent)
    mean_delta=sum((p[-1] for p in independent),Decimal(0))/Decimal(n) if n else None
    return {
        "horizon":horizon,
        "control_revision":control_revision,
        "paired_events":len(pairs),
        "deduped_instrument_time_buckets":n,
        "observed_utc_days":days,
        "mean_incremental_bps":str(mean_delta*Decimal(10_000)) if mean_delta is not None else None,
        "coin_buckets":{k:len(v) for k,v in sorted(by_coin.items())},
        "family_buckets":{k:len(v) for k,v in sorted(by_family.items())},
        "qualified_executable_pnl":False,
        "out_of_sample_qualified":False,
        "decision":"INCONCLUSIVE_INDEPENDENT_DAYS" if days<min_independent_days
                   else "EXPLORATORY_ONLY_PROSPECTIVE_HOLDOUT_REQUIRED",
        "limitations":[
            "RETROSPECTIVE_SOURCE_SELECTION",
            "MID_PRICE_PLUS_ASSUMED_COST_NOT_EXECUTABLE",
            "CONTROL_SELECTION_AND_TUNING_NOT_INDEPENDENT",
            "TIME_BUCKETS_NOT_PROOF_OF_INDEPENDENT_CLUSTERS",
        ],
    }
