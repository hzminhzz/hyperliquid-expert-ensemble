"""Append-only, as-known research cuts and future reference-price markouts.

This records *descriptive V1 cluster consensus*, not ER4 R0/R1/R2 model trials.
Price source is Hyperliquid allMids received by this process (no exchange
timestamp). An absent/stale quote is missing, never imputed from entry price.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

HORIZONS: tuple[tuple[str, timedelta], ...] = (
    ("1m", timedelta(minutes=1)),
    ("5m", timedelta(minutes=5)),
    ("15m", timedelta(minutes=15)),
    ("1h", timedelta(hours=1)),
    ("4h", timedelta(hours=4)),
    ("24h", timedelta(hours=24)),
)
MODEL_ID = "descriptive_cluster_consensus_v1_not_er4"
PRICE_SOURCE = "hyperliquid_info_allMids_received_at"
OUTCOME_MEASURE = "mid_to_mid_hypothetical_not_executable_pnl"
MAX_QUOTE_AGE = timedelta(seconds=30)
MAX_OUTCOME_LATENESS = timedelta(seconds=90)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS signal_cuts (
    cut_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    minute_utc TEXT NOT NULL,
    known_at TEXT NOT NULL,
    ledger_id TEXT NOT NULL,
    ledger_seq INTEGER NOT NULL,
    universe_revision TEXT NOT NULL,
    model_id TEXT NOT NULL,
    consensus TEXT NOT NULL,
    lower_bound TEXT NOT NULL,
    upper_bound TEXT NOT NULL,
    direction INTEGER NOT NULL,
    grade TEXT NOT NULL,
    coverage TEXT NOT NULL,
    supporting_clusters INTEGER NOT NULL,
    opposing_clusters INTEGER NOT NULL,
    flat_clusters INTEGER NOT NULL,
    price_mid TEXT,
    price_known_at TEXT,
    price_source TEXT NOT NULL,
    UNIQUE (instrument_id, minute_utc)
);
CREATE INDEX IF NOT EXISTS idx_cuts_time ON signal_cuts(known_at);
CREATE TABLE IF NOT EXISTS reference_mids (
    instrument_id TEXT NOT NULL,
    minute_utc TEXT NOT NULL,
    known_at TEXT NOT NULL,
    mid TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY(instrument_id, minute_utc)
);
CREATE INDEX IF NOT EXISTS idx_mids_lookup
ON reference_mids(instrument_id, known_at);
CREATE TABLE IF NOT EXISTS forward_outcomes (
    cut_id TEXT NOT NULL REFERENCES signal_cuts(cut_id),
    horizon TEXT NOT NULL,
    due_at TEXT NOT NULL,
    settled_at TEXT NOT NULL,
    status TEXT NOT NULL,
    price_known_at TEXT,
    future_mid TEXT,
    market_return TEXT,
    directional_gross TEXT,
    net_after_assumed_cost TEXT,
    assumed_round_trip_cost_bps TEXT NOT NULL,
    measure TEXT NOT NULL,
    PRIMARY KEY(cut_id, horizon)
);
CREATE INDEX IF NOT EXISTS idx_outcome_horizon_status
ON forward_outcomes(horizon, status);
"""


def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("research timestamps must be timezone-aware")
    return dt.astimezone(UTC)


def minute_bucket(dt: datetime) -> datetime:
    return utc(dt).replace(second=0, microsecond=0)


@dataclass(slots=True, frozen=True)
class CutInput:
    instrument_id: str
    consensus: Decimal
    lower_bound: Decimal
    upper_bound: Decimal
    direction: int
    grade: str
    coverage: Decimal
    supporting_clusters: int
    opposing_clusters: int
    flat_clusters: int
    mid: Decimal | None
    mid_known_at: datetime | None


