"""Read-only DI6 decision capsules and agent-friendly cursor views.

Return bounded, causally segmented facts, not a buy/short recommendation.
No outcome labels are included in the immutable decision section.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any


def _open(path:Path)->sqlite3.Connection:
    connection=sqlite3.connect(f"file:{path}?mode=ro",uri=True,timeout=10)
    connection.row_factory=sqlite3.Row
    return connection


def explain_copy_decision(
    *,
    journal_db:Path,
    outcome_db:Path,
    opportunity_id:str,
) -> dict[str,Any]:
    """Frozen source cut and separate postdecision labels/controls."""
    if not opportunity_id.startswith("copy:") or len(opportunity_id)>120:
        raise ValueError("invalid opportunity identity")
    with closing(_open(journal_db)) as journal:
        row=journal.execute(
            "SELECT payload_json FROM opportunity_decisions WHERE opportunity_id=?",
            (opportunity_id,),
        ).fetchone()
        if not row:
            return {"status":"NOT_FOUND","id":opportunity_id}
        cut=json.loads(str(row[0]))
        changes=[
            {"known_at":str(r["known_at"]),"action":str(r["source_action"]),
             "transition":str(r["status"]),"reason":r["reason"]}
            for r in journal.execute(
                "SELECT known_at,source_action,status,reason FROM opportunity_updates "
                "WHERE opportunity_id=? ORDER BY ledger_seq LIMIT 25",
                (opportunity_id,),
            )
        ]
    outcomes=[]
    if outcome_db.exists():
        with closing(_open(outcome_db)) as research:
            rows=research.execute(
                "SELECT horizon,status,value_json FROM copy_markouts "
                "WHERE opportunity_id=? ORDER BY horizon",
                (opportunity_id,),
            ).fetchall()
            for row in rows:
                label=json.loads(str(row["value_json"]))
                controls=[
                    json.loads(str(r[0]))
                    for r in research.execute(
                        "SELECT value_json FROM copy_controls "
                        "WHERE opportunity_id=? AND horizon=?",
                        (opportunity_id,str(row["horizon"])),
                    )
                ]
                outcomes.append({
                    "horizon":str(row["horizon"]),"status":str(row["status"]),
                    "hypothetical_mid_net_return":label.get("hypothetical_net_return"),
                    "price_model":label.get("price_model"),
                    "assumed_cost_bps":label.get("cost_bps"),
                    "paired_market_controls":controls,
                })
    return {
        "schema":"CopyDecisionCapsule-v1",
        "status":"FOUND_SHADOW_ONLY",
        "immutable_as_known_decision":cut,
        "subsequent_source_transitions":changes,
        "separately_matured_outcomes":outcomes,
        "independent_wallet_support":"UNKNOWN",
        "source_execution_fill":"NOT_OBSERVED",
        "market_only_incremental_edge":"NOT_QUALIFIED",
        "trade_entry_recommendation":None,
        "position_size_recommendation":None,
        "safety":["NO_ORDERS","NOT_A_QUALIFIED_PREDICTIVE_SIGNAL"],
    }


def inspect_changes(
    *,
    journal_db:Path,
    since_seq:int,
    limit:int=40,
) -> dict[str,Any]:
    if since_seq<0 or not 1<=limit<=100:
        raise ValueError("invalid cursor/limit")
    with closing(_open(journal_db)) as journal:
        current=journal.execute(
            "SELECT max(last_seq) FROM opportunity_state"
        ).fetchone()[0]
        items=[
            {"opportunity_id":r["opportunity_id"],"wallet_id":r["wallet_id"],
             "instrument_id":r["instrument_id"],"source_action":r["source_action"],
             "family":r["family"],"direction":r["copy_direction"],
             "first_known_at":r["first_known_at"],"seq":int(r["first_ledger_seq"])}
            for r in journal.execute(
                "SELECT opportunity_id,wallet_id,instrument_id,source_action,family,"
                "copy_direction,first_known_at,first_ledger_seq "
                "FROM opportunity_decisions WHERE first_ledger_seq>? "
                "ORDER BY first_ledger_seq LIMIT ?",
                (since_seq,limit),
            )
        ]
        next_seq=int(items[-1]["seq"]) if items else since_seq
        has_more=journal.execute(
            "SELECT 1 FROM opportunity_decisions WHERE first_ledger_seq>? LIMIT 1",
            (next_seq,),
        ).fetchone() is not None
    return {
        "mode":"SHADOW_OBSERVATIONS_ONLY","cursor":int(current or 0),
        "since_seq":since_seq,"next_seq":next_seq,"more":has_more,
        "changes":items,"predictive_qualified":False,
    }


def inspect_wallet(
    *,
    journal_db:Path,
    wallet_id:str,
    limit:int=100,
) -> dict[str,Any]:
    if not wallet_id or len(wallet_id)>128 or not 1<=limit<=500:
        raise ValueError("invalid wallet inspection parameters")
    with closing(_open(journal_db)) as journal:
        records=journal.execute(
            "SELECT family,instrument_id,first_known_at,source_action "
            "FROM opportunity_decisions WHERE wallet_id=? "
            "ORDER BY first_ledger_seq DESC LIMIT ?",
            (wallet_id.lower(),limit),
        ).fetchall()
    if not records:
        return {"status":"NO_OBSERVED_OPPORTUNITIES","wallet_id":wallet_id.lower()}
    families=Counter(str(r["family"]) for r in records)
    instruments=Counter(str(r["instrument_id"]).rsplit(":",1)[-1] for r in records)
    return {
        "status":"DESCRIPTIVE_WALLET_BEHAVIOR_NOT_SKILL",
        "wallet_id":wallet_id.lower(),"observed_decisions":len(records),
        "sample_limit":limit,"families":dict(families),
        "most_observed_coins":instruments.most_common(8),
        "last_known_at":str(records[0]["first_known_at"]),
        "lagged_independent_skill":None,
        "profitability_qualified":False,
    }
