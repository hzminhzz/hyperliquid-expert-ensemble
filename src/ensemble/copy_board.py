"""Production-safe descriptive trade-flow board from causal opportunities.

This is an evidence index, NOT a signal policy, probability forecast, order,
stop-loss recommendation, or inferred set of independent expert traders.
"""
from __future__ import annotations

import html
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_ALLOWED = {"ENTER", "INCREASE", "DE_RISK", "EXIT", "REVERSE"}


def build_trade_flow_board(
    *,
    journal_db: Path,
    now: datetime,
    hours: int = 4,
    max_source_rows: int = 5000,
    max_coins: int = 8,
) -> dict[str, Any]:
    """Aggregate only *as-known* first opportunities, never repeated fills."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("board requires a timezone-aware decision cut")
    if not 1 <= hours <= 24 or not 1 <= max_source_rows <= 20000 or not 1 <= max_coins <= 20:
        raise ValueError("board research budget exceeded")
    cut = now.astimezone(UTC)
    since = cut - timedelta(hours=hours)
    if not journal_db.exists():
        return {"status":"UNAVAILABLE","reason":"OPPORTUNITY_JOURNAL_MISSING",
                "predictive_qualified":False, "coins":[]}
    with closing(sqlite3.connect(f"file:{journal_db}?mode=ro",uri=True,timeout=10)) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute(
            "SELECT opportunity_id,instrument_id,wallet_id,first_known_at,"
            "family,source_action,copy_direction,blocker FROM opportunity_decisions "
            "WHERE first_known_at>=? AND first_known_at<=? "
            "ORDER BY first_known_at DESC LIMIT ?",
            (since.isoformat(),cut.isoformat(),max_source_rows+1),
        ).fetchall()
    truncated=len(rows)>max_source_rows
    if truncated:
        rows=rows[:max_source_rows]
    grouped:dict[str,dict[str,Any]]={}
    fresh_total=0
    risk_total=0
    for row in rows:
        family=str(row["family"])
        if family not in _ALLOWED:
            continue
        instrument=str(row["instrument_id"])
        if not instrument.startswith("hyperliquid:mainnet:perp:default:"):
            continue
        coin=instrument.rsplit(":",1)[-1]
        entry=grouped.setdefault(coin,{
            "coin":coin,"instrument_id":instrument,
            "entry_longs":set(),"entry_shorts":set(),
            "de_risk":set(),"exit":set(),
            "new_events":0,"latest_known_at":str(row["first_known_at"]),
        })
        wallet=str(row["wallet_id"]).lower()
        if family in {"ENTER","INCREASE","REVERSE"} and row["blocker"] is None:
            if row["copy_direction"] == 1:
                entry["entry_longs"].add(wallet)
            elif row["copy_direction"] == -1:
                entry["entry_shorts"].add(wallet)
            fresh_total+=1
        elif family == "DE_RISK":
            entry["de_risk"].add(wallet)
            risk_total+=1
        elif family == "EXIT":
            entry["exit"].add(wallet)
        entry["new_events"]+=1
    coins=[]
    for entry in grouped.values():
        longs=len(entry["entry_longs"])
        shorts=len(entry["entry_shorts"])
        risk=len(entry["de_risk"])
        coins.append({
            "coin":entry["coin"],
            "instrument_id":entry["instrument_id"],
            "long_entry_wallets":longs,
            "short_entry_wallets":shorts,
            "de_risk_wallets":risk,
            "exit_wallets":len(entry["exit"]),
            "new_opportunity_events":entry["new_events"],
            "latest_known_at":entry["latest_known_at"],
            "independent_cluster_count":None,
            "independence":"UNQUALIFIED_WALLET_ADDRESSES_ARE_NOT_INDEPENDENT",
            "directional_trading_signal":None,
            "expected_return":None,
        })
    coins.sort(key=lambda e:(
        -max(int(e["long_entry_wallets"] or 0),int(e["short_entry_wallets"] or 0)),
        -int(e["new_opportunity_events"] or 0),str(e["coin"])
    ))
    return {
        "schema":"copy-flow-board-v1",
        "status":"DESCRIPTIVE_ONLY_INDEPENDENCE_NOT_QUALIFIED",
        "as_of":cut.isoformat(),
        "window_start":since.isoformat(),
        "observed_opportunities":len(rows),
        "fresh_entry_events":fresh_total,
        "de_risk_events":risk_total,
        "truncated":truncated,
        "coin_count":len(coins),
        "coins":coins[:max_coins],
        "financial_execution":False,
        "predictive_qualified":False,
        "risk_size_recommendation":None,
        "limitations":[
            "UNKNOWN_WALLET_INDEPENDENCE",
            "NOT_A_PROFIT_FORECAST",
            "NO_EXECUTABLE_PRICE_OR_BROKER_FILLS",
            "PREDICTIVE_FORWARD_VALIDATION_PENDING",
        ],
    }


def render_flow_digest(board:dict[str,Any]) -> str:
    """Human Telegram HTML summary, no inferred buy/sell instruction."""
    if board.get("status") != "DESCRIPTIVE_ONLY_INDEPENDENCE_NOT_QUALIFIED":
        return "Copytrade flow observation unavailable; no actionable signals."
    hours=(
        datetime.fromisoformat(str(board["as_of"]))
        - datetime.fromisoformat(str(board["window_start"]))
    ).total_seconds()/3600
    lines=[
        "<b>Hyperliquid | Trade-flow intelligence</b>",
        "<b>DESCRIPTIVE ONLY — NOT a copy/entry signal</b>",
        (f"Window: last {hours:g}h (UTC);"
         f" {board['fresh_entry_events']} new entry/add events,"
         f" {board['de_risk_events']} de-risk events."),
    ]
    if board["truncated"]:
        lines.append("WARNING: truncated evidence window; totals incomplete.")
    if not board["coins"]:
        lines.append("No new eligible trade observations in the window.")
    for coin in board["coins"][:5]:
        ticker=html.escape(str(coin["coin"]))
        lines.append(
            f"<b>{ticker}</b>: new long addresses {coin['long_entry_wallets']}, "
            f"new short addresses {coin['short_entry_wallets']}; "
            f"de-risk {coin['de_risk_wallets']}; exits {coin['exit_wallets']}"
        )
    lines.extend([
        "Addresses are NOT validated independent trader clusters.",
        "Predictive edge: NOT QUALIFIED. Entry price/size: NOT AVAILABLE.",
        "Shadow evidence only; no order is placed by this message.",
    ])
    return "\n".join(lines)