class ResearchRecorder:
    """Single-writer WAL store. Existing cuts and outcome labels are immutable."""

    def __init__(self, path: Path, *, assumed_cost_bps: Decimal = Decimal(10)) -> None:
        if assumed_cost_bps < 0:
            raise ValueError("assumed cost must be non-negative")
        self.path = path
        self.assumed_cost_bps = assumed_cost_bps
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def capture(
        self,
        *,
        decision_at: datetime,
        ledger_id: str,
        ledger_seq: int,
        universe_revision: str,
        cuts: Sequence[CutInput],
    ) -> int:
        known_at = utc(decision_at)
        bucket = minute_bucket(known_at).isoformat()
        inserted = 0
        with self.conn:
            for cut in cuts:
                if cut.direction not in (-1, 0, 1):
                    raise ValueError("invalid directional sign")
                eligible_quote = (
                    cut.mid is not None
                    and cut.mid > 0
                    and cut.mid_known_at is not None
                    and timedelta(0)
                    <= known_at - utc(cut.mid_known_at)
                    <= MAX_QUOTE_AGE
                )
                mid = str(cut.mid) if eligible_quote else None
                mid_known = (
                    utc(cut.mid_known_at).isoformat()
                    if eligible_quote and cut.mid_known_at is not None
                    else None
                )
                identity = (
                    f"{MODEL_ID}|{cut.instrument_id}|{bucket}|"
                    f"{ledger_id}|{ledger_seq}|{universe_revision}"
                )
                cut_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

                changed = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO signal_cuts(
                        cut_id,instrument_id,minute_utc,known_at,ledger_id,ledger_seq,
                        universe_revision,model_id,consensus,lower_bound,upper_bound,
                        direction,grade,coverage,supporting_clusters,opposing_clusters,
                        flat_clusters,price_mid,price_known_at,price_source
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        cut_id,
                        cut.instrument_id,
                        bucket,
                        known_at.isoformat(),
                        ledger_id,
                        ledger_seq,
                        universe_revision,
                        MODEL_ID,
                        str(cut.consensus),
                        str(cut.lower_bound),
                        str(cut.upper_bound),
                        cut.direction,
                        cut.grade,
                        str(cut.coverage),
                        cut.supporting_clusters,
                        cut.opposing_clusters,
                        cut.flat_clusters,
                        mid,
                        mid_known,
                        PRICE_SOURCE,
                    ),
                ).rowcount
                inserted += changed
                if eligible_quote and mid is not None and mid_known is not None:
                    self.conn.execute(
                        """
                        INSERT OR IGNORE INTO reference_mids(
                            instrument_id,minute_utc,known_at,mid,source
                        ) VALUES(?,?,?,?,?)
                        """,
                        (cut.instrument_id, bucket, mid_known, mid, PRICE_SOURCE),
                    )
        return inserted

    def settle_due(
        self,
        *,
        now: datetime,
        max_rows_per_horizon: int = 2000,
    ) -> dict[str, int]:
        """Attach only observed, causally available future prices.

        Mature rows become final only after the tolerance window closes.
        A missing price is CENSORED, not fabricated or retroactively repaired.
        """
        as_known = utc(now)
        totals: dict[str, int] = {}
        with self.conn:
            for label, delay in HORIZONS:
                ready_before = (
                    as_known - delay - MAX_OUTCOME_LATENESS
                ).isoformat()
                rows = self.conn.execute(
                    """
                    SELECT c.cut_id,c.instrument_id,c.known_at,
                           c.price_mid,c.direction
                    FROM signal_cuts AS c
                    WHERE c.known_at <= ?
                      AND NOT EXISTS (
                        SELECT 1 FROM forward_outcomes AS o
                        WHERE o.cut_id=c.cut_id AND o.horizon=?
                      )
                    ORDER BY c.known_at,c.instrument_id
                    LIMIT ?
                    """,
                    (ready_before, label, max_rows_per_horizon),
                ).fetchall()
                saved = 0
                for cut_id, instrument_id, origin_iso, initial, direction in rows:
                    due = datetime.fromisoformat(origin_iso) + delay
                    end = due + MAX_OUTCOME_LATENESS
                    future = self.conn.execute(
                        """
                        SELECT mid,known_at FROM reference_mids
                        WHERE instrument_id=?
                          AND known_at >= ? AND known_at <= ?
                        ORDER BY known_at ASC LIMIT 1
                        """,
                        (instrument_id, due.isoformat(), end.isoformat()),
                    ).fetchone()
                    status = "VALID" if initial is not None and future else "CENSORED"
                    price_known = future[1] if future else None
                    exit_mid = future[0] if future else None
                    market_ret = None
                    directional_gross = None
                    net = None
                    if status == "VALID" and exit_mid is not None:
                        base = Decimal(initial)
                        market = (Decimal(exit_mid) - base) / base
                        market_ret = str(market)
                        if direction != 0:
                            directional = Decimal(direction) * market
                            directional_gross = str(directional)
                            net = str(
                                directional
                                - self.assumed_cost_bps / Decimal(10_000)
                            )
                    self.conn.execute(
                        """
                        INSERT INTO forward_outcomes(
                            cut_id,horizon,due_at,settled_at,status,price_known_at,
                            future_mid,market_return,directional_gross,
                            net_after_assumed_cost,assumed_round_trip_cost_bps,measure
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            cut_id,
                            label,
                            due.isoformat(),
                            as_known.isoformat(),
                            status,
                            price_known,
                            exit_mid,
                            market_ret,
                            directional_gross,
                            net,
                            str(self.assumed_cost_bps),
                            OUTCOME_MEASURE,
                        ),
                    )
                    saved += 1
                totals[label] = saved
        return totals

    def stats(self) -> dict[str, object]:
        cut_count, priced_count, last_cut = self.conn.execute(
            """
            SELECT COUNT(*),SUM(CASE WHEN price_mid IS NOT NULL THEN 1 ELSE 0 END),
                   MAX(known_at)
            FROM signal_cuts
            """
        ).fetchone()
        prices = self.conn.execute(
            "SELECT COUNT(*) FROM reference_mids"
        ).fetchone()[0]
        records = self.conn.execute(
            "SELECT horizon,status,COUNT(*) FROM forward_outcomes GROUP BY horizon,status"
        ).fetchall()
        return {
            "model_id": MODEL_ID,
            "cut_rows": cut_count,
            "priced_cut_rows": priced_count or 0,
            "price_rows": prices,
            "last_cut_known_at": last_cut,
            "outcomes": {
                f"{horizon}:{status}": number
                for horizon, status, number in records
            },
            "assumed_cost_bps": str(self.assumed_cost_bps),
            "outcome_measure": OUTCOME_MEASURE,
            "status": "DESCRIPTIVE_ONLY_NOT_ER4_QUALIFIED",
        }
