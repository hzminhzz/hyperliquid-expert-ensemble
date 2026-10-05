"""Transactional projection store and consumer offsets (Contracts C2, C4).

Maintains an independent SQLite database for expert ensemble projections,
tracking consumer offsets against the Rust observation ledger.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS consumer_offsets (
    ledger_id          TEXT    PRIMARY KEY,
    last_seq           INTEGER NOT NULL DEFAULT 0,
    updated_at         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS projected_positions (
    wallet_id          TEXT    NOT NULL,
    coin               TEXT    NOT NULL,
    szi                TEXT    NOT NULL,
    direction          TEXT    NOT NULL,
    avg_entry          TEXT    NOT NULL,
    opened_at          TEXT    NOT NULL,
    last_added_at      TEXT    NOT NULL,
    realized_pnl       TEXT    NOT NULL,
    revision           INTEGER NOT NULL,
    generation         INTEGER NOT NULL,
    coverage           TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    PRIMARY KEY (wallet_id, coin)
);

CREATE TABLE IF NOT EXISTS worldviews (
    view_id            TEXT    PRIMARY KEY,
    ledger_id          TEXT    NOT NULL,
    as_of_seq          INTEGER NOT NULL,
    knowledge_time     TEXT    NOT NULL,
    status             TEXT    NOT NULL,
    blocker_code       TEXT,
    blocker_reason     TEXT,
    details_json       TEXT    NOT NULL
);
"""


@dataclass(slots=True, frozen=True)
class ProjectedPosition:
    wallet_id: str
    coin: str
    szi: Decimal
    direction: str
    avg_entry: Decimal
    opened_at: datetime
    last_added_at: datetime
    realized_pnl: Decimal
    revision: int
    generation: int
    coverage: str
    updated_at: datetime


@dataclass(slots=True, frozen=True)
class Worldview:
    view_id: str
    ledger_id: str
    as_of_seq: int
    knowledge_time: datetime
    status: str  # HEALTHY, DEGRADED, BLOCKED
    blocker_code: str | None
    blocker_reason: str | None
    positions_count: int
    details: dict[str, Any]


