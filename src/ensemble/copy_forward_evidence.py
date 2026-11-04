"""WP5 bounded read-only inspection and replay of frozen forward decisions.

No model execution, source mutation, Telegram delivery, protected return
inspection, or license to trade. All source evidence is referred to by IDs.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from .copy_economics import _quotes, control_directions
from .copy_forward import (
    DATA,
    FORWARD_DB,
    PROTOCOL,
    FrozenEvidence,
    build_arm_set,
    parse_utc,
    stable_hash,
)

MAX_PAGE = 100
MAX_EXPORT = 200
_SCHEMA = "wp5-forward-evidence-v1"


def _read(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"missing frozen evidence store: {path}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def list_cuts(*, frozen: FrozenEvidence, forward_db: Path = FORWARD_DB,
              after: str = "", limit: int = 30) -> dict[str, Any]:
    if not 1 <= limit <= MAX_PAGE:
        raise ValueError("bounded pagination only")
    if after and (len(after) != 17 or after[10] != "T" or not after.endswith("Z")):
        raise ValueError("period cursor must be exact RFC3339 hour key")
    if not forward_db.exists():
        return {"schema":_SCHEMA,"protocol_id":frozen.protocol_id,
                "cuts":[],"truncated":False,"next_cursor":None,
                "status":"NOT_YET_STARTED"}
    with closing(_read(forward_db)) as db:
        rows=db.execute(
            "SELECT c.period,c.cut_id,c.decision_at,c.shadow_seq,c.evidence_sha256,"
            "COUNT(a.arm_id) AS arms,"
            "SUM(CASE WHEN a.direction!=0 THEN 1 ELSE 0 END) AS candidates "
            "FROM forward_cuts c LEFT JOIN forward_arms a ON a.cut_id=c.cut_id "
            "WHERE c.protocol_id=? AND c.period>? "
            "GROUP BY c.period ORDER BY c.period LIMIT ?",
            (frozen.protocol_id, after, limit+1),
        ).fetchall()
    truncated=len(rows)>limit
    body=[
        {"period":str(r["period"]),"cut_id":str(r["cut_id"]),
         "decision_at":str(r["decision_at"]),"source_seq":int(r["shadow_seq"]),
         "evidence_sha256":str(r["evidence_sha256"]),
         "arm_count":int(r["arms"]),"candidate_count":int(r["candidates"] or 0)}
        for r in rows[:limit]
    ]
    return {"schema":_SCHEMA,"protocol_id":frozen.protocol_id,
            "cuts":body,"truncated":truncated,
            "next_cursor":body[-1]["period"] if truncated and body else None,
            "status":"READ_ONLY_SOURCE_REFERENCES",
            "outcomes_disclosed":False,
            "financial_execution":False}


def explain_arm(*, frozen:FrozenEvidence,forward_db:Path,
                arm_id:str, now:datetime|None=None) -> dict[str,Any]:
    """Exact as-known snapshot; only outcome STATUS, never returns before embargo."""
    if not arm_id or len(arm_id)>100:
        raise ValueError("exact bounded arm ID required")
    if now is not None and now.tzinfo is None:
        raise ValueError("aware inspection time required")
    with closing(_read(forward_db)) as db:
        row=db.execute(
            "SELECT a.payload_json arm,c.payload_json cut "
            "FROM forward_arms a JOIN forward_cuts c ON c.cut_id=a.cut_id "
            "WHERE a.arm_id=? AND c.protocol_id=?",
            (arm_id,frozen.protocol_id),
        ).fetchone()
        if row is None:
            return {"schema":_SCHEMA,"status":"UNKNOWN_ARM","arm_id":arm_id}
        arm=json.loads(str(row["arm"]))
        cut=json.loads(str(row["cut"]))
        deliveries=db.execute(
            "SELECT status,attempt_at,acknowledged_at FROM forward_delivery "
            "WHERE cut_id=? ORDER BY delivery_id LIMIT 16",
            (cut["cut_id"],),
        ).fetchall()
        statuses=db.execute(
            "SELECT evaluation_revision,status FROM forward_evaluations "
            "WHERE arm_id=? ORDER BY evaluation_revision LIMIT 15",
            (arm_id,),
        ).fetchall()
    return {
        "schema":_SCHEMA,
        "status":"FROZEN_AS_KNOWN_UNQUALIFIED",
        "protocol_id":frozen.protocol_id,
        "cut":cut,
        "immutable_decision":arm,
        "source_references":{
            "ledger_id":cut["source_ledger_id"],
            "ledger_read_through_seq":cut["source_ledger_seq"],
            "shadow_cursor":cut["shadow_seq"],
            "source_evidence_sha256":arm.get("source_evidence_sha256"),
        },
        "delivery_history":[dict(x) for x in deliveries[:15]],
        "delivery_truncated":len(deliveries)>15,
        "outcomes_status_only":[dict(x) for x in statuses],
        "return_values_redacted":True,
        "exit_signal":None,
        "expected_return":None,
        "risk_size":None,
        "financial_execution":False,
    }


def replay_cut(
    *, frozen:FrozenEvidence,forward_db:Path,journal_db:Path,
    price_db:Path,period:str,
) -> dict[str,Any]:
    """Recompute old as-known projections without creating or changing a cut."""
    if not forward_db.exists():
        return {"status":"NOT_YET_STARTED","period":period}
    with closing(_read(forward_db)) as db:
        record=db.execute(
            "SELECT payload_json FROM forward_cuts WHERE period=? AND protocol_id=?",
            (period,frozen.protocol_id),
        ).fetchone()
        if record is None:
            return {"status":"UNKNOWN_FROZEN_CUT","period":period}
        cut=json.loads(str(record[0]))
        stored=[
            json.loads(str(r[0])) for r in db.execute(
                "SELECT payload_json FROM forward_arms WHERE cut_id=? "
                "ORDER BY instrument_id", (cut["cut_id"],)
            )
        ]
    now=parse_utc(str(cut["decision_at"]))
    arms, evidence, _counts=build_arm_set(
        frozen=frozen,journal_db=journal_db,
        period=period,at=now,shadow_seq=int(cut["shadow_seq"]),
    )
    original={str(a["instrument_id"]):a for a in stored}
    # Independently reconstruct the pre-known baseline using read-only prices.
    with closing(_read(price_db)) as price:
        rebuilt=[]
        for arm in arms:
            if arm["instrument_id"]=="NONE":
                directions={k:None for k in frozen.comparison["baseline_families"]}
                directions["cash"]=0
            else:
                directions=control_directions(
                    known_at=now,quotes=_quotes(
                        price,instrument=str(arm["instrument_id"]),cutoff=now,
                    ),
                )
            rebuilt.append([arm["arm_id"],directions])
    baseline_hash=stable_hash(rebuilt)
    # Compare only fields emitted from the as-known source projection, not
    # fields added by the immutable frozen-market baseline decorator.
    keys=("arm_id","instrument_id","direction","disposition","reason",
          "support_long_wallets","support_short_wallets","unwind_wallets",
          "raw_source_evidence_count","source_evidence_sha256")
    matches=(
        evidence==cut["evidence_sha256"]
        and baseline_hash==cut.get("baseline_directions_sha256")
        and len(arms)==len(stored)
        and all(
            arm["instrument_id"] in original and all(
                arm.get(k)==original[arm["instrument_id"]].get(k)
                for k in keys
            ) for arm in arms
        )
    )
    return {
        "schema":_SCHEMA,"status":"REPLAY_MATCH" if matches else "REPLAY_DRIFT",
        "period":period,
        "cut_id":cut["cut_id"],"protocol_id":frozen.protocol_id,
        "arm_count":len(stored),
        "source_evidence_hash_matches":evidence==cut["evidence_sha256"],
        "baseline_direction_hash_matches":baseline_hash==cut.get(
            "baseline_directions_sha256"
        ),
        "safety":"READ_ONLY_REPLAY_NO_RECONSTRUCTED_PROSPECTIVE_SIGNALS",
        "predictive_qualified":False,
    }


def measure_storage(*,forward_db:Path=FORWARD_DB) -> dict[str,Any]:
    """Evidence retention is immutable; measure storage, never delete source."""
    paths=[
        forward_db,Path(str(forward_db)+"-wal"),
        Path(str(forward_db)+"-shm"),
    ]
    sizes={str(p.name):p.stat().st_size for p in paths if p.exists()}
    return {
        "schema":_SCHEMA,"status":"NO_AUTO_RETENTION_DELETION",
        "bytes_total":sum(sizes.values()),"files":sizes,
        "retention_policy":"KEEP_ORIGINAL_CUTS_DELIVERIES_EVALUATION_REVISIONS",
        "archive_candidate":"COPY_SEALED_EXPORT_WITH_HASH_NOT_DELETE",
        "source_history_complete":False,
    }


def export_refs(*,frozen:FrozenEvidence,forward_db:Path,output:Path,
                after:str="",limit:int=MAX_EXPORT) -> dict[str,Any]:
    """Exclusive deterministic private gzip export of source/arm references."""
    if not 1<=limit<=MAX_EXPORT or output.exists() or Path(
        str(output)+".manifest.json"
    ).exists():
        raise ValueError("invalid export budget or destination already exists")
    if output.suffix!=".gz":
        raise ValueError("bounded export requires .gz")
    if not forward_db.exists():
        raise FileNotFoundError("no frozen hourly journal to export")
    with closing(_read(forward_db)) as db:
        rows=db.execute(
            "SELECT c.payload_json FROM forward_cuts c "
            "WHERE c.protocol_id=? AND c.period>? ORDER BY c.period LIMIT ?",
            (frozen.protocol_id,after,limit+1),
        ).fetchall()
        if len(rows)>limit:
            raise ValueError("export would truncate; paginate first")
        lines=[]
        for r in rows:
            cut=json.loads(str(r[0]))
            references=[
                json.loads(str(a[0])) for a in db.execute(
                    "SELECT payload_json FROM forward_arms WHERE cut_id=? "
                    "ORDER BY instrument_id", (cut["cut_id"],)
                )
            ]
            lines.append(json.dumps(
                {"schema":_SCHEMA,"cut":cut,"arms":references,
                 "future_label_returns_included":False},
                sort_keys=True,separators=(",",":"),
            ))
    raw=("\n".join(lines)+"\n" if lines else "").encode()
    memory=io.BytesIO()
    with gzip.GzipFile(fileobj=memory,mode="wb",mtime=0,filename="") as zipped:
        zipped.write(raw)
    data=memory.getvalue()
    digest=hashlib.sha256(data).hexdigest()
    manifest={
        "schema":"wp5-sealed-reference-export-v1",
        "protocol_id":frozen.protocol_id,"sha256":digest,
        "cut_count":len(lines),"first_after":after,
        "last_period":json.loads(lines[-1])["cut"]["period"] if lines else None,
        "source_store":str(forward_db),
        "source_cut_is_immutable":True,
        "contains_returns":False,
        "financial_execution":False,
    }
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    output.parent.chmod(0o700)
    fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,"wb") as f:
            f.write(data)
        fd_manifest=os.open(str(output)+".manifest.json",
                            os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd_manifest,"wb") as f:
            f.write((json.dumps(manifest,sort_keys=True,indent=2)+"\n").encode())
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {"status":"IMMUTABLE_REFERENCE_EXPORT","output":str(output),
            "manifest_sha256":digest,"rows":len(lines),
            "truncated":False,"financial_execution":False}


def verify_export(*,output:Path) -> dict[str,Any]:
    manifest_path=Path(str(output)+".manifest.json")
    manifest=json.loads(manifest_path.read_text())
    if manifest.get("schema")!="wp5-sealed-reference-export-v1":
        raise ValueError("unknown export manifest")
    actual=hashlib.sha256(output.read_bytes()).hexdigest()
    if actual!=manifest.get("sha256"):
        raise ValueError("export byte hash mismatch")
    return {"status":"VERIFIED_SEALED_REFERENCE_BYTES",
            "protocol_id":manifest["protocol_id"],
            "sha256":actual,"rows":manifest["cut_count"],
            "contains_returns":False,"financial_execution":False}


def main() -> None:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=("list","explain","replay","storage","export","verify"))
    p.add_argument("--protocol",type=Path,default=PROTOCOL)
    p.add_argument("--forward",type=Path,default=FORWARD_DB)
    p.add_argument("--journal",type=Path,default=DATA/"copy-opportunities-live.db")
    p.add_argument("--prices",type=Path,default=DATA/"market-reference.db")
    p.add_argument("--period",type=str,default="")
    p.add_argument("--arm",type=str,default="")
    p.add_argument("--after",type=str,default="")
    p.add_argument("--limit",type=int,default=30)
    p.add_argument("--output",type=Path)
    a=p.parse_args()
    if a.command=="storage":
        value=measure_storage(forward_db=a.forward)
    elif a.command=="verify":
        if a.output is None:
            p.error("verify requires --output")
        value=verify_export(output=a.output)
    else:
        frozen=FrozenEvidence(protocol_path=a.protocol)
        if a.command=="list":
            value=list_cuts(frozen=frozen,forward_db=a.forward,
                            after=a.after,limit=a.limit)
        elif a.command=="explain":
            if not a.arm:
                p.error("explain requires --arm")
            value=explain_arm(frozen=frozen,forward_db=a.forward,
                              arm_id=a.arm)
        elif a.command=="replay":
            if not a.period:
                p.error("replay requires --period")
            value=replay_cut(frozen=frozen,forward_db=a.forward,
                             journal_db=a.journal,price_db=a.prices,
                             period=a.period)
        else:
            if a.output is None:
                p.error("export requires --output")
            value=export_refs(frozen=frozen,forward_db=a.forward,
                              output=a.output,after=a.after,limit=a.limit)
    print(json.dumps(value,sort_keys=True,indent=2))


if __name__=="__main__":
    main()
