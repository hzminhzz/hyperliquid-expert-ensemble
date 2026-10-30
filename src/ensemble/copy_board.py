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
        has_updates=db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_updates'"
        ).fetchone() is not None
        # Historical first decisions stay frozen. A separate latest-as-known
        # lifecycle projection decides whether they are still current at the cut.
        lifecycle=(
            "(SELECT u.status FROM opportunity_updates u "
            "WHERE u.opportunity_id=d.opportunity_id AND u.known_at<=? "
            "ORDER BY u.ledger_seq DESC,u.rowid DESC LIMIT 1)"
            if has_updates else "NULL"
        )
        rows=db.execute(
            "SELECT d.opportunity_id,d.instrument_id,d.wallet_id,d.first_known_at,"
            "d.family,d.source_action,d.copy_direction,d.blocker,"
            + lifecycle + " AS lifecycle_status FROM opportunity_decisions d "
            "WHERE d.first_known_at>? AND d.first_known_at<=? "
            "ORDER BY d.first_known_at DESC LIMIT ?",
            ((cut.isoformat(),) if has_updates else ())
            + (since.isoformat(),cut.isoformat(),max_source_rows+1),
        ).fetchall()
        has_barriers=db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='source_coverage_barriers'"
        ).fetchone() is not None
        latest_barrier=(
            db.execute(
                "SELECT max(known_at) FROM source_coverage_barriers WHERE known_at<=?",
                (cut.isoformat(),),
            ).fetchone()[0]
            if has_barriers else None
        )
    truncated=len(rows)>max_source_rows
    if truncated:
        rows=rows[:max_source_rows]
    grouped:dict[str,dict[str,Any]]={}
    fresh_total=0
    risk_total=0
    for row in rows:
        # A source outage cannot be used as proof that a wallet stayed long or
        # short. Suppress pre-gap votes; never fabricate an EXIT or rewrite them.
        if latest_barrier is not None and row["first_known_at"]<=latest_barrier:
            continue
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
            "seen_entry_wallets":set(),
            "de_risk":set(),"exit":set(),
            "new_events":0,"latest_known_at":str(row["first_known_at"]),
        })
        wallet=str(row["wallet_id"]).lower()
        if (family in {"ENTER","INCREASE","REVERSE"}
                and row["blocker"] is None
                and row["lifecycle_status"] not in ("INVALIDATED", "EXPIRED")):
            # Rows are newest-first. Terminal changes known before this
            # decision cut cannot count as current directional support.
            # Within the hour a wallet's latest surviving direction wins.
            if wallet not in entry["seen_entry_wallets"]:
                if row["copy_direction"] == 1:
                    entry["entry_longs"].add(wallet)
                    entry["seen_entry_wallets"].add(wallet)
                elif row["copy_direction"] == -1:
                    entry["entry_shorts"].add(wallet)
                    entry["seen_entry_wallets"].add(wallet)
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
        # Directional WATCH classification, not predictive or executable advice.
        # Require at least 3 distinct observed addresses and 2:1 dominance.
        if longs >= 3 and longs >= 2*max(1,shorts) and longs-shorts >= 2:
            bias="LONG_WATCH"
        elif shorts >= 3 and shorts >= 2*max(1,longs) and shorts-longs >= 2:
            bias="SHORT_WATCH"
        elif longs and shorts:
            bias="MIXED"
        else:
            bias="INSUFFICIENT_SUPPORT"
        leaning=bias
        # Reductions/exits have unknown signed direction. If they involve at
        # least as many distinct wallets as directional entry support, do NOT
        # present the pair as a straightforward directional watch.
        unwind_wallets=len(entry["de_risk"] | entry["exit"])
        caution=unwind_wallets >= max(longs,shorts) and unwind_wallets>0
        if caution and bias in {"LONG_WATCH","SHORT_WATCH"}:
            bias="CAUTION"
        coins.append({
            "coin":entry["coin"],
            "observed_directional_bias":bias,
            "underlying_lean":leaning,
            "risk_reduction_caution":caution,
            "reduce_or_exit_wallets":unwind_wallets,
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
        "coverage_gap_boundary":latest_barrier,
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


def render_flow_digest(
    board:dict[str,Any], recent_board:dict[str,Any]|None=None,
) -> str:
    """Hourly delivery; 4h flow context and optional 1h flow update."""
    if board.get("status") != "DESCRIPTIVE_ONLY_INDEPENDENCE_NOT_QUALIFIED":
        return "Copytrade flow observation unavailable; no actionable signals."
    from zoneinfo import ZoneInfo

    ict=ZoneInfo("Asia/Bangkok")
    end=datetime.fromisoformat(str(board["as_of"])).astimezone(ict)
    start=datetime.fromisoformat(str(board["window_start"])).astimezone(ict)
    window_hours=(end-start).total_seconds()/3600
    lines=[
        "<b>HYPERLIQUID | HOURLY DIRECTIONAL WATCH</b>",
        f"{end:%H:%M} ICT · {end:%d %b %Y}",
        "<b>DESCRIPTIVE ONLY — NOT a copy/entry signal</b>",
        f"Directional context: rolling {window_hours:g}h",
    ]
    recent_by_coin={}
    if recent_board is not None:
        recent_by_coin={c["coin"]:c for c in recent_board.get("coins",[])}
        lines.append(
            f"Last 1h: {recent_board['fresh_entry_events']} entries/adds · "
            f"{recent_board['de_risk_events']} de-risk events"
        )
    else:
        lines.append(
            f"Window: {board['fresh_entry_events']} entries/adds · "
            f"{board['de_risk_events']} de-risk events"
        )
    if board["truncated"] or (recent_board is not None and recent_board["truncated"]):
        lines.append("WARNING: truncated evidence window; totals incomplete.")
    if not board["coins"]:
        lines.append("No new eligible trade observations in the window.")
    for directions,title in (
        ({"LONG_WATCH"},"LONG WATCH — dominant observed long entries"),
        ({"SHORT_WATCH"},"SHORT WATCH — dominant observed short entries"),
        ({"MIXED","CAUTION"},"CAUTION / MIXED — conflicting or unwinding"),
    ):
        selection=[coin for coin in board["coins"]
                   if coin.get("observed_directional_bias") in directions][:3]
        lines.append(f"\n<b>{title}</b>")
        if not selection:
            lines.append("None met the descriptive threshold.")
        for coin in selection:
            ticker=html.escape(str(coin["coin"]))
            lean=coin.get("underlying_lean","")
            caution=f" ({lean.lower().replace('_watch','')}-leaning)" if coin[
                "observed_directional_bias"
            ]=="CAUTION" else ""
            recent=recent_by_coin.get(coin["coin"])
            hour_detail=(
                f"; last 1h {recent['long_entry_wallets']}L/"
                f"{recent['short_entry_wallets']}S"
                if recent is not None else ("; last 1h 0L/0S" if recent_board else "")
            )
            lines.append(
                f"• {ticker}{caution}: {coin['long_entry_wallets']}L/"
                f"{coin['short_entry_wallets']}S addresses (4h); "
                f"reduce {coin['de_risk_wallets']}, exits {coin['exit_wallets']}"
                f"{hour_detail}"
            )
    lines.extend([
        "\nWatch threshold: 3+ addresses, at least 2:1 directional imbalance.",
        "Addresses are NOT validated independent trader clusters.",
        "Predictive edge: NOT QUALIFIED. Entry price/size: NOT AVAILABLE.",
        "Shadow evidence only; no order is placed by this message.",
    ])
    return "\n".join(lines)
