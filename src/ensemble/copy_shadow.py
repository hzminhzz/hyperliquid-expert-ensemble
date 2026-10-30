"""DI1-DI4 bounded, shadow-only recorder and research command.

Runs on independent SQLite files and never writes into the Rust source ledger,
sends Telegram notifications, or places orders. Retrospective research and
newly activated prospective collection are explicitly distinct.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import zip_longest
from pathlib import Path
from typing import Any

from .copy_opportunities import (
    OpportunityDecision,
    OpportunityJournal,
    OpportunityStatus,
    TradeTransition,
)
from .copy_research import (
    DEFAULT_HORIZONS,
    CopyResearchStore,
    PairedControl,
    RecordedMid,
    copy_markout,
    frozen_control_revision,
)


@dataclass(frozen=True,slots=True)
class ShadowSettings:
    ledger_db: Path
    journal_db: Path
    price_db: Path | None = None
    outcome_db: Path | None = None
    batch_size: int = 500
    max_batches: int = 10
    retrospective: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.batch_size <= 5000 or not 1 <= self.max_batches <= 100:
            raise ValueError("unbounded shadow research budget")


class ShadowConsumer:
    def __init__(self,settings:ShadowSettings) -> None:
        self.settings=settings
        self.ledger=sqlite3.connect(
            f"file:{settings.ledger_db}?mode=ro",uri=True,timeout=15,
        )
        self.ledger.row_factory=sqlite3.Row
        self.journal=OpportunityJournal(settings.journal_db)
        meta=self.ledger.execute(
            "SELECT ledger_id,current_seq FROM ledger_meta LIMIT 1"
        ).fetchone()
        if meta is None:
            raise ValueError("source ledger missing")
        self.ledger_id=str(meta["ledger_id"])
        mode=("RETROSPECTIVE_AS_KNOWN_NOT_PROSPECTIVE"
              if settings.retrospective else "PROSPECTIVE_SHADOW_ONLY")
        with self.journal.db:
            self.journal.db.execute(
                "CREATE TABLE IF NOT EXISTS journal_modes("
                "ledger_id TEXT PRIMARY KEY,mode TEXT NOT NULL)"
            )
        existing=self.journal.db.execute(
            "SELECT mode FROM journal_modes WHERE ledger_id=?",(self.ledger_id,)
        ).fetchone()
        if existing is not None and str(existing[0])!=mode:
            self.close()
            raise RuntimeError(
                "opportunity journal mode mismatch: cannot relabel historical data"
            )
        with self.journal.db:
            if existing is None:
                self.journal.db.execute(
                    "INSERT INTO journal_modes VALUES (?,?)",(self.ledger_id,mode)
                )
            if self.journal.cursor(self.ledger_id)==0 and not settings.retrospective:
                self.journal.seal_cursor(self.ledger_id,int(meta["current_seq"]))

    def close(self) -> None:
        self.ledger.close()
        self.journal.close()

    def scan(self) -> dict[str,Any]:
        seen=created=skipped=0
        cursor=self.journal.cursor(self.ledger_id)
        for _ in range(self.settings.max_batches):
            rows=self.ledger.execute(
                "SELECT seq,event_id,kind,instrument_id,wallet_id,event_time,known_at,"
                "quality,payload FROM outbox_events WHERE seq>? ORDER BY seq LIMIT ?",
                (cursor,self.settings.batch_size),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                seq=int(row["seq"])
                if seq!=cursor+1:
                    raise ValueError("source cursor gap; no reliable opportunity interpretation")
                if (row["kind"]=="COVERAGE_CHANGE"
                        and str(row["quality"]).lower() != "valid"):
                    payload=json.loads(str(row["payload"]))
                    if not isinstance(payload,dict):
                        raise ValueError("invalid source gap payload")
                    self.journal.record_source_gap(
                        ledger_id=self.ledger_id,ledger_seq=seq,
                        source_id=str(row["event_id"]),
                        known_at=datetime.fromisoformat(str(row["known_at"])),
                        reason=str(payload.get("status") or "SOURCE_COVERAGE_DEGRADED"),
                    )
                    skipped+=1
                elif row["kind"]!="POSITION_CHANGE":
                    with self.journal.db:
                        self.journal.seal_cursor(self.ledger_id,seq)
                    skipped+=1
                else:
                    source=TradeTransition.from_ledger(row,ledger_id=self.ledger_id)
                    if source is None:
                        raise ValueError("unexpected transition parser result")
                    observed=source.known_at if self.settings.retrospective else datetime.now(UTC)
                    if self.journal.observe(source,observation_at=observed) is not None:
                        created+=1
                cursor=seq
                seen+=1
            if len(rows)<self.settings.batch_size:
                break
        return {
            "mode":"RETROSPECTIVE_AS_KNOWN_NOT_PROSPECTIVE"
            if self.settings.retrospective else "PROSPECTIVE_SHADOW_ONLY",
            "ledger_id":self.ledger_id,"cursor":cursor,"processed":seen,
            "new_opportunities":created,"nontrade_events":skipped,
            "financial_execution":False,
        }


def load_decision(payload: str) -> OpportunityDecision:
    data=json.loads(payload)
    for k in ("first_event_at","first_known_at","earliest_actionable_at"):
        data[k]=datetime.fromisoformat(data[k])
    for k in ("source_quantity_before","source_quantity_after","source_price"):
        data[k]=Decimal(data[k])
    data["status"]=OpportunityStatus(data["status"])
    return OpportunityDecision(**data)


def price_window(
    prices:sqlite3.Connection, instrument:str, ready_at:datetime,
) -> list[RecordedMid]:
    start=(ready_at-timedelta(hours=2)).isoformat()
    end=(ready_at+timedelta(hours=24,minutes=3)).isoformat()
    # Bounded point retrieval uses the existing unique instrument/minute key.
    rows=prices.execute(
        "SELECT known_at,mid FROM reference_mids WHERE instrument_id=? "
        "AND minute_utc BETWEEN ? AND ? ORDER BY minute_utc LIMIT 1620",
        (instrument,start,end),
    ).fetchall()
    return [
        RecordedMid(instrument,datetime.fromisoformat(str(r[0])),Decimal(str(r[1])))
        for r in rows
    ]


def momentum_control(
    opportunity:OpportunityDecision,
    outcome_horizon:str,
    mids:list[RecordedMid],
    entry_at:datetime,
    exit_at:datetime,
    *,
    cost_bps:Decimal,
) -> PairedControl | None:
    cutoff=opportunity.first_known_at
    t0=cutoff-timedelta(hours=1)
    previous=[p for p in mids if t0-timedelta(seconds=90)<=p.known_at<=t0]
    recent=[p for p in mids if p.known_at<=cutoff]
    entry=[p for p in mids if p.known_at==entry_at]
    exit_price=[p for p in mids if p.known_at==exit_at]
    if not (previous and recent and entry and exit_price):
        return None
    p0=max(previous,key=lambda p:p.known_at).price
    pt=max(recent,key=lambda p:p.known_at).price
    direction=1 if pt>p0 else -1 if pt<p0 else 0
    r=Decimal(direction)*(exit_price[0].price/entry[0].price-1)
    if direction:
        r-=cost_bps/Decimal(10000)
    return PairedControl(
        opportunity_id=opportunity.opportunity_id,
        horizon=outcome_horizon,
        market_only_net_return=r,
        control_family="same_coin_lagged_1h_momentum",
        control_revision=frozen_control_revision(
            "same_coin_lagged_1h_momentum",
            {"lookback_s":3600,"max_quote_lateness_s":90,"cost_bps":str(cost_bps)},
        ),
        eligible_at=cutoff,
    )


def evaluate(
    settings:ShadowSettings, *, now:datetime | None = None,
    max_opportunities:int=25, cost_bps:Decimal=Decimal(10),
    since:datetime|None=None,
) -> dict[str,Any]:
    if settings.price_db is None or settings.outcome_db is None:
        raise ValueError("price and research stores required")
    if not 1<=max_opportunities<=500:
        raise ValueError("opportunity budget exceeded")
    now=now or datetime.now(UTC)
    journal=OpportunityJournal(settings.journal_db)
    research=CopyResearchStore(settings.outcome_db)
    prices=sqlite3.connect(f"file:{settings.price_db}?mode=ro",uri=True,timeout=15)
    result={"inspected":0,"valid_hypothetical":0,"censored":0,"pending":0,
            "no_copy":0,"paired_controls":0}
    try:
        # Fairly select due horizons, not the oldest incomplete opportunities:
        # an immature 24h label must never hide a newer, mature 5m/15m/1h label.
        # Each queue is bounded and read-only; no history or source cursor changes.
        research.db.execute("ATTACH DATABASE ? AS opportunity_source", (str(settings.journal_db),))
        queues=[]
        for horizon,delay in DEFAULT_HORIZONS:
            ready_cutoff=(now-delay-timedelta(seconds=90)).isoformat()
            # Preserve old backlog progress while reserving space for newly
            # mature actionable entries during a burst or after an outage.
            for recent in (True,False):
                direction="DESC" if recent else "ASC"
                actionable="AND d.copy_direction IN (-1,1) AND d.blocker IS NULL " if recent else ""
                queues.append(research.db.execute(
                    "SELECT d.opportunity_id,d.payload_json "
                    "FROM opportunity_source.opportunity_decisions d "
                    "WHERE d.first_known_at >= ? AND d.copy_direction IN (-1,1) "
                    "AND (d.blocker IS NOT NULL OR d.earliest_actionable_at <= ?) "
                    + actionable
                    + "AND NOT EXISTS (SELECT 1 FROM copy_markouts m "
                    "WHERE m.opportunity_id=d.opportunity_id AND m.horizon=?) "
                    + f"ORDER BY d.first_known_at {direction},d.first_ledger_seq {direction} LIMIT ?",
                    ((since or datetime.min.replace(tzinfo=UTC)).isoformat(),
                     ready_cutoff,horizon,max_opportunities),
                ).fetchall())
        rows=[]
        selected=set()
        for round_rows in zip_longest(*queues):
            for row in round_rows:
                if row is None or row["opportunity_id"] in selected:
                    continue
                selected.add(row["opportunity_id"])
                rows.append(row)
                if len(rows)==max_opportunities:
                    break
            if len(rows)==max_opportunities:
                break
        for row in rows:
            decision=load_decision(str(row["payload_json"]))
            mids=price_window(prices,decision.instrument_id,decision.earliest_actionable_at)
            result["inspected"]+=1
            for horizon,_ in DEFAULT_HORIZONS:
                if research.db.execute(
                    "SELECT 1 FROM copy_markouts WHERE opportunity_id=? AND horizon=?",
                    (decision.opportunity_id,horizon),
                ).fetchone():
                    continue
                outcome=copy_markout(
                    decision,horizon,mids,cost_bps=cost_bps,
                    max_quote_lateness=timedelta(seconds=90),now=now,
                )
                if outcome.status=="PENDING":
                    result["pending"]+=1
                    continue
                if research.record(outcome,recorded_at=now):
                    if outcome.status=="VALID_HYPOTHETICAL":
                        result["valid_hypothetical"]+=1
                    elif outcome.status=="CENSORED":
                        result["censored"]+=1
                    else:
                        result["no_copy"]+=1
                if outcome.status=="VALID_HYPOTHETICAL" and outcome.entry_at and outcome.exit_at:
                    baseline=momentum_control(
                        decision,horizon,mids,outcome.entry_at,outcome.exit_at,
                        cost_bps=cost_bps,
                    )
                    if baseline and research.attach_control(baseline):
                        result["paired_controls"]+=1
        result["status"]="RESEARCH_DIAGNOSTIC_ONLY_NOT_QUALIFIED"
        return result
    finally:
        prices.close()
        research.close()
        journal.close()


def main(argv: list[str] | None = None) -> None:
    p=argparse.ArgumentParser(description="Copytrade shadow opportunity pipeline")
    p.add_argument("command",choices=("scan","evaluate","inspect","health","study","explain","board","wallet","changes","export","verify"))
    p.add_argument("--ledger",type=Path,default=Path("/home/quant/.local/share/copytrade/observation.db"))
    p.add_argument("--journal",type=Path,default=Path("/home/quant/.local/share/copytrade/copy-opportunities-live.db"))
    p.add_argument("--prices",type=Path,default=Path("/home/quant/.local/share/copytrade/market-reference.db"))
    p.add_argument("--outcomes",type=Path,default=Path("/home/quant/.local/share/copytrade/copy-outcomes-live.db"))
    p.add_argument("--batch",type=int,default=500)
    p.add_argument("--batches",type=int,default=10)
    p.add_argument("--limit",type=int,default=25)
    p.add_argument("--retrospective",action="store_true")
    p.add_argument("--id",type=str,default=None,help="Immutable opportunity ID")
    p.add_argument("--output",type=Path,default=None)
    p.add_argument("--format",choices=("ndjson.gz","parquet"),default="ndjson.gz")
    p.add_argument("--first-seq",type=int,default=0)
    p.add_argument("--horizon",type=str,default="1h")
    p.add_argument("--hours",type=int,default=4)
    p.add_argument("--wallet",type=str,default=None)
    p.add_argument("--since-seq",type=int,default=0)
    p.add_argument("--control-revision",type=str,default=None)
    p.add_argument("--since",type=str,default=None,
                   help="Earliest opportunity knowledge timestamp for bounded evaluation")
    args=p.parse_args(argv)
    settings=ShadowSettings(
        args.ledger,args.journal,args.prices,args.outcomes,
        args.batch,args.batches,args.retrospective,
    )
    if args.command=="scan":
        runner=ShadowConsumer(settings)
        try:
            output=runner.scan()
        finally:
            runner.close()
    elif args.command=="evaluate":
        output=evaluate(
            settings,max_opportunities=args.limit,
            since=datetime.fromisoformat(args.since) if args.since else None,
        )
    elif args.command=="study":
        from .copy_study import evaluate_incremental_edge

        output=evaluate_incremental_edge(
            opportunity_db=args.journal,outcome_db=args.outcomes,
            horizon=args.horizon,
            control_revision=args.control_revision or frozen_control_revision(
                "same_coin_lagged_1h_momentum",
                {"lookback_s":3600,"max_quote_lateness_s":90,"cost_bps":"10"},
            ),
        )
    elif args.command=="explain":
        from .copy_agent import explain_copy_decision

        if not args.id:
            raise ValueError("explain requires --id")
        output=explain_copy_decision(
            journal_db=args.journal,outcome_db=args.outcomes,
            opportunity_id=args.id,
        )
    elif args.command=="board":
        from .copy_board import build_trade_flow_board

        output=build_trade_flow_board(
            journal_db=args.journal,now=datetime.now(UTC),hours=args.hours,
            max_coins=min(args.limit,20),
        )
    elif args.command=="wallet":
        from .copy_agent import inspect_wallet

        if not args.wallet:
            raise ValueError("wallet requires --wallet")
        output=inspect_wallet(journal_db=args.journal,wallet_id=args.wallet,limit=args.limit)
    elif args.command=="changes":
        from .copy_agent import inspect_changes

        output=inspect_changes(
            journal_db=args.journal,since_seq=args.since_seq,limit=args.limit,
        )
    elif args.command=="export":
        from .copy_export import export_opportunity_partition

        if args.output is None:
            raise ValueError("export requires --output")
        output=export_opportunity_partition(
            source_db=args.journal,output=args.output,
            first_seq=args.first_seq,limit=min(args.limit,100_000),format=args.format,
        )
    elif args.command=="verify":
        from .copy_export import verify_opportunity_partition

        if args.output is None:
            raise ValueError("verify requires --output")
        output=verify_opportunity_partition(args.output)
    elif args.command=="health":
        from .copy_health import inspect_integrity

        output=inspect_integrity(
            ledger_db=args.ledger, research_db=args.prices,
            opportunity_db=args.journal,outcome_db=args.outcomes,
            health_path=Path("/home/quant/.local/share/copytrade/ensemble-health.json"),
            inspect_systemd=True,
        )
    else:
        journal=OpportunityJournal(settings.journal_db)
        try:
            output={"recent":journal.recent(args.limit),"stats":journal.stats()}
        finally:
            journal.close()
    print(json.dumps(output,indent=2,sort_keys=True))


if __name__=="__main__":
    main()