class ProjectionStore:
    """Manages transactional projection state and consumer offsets."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        with self.conn:
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def get_offset(self, ledger_id: str) -> int:
        cur = self.conn.execute(
            "SELECT last_seq FROM consumer_offsets WHERE ledger_id = ?",
            (ledger_id,),
        )
        row = cur.fetchone()
        return int(row["last_seq"]) if row else 0

    def apply_canonical_events(
        self,
        ledger_id: str,
        events: list[dict[str, Any]],
        knowledge_time: datetime | None = None,
    ) -> int:
        """Apply events in ONE transaction, advancing the offset atomically."""
        if not events:
            return 0

        now = (knowledge_time or datetime.now(UTC)).isoformat()
        current_offset = self.get_offset(ledger_id)
        applied_count = 0

        with self.conn:
            for event in events:
                cursor = event.get("cursor", {})
                seq = int(cursor.get("seq", 0))

                # Idempotency guard: skip already applied events
                if seq <= current_offset:
                    continue

                kind = event.get("kind", "")
                wallet_id = event.get("wallet_id", "")
                instrument = event.get("instrument_id", "")
                coin = instrument.split(":")[-1] if ":" in instrument else instrument
                payload = event.get("payload", {})
                after_rev = int(event.get("after_revision", 0))
                event_time = event.get("event_time", now)

                if kind == "POSITION_CHANGE":
                    trans = payload.get("transition", "")
                    if trans in ("OPEN", "ADD", "REDUCE"):
                        qty = Decimal(
                            str(payload.get("quantity_after") or payload.get("size") or "0")
                        )
                        px = Decimal(str(payload.get("px") or payload.get("avg_entry") or "0"))
                        direction = "Long" if qty > 0 else "Short"

                        self.conn.execute(
                            """
                            INSERT INTO projected_positions (
                                wallet_id, coin, szi, direction, avg_entry, opened_at,
                                last_added_at, realized_pnl, revision, generation, coverage, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, '0', ?, 1, 'Seeded', ?)
                            ON CONFLICT(wallet_id, coin) DO UPDATE SET
                                szi = excluded.szi,
                                direction = excluded.direction,
                                avg_entry = excluded.avg_entry,
                                last_added_at = excluded.last_added_at,
                                revision = excluded.revision,
                                updated_at = excluded.updated_at
                            """,
                            (
                                wallet_id,
                                coin,
                                str(qty),
                                direction,
                                str(px),
                                event_time,
                                event_time,
                                after_rev,
                                now,
                            ),
                        )
                    elif trans == "CLOSE":
                        self.conn.execute(
                            "DELETE FROM projected_positions WHERE wallet_id = ? AND coin = ?",
                            (wallet_id, coin),
                        )

                current_offset = max(current_offset, seq)
                applied_count += 1

            # Atomically update consumer offset
            self.conn.execute(
                """
                INSERT INTO consumer_offsets (ledger_id, last_seq, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(ledger_id) DO UPDATE SET
                    last_seq = excluded.last_seq,
                    updated_at = excluded.updated_at
                """,
                (ledger_id, current_offset, now),
            )

        return applied_count

    def get_position(self, wallet_id: str, coin: str) -> ProjectedPosition | None:
        cur = self.conn.execute(
            "SELECT * FROM projected_positions WHERE wallet_id = ? AND coin = ?",
            (wallet_id, coin),
        )
        row = cur.fetchone()
        if not row:
            return None
        return ProjectedPosition(
            wallet_id=row["wallet_id"],
            coin=row["coin"],
            szi=Decimal(row["szi"]),
            direction=row["direction"],
            avg_entry=Decimal(row["avg_entry"]),
            opened_at=datetime.fromisoformat(row["opened_at"]),
            last_added_at=datetime.fromisoformat(row["last_added_at"]),
            realized_pnl=Decimal(row["realized_pnl"]),
            revision=int(row["revision"]),
            generation=int(row["generation"]),
            coverage=row["coverage"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_positions(self) -> list[ProjectedPosition]:
        cur = self.conn.execute("SELECT * FROM projected_positions ORDER BY wallet_id, coin")
        return [
            ProjectedPosition(
                wallet_id=row["wallet_id"],
                coin=row["coin"],
                szi=Decimal(row["szi"]),
                direction=row["direction"],
                avg_entry=Decimal(row["avg_entry"]),
                opened_at=datetime.fromisoformat(row["opened_at"]),
                last_added_at=datetime.fromisoformat(row["last_added_at"]),
                realized_pnl=Decimal(row["realized_pnl"]),
                revision=int(row["revision"]),
                generation=int(row["generation"]),
                coverage=row["coverage"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in cur.fetchall()
        ]

    def build_worldview(
        self,
        ledger_id: str,
        knowledge_time: datetime | None = None,
        stale_equity_threshold_s: float = 300.0,
    ) -> Worldview:
        """Construct a coherent worldview cut, flagging blockers and uncertainty (Q09)."""
        now = knowledge_time or datetime.now(UTC)
        offset = self.get_offset(ledger_id)
        positions = self.list_positions()

        status = "HEALTHY"
        blocker_code = None
        blocker_reason = None

        # Check for unseeded positions or coverage issues
        for pos in positions:
            if pos.coverage == "Quarantined":
                status = "BLOCKED"
                blocker_code = "STATE_QUARANTINED"
                blocker_reason = f"wallet {pos.wallet_id} has quarantined observation state"
                break
            elif pos.coverage == "Unseeded":
                status = "DEGRADED"
                blocker_code = "STATE_UNSEEDED"
                blocker_reason = f"wallet {pos.wallet_id} is unseeded"
                break

        view_id = f"wv:{ledger_id}:{offset}"
        details = {
            "total_positions": len(positions),
            "wallets": sorted({p.wallet_id for p in positions}),
            "coins": sorted({p.coin for p in positions}),
        }

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO worldviews (
                    view_id, ledger_id, as_of_seq, knowledge_time, status,
                    blocker_code, blocker_reason, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(view_id) DO UPDATE SET
                    status = excluded.status,
                    blocker_code = excluded.blocker_code,
                    blocker_reason = excluded.blocker_reason,
                    details_json = excluded.details_json
                """,
                (
                    view_id,
                    ledger_id,
                    offset,
                    now.isoformat(),
                    status,
                    blocker_code,
                    blocker_reason,
                    json.dumps(details),
                ),
            )

        return Worldview(
            view_id=view_id,
            ledger_id=ledger_id,
            as_of_seq=offset,
            knowledge_time=now,
            status=status,
            blocker_code=blocker_code,
            blocker_reason=blocker_reason,
            positions_count=len(positions),
            details=details,
        )
