"""One hourly Telegram directional watchlist for unqualified observed flow.

The digest is NOT a predictive signal. Never sends orders or a sizing hint.
Delivery state persists across timer restarts; network ambiguity is retained.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .copy_board import build_trade_flow_board, render_flow_digest
from .copy_forward import FORWARD_DB, PROTOCOL, ForwardStore, FrozenEvidence, record_hourly

DATA=Path("/home/quant/.local/share/copytrade")


def _telegram_send(*, token:str, chat_id:str, message:str)->bool:
    url=f"https://api.telegram.org/bot{token}/sendMessage"
    payload=json.dumps({
        "chat_id":chat_id,"text":message,
        "parse_mode":"HTML","disable_web_page_preview":True,
    }).encode("utf-8")
    request=urllib.request.Request(
        url,data=payload,method="POST",headers={
            "Content-Type":"application/json","User-Agent":"copytrade-flow-digest/1",
        },
    )
    with urllib.request.urlopen(request,timeout=12) as response:
        reply=json.loads(response.read().decode("utf-8"))
    return isinstance(reply,dict) and reply.get("ok") is True


def digest_period_key(now:datetime)->str:
    t=now.astimezone(UTC)
    return f"{t:%Y-%m-%dT%H}:00Z"


def publish_digest(
    *,
    journal_db:Path,
    state_db:Path,
    source_db:Path,
    now:datetime,
    token:str|None,
    chat_id:str|None,
    send:Callable[[str,str,str],bool]|None=None,
    capture_forward:bool=False,
    forward_db:Path=FORWARD_DB,
    protocol_path:Path=PROTOCOL,
    price_db:Path=DATA/"market-reference.db",
    receipt_clock:Callable[[],datetime]|None=None,
) -> dict[str,Any]:
    if now.tzinfo is None:
        raise ValueError("timezone-aware time required")
    if not journal_db.exists() or not source_db.exists():
        return {"status":"BLOCKED_NO_SOURCE"}
    if not token or not chat_id:
        return {"status":"BLOCKED_MISSING_TELEGRAM_CONFIG"}
    with closing(sqlite3.connect(f"file:{source_db}?mode=ro",uri=True)) as src:
        row=src.execute("SELECT current_seq FROM ledger_meta LIMIT 1").fetchone()
        if row is None:
            return {"status":"BLOCKED_NO_LEDGER_ID"}
        latest=int(row[0])
        source_stamp=src.execute(
            "SELECT known_at FROM outbox_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if source_stamp is None:
            return {"status":"BLOCKED_SOURCE_LEDGER_EMPTY"}
        latest_at=datetime.fromisoformat(str(source_stamp[0]))
        if latest_at.tzinfo is None or not -timedelta(minutes=2) <= (
            now.astimezone(UTC)-latest_at.astimezone(UTC)
        ) <= timedelta(minutes=5):
            return {"status":"BLOCKED_SOURCE_LEDGER_STALE"}
    with closing(sqlite3.connect(f"file:{journal_db}?mode=ro",uri=True)) as journal:
        row=journal.execute("SELECT max(last_seq) FROM opportunity_state").fetchone()
        shadow_cursor=int(row[0] or 0)
    if latest-shadow_cursor>200:
        return {"status":"BLOCKED_SHADOW_CURSOR_STALE","source_lag":latest-shadow_cursor}
    board=build_trade_flow_board(
        journal_db=journal_db,now=now,hours=4,max_coins=20,
    )
    recent_board=build_trade_flow_board(
        journal_db=journal_db,now=now,hours=1,max_coins=20,
    )
    if board["truncated"] or recent_board["truncated"]:
        return {"status":"BLOCKED_TRUNCATED_BOARD"}
    key=digest_period_key(now)
    state_db.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(state_db,timeout=10)) as state:
        state.execute("PRAGMA busy_timeout=10000")
        state.execute(
            "CREATE TABLE IF NOT EXISTS digest_delivery("
            "period TEXT PRIMARY KEY,status TEXT NOT NULL,attempt_count INTEGER NOT NULL,"
            "last_attempt_at TEXT,delivered_at TEXT)"
        )
        state.commit()
        row=state.execute(
            "SELECT status FROM digest_delivery WHERE period=?", (key,)
        ).fetchone()
        if row and row[0]=="DELIVERED":
            return {"status":"ALREADY_DELIVERED","period":key}
        with state:
            state.execute(
                "INSERT INTO digest_delivery(period,status,attempt_count,last_attempt_at) "
                "VALUES (?,'PENDING',1,?) ON CONFLICT(period) DO UPDATE "
                "SET attempt_count=attempt_count+1,last_attempt_at=excluded.last_attempt_at",
                (key,now.isoformat()),
            )
        # Freeze the as-known research hypothesis BEFORE attempting delivery.
        # Do not make the descriptive digest dependent on experimental code.
        forward_result:dict[str,Any]={"status":"DISABLED"}
        if capture_forward:
            try:
                forward_result=record_hourly(
                    frozen=FrozenEvidence(protocol_path=protocol_path),
                    forward_db=forward_db,journal_db=journal_db,
                    source_db=source_db,decided_at=now,period=key,
                    price_db=price_db,physical_clock=receipt_clock,
                )
            except (OSError,ValueError,RuntimeError,sqlite3.Error) as err:
                forward_result={"status":"RESEARCH_CAPTURE_BLOCKED",
                                "reason":type(err).__name__}
        # A period ID helps operators reconcile ambiguous Telegram retries.
        message=render_flow_digest(board,recent_board)+f"\nDigest: <code>{key}</code>"
        if forward_result["status"] in ("RECORDED_RESEARCH_ONLY","ALREADY_RECORDED"):
            from html import escape

            candidates=forward_result.get("candidate_preview",[])
            appendix=(
                "\n<b>Frozen 1h research hypotheses — NOT trading signals</b>"
                "\n" + (
                    ", ".join(
                        escape(str(coin).removeprefix(
                            "hyperliquid:mainnet:perp:default:"
                        )) + (" long" if side==1 else " short")
                        for coin,side in candidates
                    ) if candidates else "No supported directional hypothesis."
                )
                + "\nUnverified independence, execution cost, and wallet skill."
            )
            if len(message)+len(appendix) <= 3800:
                message+=appendix
            else:
                forward_result["status"]="RESEARCH_DISPLAY_CAP_EXCEEDED"
                message+="\nForward research list unavailable; no entry signal."
        elif capture_forward and forward_result["status"] != "OUTSIDE_FROZEN_HOLDOUT":
            message+="\nForward research measurement unavailable; no entry signal."
        try:
            disk=shutil.disk_usage("/")
            if disk.used/disk.total>=0.85:
                message+=(
                    f"\nOPS WARNING: VPS disk {disk.used * 100 // disk.total}% used."
                    " Keep observation archives intact; review storage capacity."
                )
        except OSError:
            pass
        sender=send or (lambda t,c,m:_telegram_send(token=t,chat_id=c,message=m))
        actual_clock=receipt_clock or (lambda:datetime.now(UTC))
        attempted=actual_clock()
        forward_cut=forward_result.get("cut_id") if forward_result["status"] in (
            "RECORDED_RESEARCH_ONLY","ALREADY_RECORDED"
        ) else None

        def record_attempt(status:str,acked:datetime|None=None)->bool:
            if not forward_cut:
                return False
            try:
                with ForwardStore(forward_db) as store:
                    store.append_delivery(
                        cut_id=str(forward_cut),attempted=attempted,
                        status=status,acknowledged=acked,
                    )
                return True
            except (sqlite3.Error,OSError,ValueError):
                return False

        try:
            success=sender(token,chat_id,message)
        except (OSError,TimeoutError,ValueError,urllib.error.URLError):
            record_attempt("UNCONFIRMED")
            return {"status":"DELIVERY_UNCONFIRMED","period":key,
                    "forward_capture":forward_result["status"]}
        if not success:
            record_attempt("REJECTED")
            return {"status":"DELIVERY_NOT_ACKNOWLEDGED","period":key,
                    "forward_capture":forward_result["status"]}
        actual_ack=actual_clock()
        recorded=record_attempt("ACKNOWLEDGED",actual_ack)
        with state:
            state.execute(
                "UPDATE digest_delivery SET status='DELIVERED',delivered_at=? "
                "WHERE period=?",(actual_ack.isoformat(),key),
            )
        return {
            "status":"DELIVERED_DESCRIPTIVE_ONLY","period":key,
            "source_lag":latest-shadow_cursor,
            "coins_in_board":board["coin_count"],
            "predictive_qualified":False,
            "forward_capture":forward_result["status"],
            "forward_ack_recorded":recorded,
        }


def main() -> None:
    parser=argparse.ArgumentParser(description="Send an hourly descriptive long/short watchlist")
    parser.add_argument("--send",action="store_true",help="Require explicit sending")
    parser.add_argument("--journal",type=Path,default=DATA/"copy-opportunities-live.db")
    parser.add_argument("--ledger",type=Path,default=DATA/"observation.db")
    parser.add_argument("--state",type=Path,default=DATA/"copy-digest-delivery.db")
    parser.add_argument("--capture-forward",action="store_true")
    parser.add_argument("--protocol",type=Path,default=PROTOCOL)
    parser.add_argument("--forward",type=Path,default=FORWARD_DB)
    parser.add_argument("--prices",type=Path,default=DATA/"market-reference.db")
    args=parser.parse_args()
    now=datetime.now(UTC)
    if not args.send:
        board=build_trade_flow_board(
            journal_db=args.journal,now=now,hours=4,max_coins=20,
        )
        recent_board=build_trade_flow_board(
            journal_db=args.journal,now=now,hours=1,max_coins=20,
        )
        print(render_flow_digest(board,recent_board))
        return
    chat=os.getenv("TELEGRAM_CHAT_ID")
    if not chat:
        chatfile=DATA/"telegram-chat-id"
        if chatfile.exists():
            chat=chatfile.read_text(encoding="utf-8").strip()
    result=publish_digest(
        journal_db=args.journal,state_db=args.state,source_db=args.ledger,
        now=now,token=os.getenv("TELEGRAM_BOT_TOKEN"),chat_id=chat,
        capture_forward=args.capture_forward,forward_db=args.forward,
        protocol_path=args.protocol,price_db=args.prices,
    )
    print(json.dumps(result,sort_keys=True))


if __name__=="__main__":
    main()
