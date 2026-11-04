"""Lean, causally timestamped reference-price checkpoints for DI7.

The price stream is shared by event-driven copy opportunities and historical
control grids. It never creates a consensus forecast or forward-outcome row.
Immutable historical prices may be seeded from the old recorder's exact stored
known_at timestamps without reclassifying them as newly observed signals.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .research_recorder import MAX_QUOTE_AGE, PRICE_SOURCE, minute_bucket

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reference_mids (
 instrument_id TEXT NOT NULL,
 minute_utc TEXT NOT NULL,
 known_at TEXT NOT NULL,
 mid TEXT NOT NULL,
 source TEXT NOT NULL,
 PRIMARY KEY(instrument_id,minute_utc)
);
CREATE INDEX IF NOT EXISTS idx_mids_lookup
 ON reference_mids(instrument_id,known_at);
CREATE TABLE IF NOT EXISTS price_checkpoint_meta(
 key TEXT PRIMARY KEY,value TEXT NOT NULL
);
"""


class PriceCheckpointStore:
    """One price point per supported instrument/minute, never a model cut."""

    def __init__(self, path:Path)->None:
        path.parent.mkdir(parents=True,exist_ok=True)
        self.path=path
        self.db=sqlite3.connect(path,timeout=15)
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            self.db.executescript(_SCHEMA)
            row=self.db.execute(
                "SELECT value FROM price_checkpoint_meta WHERE key='role'"
            ).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO price_checkpoint_meta VALUES ('role',?)",
                    ("event_driven_reference_prices_only_v1",),
                )
            elif row[0]!="event_driven_reference_prices_only_v1":
                raise ValueError("unexpected price checkpoint store schema")

    def close(self)->None:
        self.db.close()

    def capture(
        self,
        *,
        known_at:datetime,
        prices:Mapping[str,tuple[Decimal,datetime | None]],
    )->int:
        """Keep only quotes verifiably received by the decision cutoff."""
        if known_at.tzinfo is None:
            raise ValueError("decision clock needs timezone")
        when=known_at.astimezone(UTC)
        minute=minute_bucket(when).isoformat()
        accepted=0
        with self.db:
            for instrument,(mid,received_at) in sorted(prices.items()):
                if not instrument.startswith("hyperliquid:mainnet:perp:default:"):
                    continue
                if (
                    not mid.is_finite() or mid<=0 or received_at is None
                    or received_at.tzinfo is None
                ):
                    continue
                quote_known=received_at.astimezone(UTC)
                if not timedelta() <= when-quote_known <= MAX_QUOTE_AGE:
                    continue
                accepted+=self.db.execute(
                    "INSERT OR IGNORE INTO reference_mids("
                    "instrument_id,minute_utc,known_at,mid,source) VALUES (?,?,?,?,?)",
                    (instrument,minute,quote_known.isoformat(),str(mid),PRICE_SOURCE),
                ).rowcount
        return accepted

    def stats(self)->dict[str,Any]:
        count,stamp=self.db.execute(
            "SELECT count(*),max(known_at) FROM reference_mids"
        ).fetchone()
        return {
            "status":"EVENT_DRIVEN_PRIMARY_REFERENCE_ONLY_NOT_ER4_QUALIFIED",
            "mode":"price_checkpoints_only",
            "price_rows":int(count),
            "last_price_known_at":stamp,
            "legacy_consensus_cuts_enabled":False,
            "legacy_forward_outcomes_enabled":False,
            "predictive_qualified":False,
        }


def backfill_known_prices(
    *,
    old_db:Path,
    new_db:Path,
    cutoff:datetime,
    max_rows:int=1_000_000,
)->dict[str,int|str]:
    """Copy only actual historical price receipts; does NOT synthesize observations.

    Reruns are idempotent; source is read-only and never changed. The source
    ledger, old signals and old outcomes remain unmodified.
    """
    if cutoff.tzinfo is None or not 1<=max_rows<=2_000_000:
        raise ValueError("invalid bounded historical backfill")
    store=PriceCheckpointStore(new_db)
    try:
        with closing(sqlite3.connect(f"file:{old_db}?mode=ro",uri=True,timeout=15)) as old:
            rows=old.execute(
                "SELECT instrument_id,minute_utc,known_at,mid,source "
                "FROM reference_mids WHERE minute_utc>=? "
                "ORDER BY minute_utc,instrument_id LIMIT ?",
                (cutoff.astimezone(UTC).isoformat(),max_rows+1),
            )
            copied=0
            scanned=0
            exhausted=True
            with store.db:
                for instrument,minute,known,mid,source in rows:
                    scanned+=1
                    if scanned>max_rows:
                        exhausted=False
                        break
                    # Preserve exact as-known times, no backdated predictions.
                    if not str(instrument).startswith(
                        "hyperliquid:mainnet:perp:default:"
                    ):
                        continue
                    parsed=datetime.fromisoformat(str(known))
                    if parsed.tzinfo is None or parsed.astimezone(UTC) > datetime.now(UTC):
                        continue
                    try:
                        price=Decimal(str(mid))
                    except InvalidOperation:
                        continue
                    if not price.is_finite() or price<=0:
                        continue
                    copied+=store.db.execute(
                        "INSERT OR IGNORE INTO reference_mids("
                        "instrument_id,minute_utc,known_at,mid,source) "
                        "VALUES (?,?,?,?,?)",
                        (instrument,minute,known,str(price),source),
                    ).rowcount
        if not exhausted:
            raise RuntimeError("backfill budget exceeded; rerun with a narrower cutoff")
        return {
            "status":"PRESERVED_HISTORICAL_PRICES_NOT_PROSPECTIVE_TRADES",
            "scanned":scanned,"inserted":copied,
            "price_rows":store.stats()["price_rows"],
        }
    finally:
        store.close()


def main()->None:
    p=argparse.ArgumentParser(description="Immutable bounded historical market-price seed")
    p.add_argument("command",choices=["backfill","stats"])
    p.add_argument("--source",type=Path,default=Path(
        "/home/quant/.local/share/copytrade/prospective-research.db"))
    p.add_argument("--destination",type=Path,default=Path(
        "/home/quant/.local/share/copytrade/market-reference.db"))
    p.add_argument("--hours",type=int,default=28)
    p.add_argument("--max-rows",type=int,default=800_000)
    a=p.parse_args()
    if a.command=="backfill":
        if not 1<=a.hours<=72:
            raise ValueError("backfill lookback hours limited to 1-72")
        result=backfill_known_prices(
            old_db=a.source,new_db=a.destination,
            cutoff=datetime.now(UTC)-timedelta(hours=a.hours),
            max_rows=a.max_rows,
        )
    else:
        store=PriceCheckpointStore(a.destination)
        try:
            result=store.stats()
        finally:
            store.close()
    print(json.dumps(result,indent=2,sort_keys=True))


if __name__=="__main__":
    main()
