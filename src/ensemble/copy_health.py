"""Bounded agent-readable integrity capsule (DI0/DI6).

Read-only source/health inspection: separate a running service from intact
coverage, sampled outcomes, market-data freshness, and actual alpha authority.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _utc(value: str) -> datetime:
    parsed=datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("unqualified health timestamp")
    return parsed.astimezone(UTC)


def _memory() -> dict[str, int | None]:
    metrics:dict[str,int]={}
    try:
        lines=Path("/proc/meminfo").read_text().splitlines()
        for line in lines:
            name,_,value=line.partition(":")
            if name in {"MemAvailable","SwapTotal","SwapFree"}:
                metrics[name]=int(value.strip().split()[0])//1024
    except (OSError,ValueError,IndexError):
        pass
    return {
        "available_mib":metrics.get("MemAvailable"),
        "swap_total_mib":metrics.get("SwapTotal"),
        "swap_free_mib":metrics.get("SwapFree"),
    }


def inspect_integrity(
    *,
    ledger_db:Path,
    research_db:Path|None=None,
    health_path:Path|None=None,
    opportunity_db:Path|None=None,
    outcome_db:Path|None=None,
    now:datetime|None=None,
) -> dict[str,Any]:
    now=now or datetime.now(UTC)
    output:dict[str,Any]={
        "schema":"DI6-system-brief-v1","as_of":now.isoformat(),
        "financial_execution":False,
        "memory":_memory(),
        "source":{"status":"UNAVAILABLE"},
        "scoring":{"status":"UNAVAILABLE"},
        "research":{"qualification":"UNKNOWN"},
        "blockers":[],
    }
    if not ledger_db.exists():
        output["blockers"].append("SOURCE_LEDGER_MISSING")
    else:
        with closing(sqlite3.connect(f"file:{ledger_db}?mode=ro",uri=True,timeout=5)) as db:
            source=db.execute(
                "SELECT ledger_id,current_seq FROM ledger_meta LIMIT 1"
            ).fetchone()
            if source is None:
                output["blockers"].append("SOURCE_LEDGER_IDENTITY_MISSING")
            else:
                output["source"]={
                    "status":"LEDGER_AVAILABLE_NOT_COVERAGE_QUALIFIED",
                    "ledger_id":str(source[0]),"current_seq":int(source[1]),
                }
    if research_db is not None and research_db.exists():
        with closing(sqlite3.connect(f"file:{research_db}?mode=ro",uri=True,timeout=5)) as db:
            cut_count=db.execute("SELECT count(*) FROM signal_cuts").fetchone()[0]
            # Minute coverage only; a detected gap is not automatically loss
            # of every Rust observation or trade receipt.
            gaps=db.execute(
                "SELECT prev,minute_utc, "
                "(unixepoch(minute_utc)-unixepoch(prev))/60 AS missing_minutes "
                "FROM (SELECT minute_utc,lag(minute_utc) OVER "
                "(ORDER BY minute_utc) prev "
                "FROM (SELECT DISTINCT minute_utc FROM signal_cuts)) "
                "WHERE (unixepoch(minute_utc)-unixepoch(prev))>120 "
                "ORDER BY missing_minutes DESC LIMIT 5"
            ).fetchall()
            output["scoring"]={
                "status":"HISTORICAL_GAPS" if gaps else "MINUTE_TIMELINE_NO_GAP_OBSERVED",
                "cut_rows":int(cut_count),
                "gap_boundaries":[
                    {"start":str(p),"end":str(q),"interval_minutes":int(m)}
                    for p,q,m in gaps
                ],
            }
            if gaps:
                output["blockers"].append("SCORING_GAP_NOT_RETROACTIVELY_RECOVERABLE")
    if opportunity_db is not None and opportunity_db.exists():
        with closing(sqlite3.connect(
            f"file:{opportunity_db}?mode=ro",uri=True,timeout=5
        )) as db:
            cursor=db.execute("SELECT max(last_seq) FROM opportunity_state").fetchone()[0]
            count,last_known=db.execute(
                "SELECT count(*),max(first_known_at) FROM opportunity_decisions"
            ).fetchone()
            output["opportunities"]={
                "mode":"SHADOW_ONLY", "cursor":cursor,
                "candidate_count":int(count),"newest_first_known_at":last_known,
                "predictive_qualification":"BLOCKED",
            }
    if outcome_db is not None and outcome_db.exists():
        with closing(sqlite3.connect(
            f"file:{outcome_db}?mode=ro",uri=True,timeout=5
        )) as db:
            output["copy_outcomes"]={
                "status":"HYPOTHETICAL_ONLY",
                "total_labels":int(db.execute(
                    "SELECT count(*) FROM copy_markouts"
                ).fetchone()[0]),
                "paired_controls":int(db.execute(
                    "SELECT count(*) FROM copy_controls"
                ).fetchone()[0]),
            }
    if health_path is not None and health_path.exists():
        health=json.loads(health_path.read_text())
        stamp=health.get("timestamp")
        age=None
        if stamp:
            age=(now-_utc(str(stamp))).total_seconds()
        output["runtime"]={
            "health_age_s":round(age,1) if age is not None else None,
            "ledger_lag":health.get("ingestion",{}).get("lag"),
            "wallets":health.get("experts",{}).get("configured"),
            "last_error":health.get("last_error"),
            "telegram_delivery":health.get("telegram",{}).get("last_delivery_ok"),
        }
        qualification=str(health.get("research",{}).get("status","UNKNOWN"))
        output["research"]={"qualification":qualification}
        if age is None or age>120:
            output["blockers"].append("RUNTIME_HEALTH_STALE")
        if "QUALIFIED" not in qualification or qualification.startswith("DESCRIPTIVE"):
            output["blockers"].append("PREDICTIVE_ALPHA_NOT_QUALIFIED")
    swap_free=output["memory"]["swap_free_mib"]
    if swap_free is not None and swap_free <= 64:
        output["blockers"].append("HOST_SWAP_HEADROOM_LOW")
    # A capability-correct agent brief is small and cannot suggest financial orders.
    if len(json.dumps(output,separators=(",",":")).encode())>=6144:
        raise ValueError("system inspection must remain compact")
    return output
