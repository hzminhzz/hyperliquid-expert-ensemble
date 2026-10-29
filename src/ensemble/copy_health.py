"""Bounded agent-readable integrity capsule (DI0/DI6).

Read-only source/health inspection: separate a running service from intact
coverage, sampled outcomes, market-data freshness, and actual alpha authority.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .copy_research import DEFAULT_HORIZONS


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
        "disk":{},
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
                floor_row=db.execute(
                    "SELECT retention_floor FROM ledger_meta LIMIT 1"
                ).fetchone()
                floor=int(floor_row[0] or 0) if floor_row else 0
                oldest,newest,count=db.execute(
                    "SELECT min(seq),max(seq),count(*) FROM outbox_events"
                ).fetchone()
                end=int(source[1])
                start=max(1,floor)
                contiguous=(int(count)==max(0,end-start+1)
                            and (int(oldest)==start if oldest is not None else end<start)
                            and (int(newest)==end if newest is not None else end<start))
                output["source"]={
                    "status":"LEDGER_AVAILABLE_NOT_COVERAGE_QUALIFIED",
                    "ledger_id":str(source[0]),"current_seq":end,
                    "retention_floor":floor,
                    "stored_outbox_contiguous":contiguous,
                    "external_websocket_coverage":"UNVERIFIED",
                }
                if not contiguous:
                    output["blockers"].append("SOURCE_OUTBOX_SEQUENCE_GAP")
    if research_db is not None and research_db.exists():
        with closing(sqlite3.connect(f"file:{research_db}?mode=ro",uri=True,timeout=5)) as db:
            has_cuts=db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_cuts'"
            ).fetchone() is not None
            if not has_cuts:
                count,latest=db.execute(
                    "SELECT count(*),max(known_at) FROM reference_mids"
                ).fetchone()
                output["scoring"]={
                    "status":"EVENT_DRIVEN_PRICE_CHECKPOINTS_NO_LEGACY_CUTS",
                    "reference_price_rows":int(count),"latest_price_known_at":latest,
                    "legacy_per_minute_scoring_enabled":False,
                }
            else:
                cut_count=db.execute("SELECT count(*) FROM signal_cuts").fetchone()[0]
                # Historical gaps remain part of archived source evidence.
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
                    output["blockers"].append(
                        "SCORING_GAP_NOT_RETROACTIVELY_RECOVERABLE"
                    )
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
            outcome_info:dict[str,Any]={
                "status":"HYPOTHETICAL_ONLY",
                "price_resolution":"MINUTE_SAMPLED_MID_NOT_2S_EXECUTION",
                "total_labels":int(db.execute(
                    "SELECT count(*) FROM copy_markouts"
                ).fetchone()[0]),
                "paired_controls":int(db.execute(
                    "SELECT count(*) FROM copy_controls"
                ).fetchone()[0]),
            }
            output["copy_outcomes"]=outcome_info
            if opportunity_db is not None and opportunity_db.exists():
                db.execute("ATTACH DATABASE ? AS opp", (str(opportunity_db),))
                pending={}
                for horizon,delay in DEFAULT_HORIZONS:
                    cutoff=(now-delay-timedelta(seconds=90)).isoformat()
                    count,oldest=db.execute(
                        "SELECT count(*),min(d.earliest_actionable_at) "
                        "FROM opp.opportunity_decisions d WHERE d.copy_direction IN (-1,1) "
                        "AND d.earliest_actionable_at<=? "
                        "AND NOT EXISTS (SELECT 1 FROM copy_markouts m "
                        "WHERE m.opportunity_id=d.opportunity_id AND m.horizon=?)",
                        (cutoff,horizon),
                    ).fetchone()
                    overdue=max(0,(now-_utc(str(oldest))-delay-
                                   timedelta(seconds=90)).total_seconds()) if oldest else 0
                    pending[horizon]={
                        "mature_pending":int(count),"oldest_mature_overdue_s":round(overdue)
                    }
                    if overdue>3600 and int(count)>0 and horizon in {"5m","15m","1h"}:
                        output["blockers"].append("MATURE_OUTCOME_BACKLOG_"+horizon.upper())
                outcome_info["mature_pending_by_horizon"]=pending
    if health_path is not None and health_path.exists():
        health=json.loads(health_path.read_text())
        stamp=health.get("timestamp")
        age=None
        if stamp:
            age=(now-_utc(str(stamp))).total_seconds()
        output["runtime"]={
            "health_age_s":max(0,round(age,1)) if age is not None else None,
            "ledger_lag":health.get("ingestion",{}).get("lag"),
            "wallets":health.get("experts",{}).get("configured"),
            "last_error":health.get("last_error"),
            "telegram_delivery":health.get("telegram",{}).get("last_delivery_ok"),
        }
        qualification=str(health.get("research",{}).get("status","UNKNOWN"))
        output["research"]={"qualification":qualification}
        if age is None or age>120:
            output["blockers"].append("RUNTIME_HEALTH_STALE")
        if qualification != "APPROVED_ADVISORY_QUALIFIED":
            output["blockers"].append("PREDICTIVE_ALPHA_NOT_QUALIFIED")
    disk=shutil.disk_usage("/")
    utilization=round(disk.used/disk.total*100,1)
    output["disk"]={"used_percent":utilization,"free_gib":round(disk.free/2**30,1)}
    if utilization>=85:
        output["blockers"].append("HOST_DISK_UTILIZATION_HIGH")
    swap_free=output["memory"]["swap_free_mib"]
    if swap_free is not None and swap_free <= 64:
        output["blockers"].append("HOST_SWAP_HEADROOM_LOW")
    # A capability-correct agent brief is small and cannot suggest financial orders.
    if len(json.dumps(output,separators=(",",":")).encode())>=6144:
        raise ValueError("system inspection must remain compact")
    return output
