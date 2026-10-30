"""Causal, immutable copy-opportunity journal (DI1-DI2).

A position reduction is not an opposite-direction entry. This module contains
no exchange access, Telegram delivery, or financial execution effects. The
existing Rust ledger remains the authoritative source of position transitions.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from .wallet_evidence import IntentKind, classify_intent


class CopyKind(StrEnum):
    ENTER = "ENTER"
    INCREASE = "INCREASE"
    DE_RISK = "DE_RISK"
    EXIT = "EXIT"
    REVERSE = "REVERSE"


class OpportunityStatus(StrEnum):
    ACTIVE = "ACTIVE"
    UPDATED = "UPDATED"
    NO_COPY = "NO_COPY"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class TradeTransition:
    source_id: str
    wallet_id: str
    instrument_id: str
    source_kind: str
    quantity_before: Decimal
    quantity_after: Decimal
    price: Decimal
    event_at: datetime
    known_at: datetime
    ledger_id: str
    ledger_seq: int
    cause: str = "ECONOMIC_CHANGE"
    source_quality: str = "valid"
    coverage: str = "SUPPORTED"

    def __post_init__(self) -> None:
        if self.event_at.tzinfo is None or self.known_at.tzinfo is None:
            raise ValueError("timestamp requires timezone")
        if self.known_at < self.event_at:
            # Clock disagreement is not permission to pretend the event was
            # knowable earlier. Record it with an explicit coverage warning.
            raise ValueError("known_at precedes event_at; source-clock audit required")
        if self.price <= 0 or not self.price.is_finite():
            raise ValueError("invalid source price")
        if not self.quantity_before.is_finite() or not self.quantity_after.is_finite():
            raise ValueError("invalid signed quantity")

    @classmethod
    def from_ledger(cls, row: Any, *, ledger_id: str) -> TradeTransition | None:
        if row["kind"] != "POSITION_CHANGE":
            return None
        data = json.loads(row["payload"])
        try:
            after = Decimal(str(data["quantity_after"]))
            delta = Decimal(str(data["delta"]))
            px = Decimal(str(data["px"]))
            event_at = datetime.fromisoformat(str(row["event_time"]))
            known_at = datetime.fromisoformat(str(row["known_at"]))
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise ValueError("invalid canonical position-change payload") from exc
        if delta == 0:
            raise ValueError("zero quantity change is not economic transition")
        before = after - delta
        transition = classify_intent(before, after)
        if transition is None or transition.value != data.get("transition"):
            raise ValueError("source transition disagrees with exact signed quantities")
        return cls(
            source_id=str(row["event_id"]),
            wallet_id=str(row["wallet_id"]).lower(),
            instrument_id=str(row["instrument_id"]),
            source_kind=transition.value,
            quantity_before=before,
            quantity_after=after,
            price=px,
            event_at=event_at,
            known_at=known_at,
            ledger_id=ledger_id,
            ledger_seq=int(row["seq"]),
            source_quality=str(row["quality"]),
            coverage=str(data.get("coverage") or "UNSPECIFIED"),
        )


@dataclass(frozen=True, slots=True)
class OpportunityPolicy:
    # Lifecycle semantics changed: do not silently reuse the old policy identity.
    revision: str = "DI2-shadow-v2"
    coalesce_window: timedelta = timedelta(minutes=10)
    action_latency: timedelta = timedelta(seconds=2)
    max_observation_delay: timedelta = timedelta(seconds=30)
    min_notional: Decimal = Decimal(0)  # Research sensitivity, not validated threshold.

    def __post_init__(self) -> None:
        if not self.revision:
            raise ValueError("policy revision required")
        if self.coalesce_window <= timedelta() or self.action_latency < timedelta():
            raise ValueError("invalid opportunity timing")
        if self.max_observation_delay <= timedelta() or self.min_notional < 0:
            raise ValueError("invalid support threshold")


@dataclass(frozen=True, slots=True)
class OpportunityDecision:
    opportunity_id: str
    wallet_id: str
    instrument_id: str
    source_action: str
    family: str
    copy_direction: int | None
    status: OpportunityStatus
    first_event_at: datetime
    first_known_at: datetime
    earliest_actionable_at: datetime
    first_source_id: str
    first_ledger_seq: int
    source_quantity_before: Decimal
    source_quantity_after: Decimal
    source_price: Decimal
    source_coverage: str
    revision: str
    blocker: str | None

    def as_dict(self) -> dict[str, Any]:
        obj = asdict(self)
        return {
            k: (v.isoformat() if isinstance(v, datetime)
                else str(v) if isinstance(v, Decimal)
                else v.value if isinstance(v, StrEnum) else v)
            for k, v in obj.items()
        }


def classify_copy_action(
    before: Decimal, after: Decimal,
) -> tuple[CopyKind, str, int | None]:
    """Typed trade semantics, preserving lifecycle direction on reductions."""
    kind = classify_intent(before, after)
    if kind is None:
        raise ValueError("unchanged position is not a copy decision")
    if kind == IntentKind.FLIP:
        return CopyKind.REVERSE, "FLIP_TO_LONG" if after > 0 else "FLIP_TO_SHORT", (
            1 if after > 0 else -1
        )
    if kind == IntentKind.OPEN:
        return CopyKind.ENTER, "OPEN_LONG" if after > 0 else "OPEN_SHORT", (
            1 if after > 0 else -1
        )
    if kind == IntentKind.ADD:
        return CopyKind.INCREASE, "ADD_LONG" if after > 0 else "ADD_SHORT", (
            1 if after > 0 else -1
        )
    if kind == IntentKind.REDUCE:
        return CopyKind.DE_RISK, "REDUCE_LONG" if before > 0 else "REDUCE_SHORT", None
    return CopyKind.EXIT, "CLOSE_LONG" if before > 0 else "CLOSE_SHORT", None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunity_state (
 ledger_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunity_decisions (
 opportunity_id TEXT PRIMARY KEY, wallet_id TEXT NOT NULL,
 instrument_id TEXT NOT NULL, first_source_id TEXT NOT NULL UNIQUE,
 first_ledger_seq INTEGER NOT NULL, first_event_at TEXT NOT NULL,
 first_known_at TEXT NOT NULL, earliest_actionable_at TEXT NOT NULL,
 family TEXT NOT NULL, source_action TEXT NOT NULL,
 copy_direction INTEGER, status TEXT NOT NULL, policy_revision TEXT NOT NULL,
 blocker TEXT, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opportunity_scope
 ON opportunity_decisions(wallet_id,instrument_id,first_known_at);
CREATE INDEX IF NOT EXISTS idx_opportunity_known
 ON opportunity_decisions(first_known_at);
CREATE INDEX IF NOT EXISTS idx_opportunity_live_scope
 ON opportunity_decisions(wallet_id,instrument_id,copy_direction,first_ledger_seq);
CREATE TABLE IF NOT EXISTS opportunity_updates (
 source_id TEXT PRIMARY KEY, opportunity_id TEXT,
 ledger_seq INTEGER NOT NULL, event_at TEXT NOT NULL,
 known_at TEXT NOT NULL, source_action TEXT, status TEXT NOT NULL,
 reason TEXT, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opp_updates
 ON opportunity_updates(opportunity_id,known_at);
CREATE TABLE IF NOT EXISTS opportunity_exclusions (
 source_id TEXT PRIMARY KEY, ledger_seq INTEGER NOT NULL UNIQUE,
 reason TEXT NOT NULL, known_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_coverage_barriers (
 ledger_id TEXT NOT NULL, ledger_seq INTEGER NOT NULL,
 source_id TEXT NOT NULL UNIQUE, known_at TEXT NOT NULL,
 reason TEXT NOT NULL, PRIMARY KEY (ledger_id,ledger_seq)
);
CREATE INDEX IF NOT EXISTS idx_source_barriers_known
 ON source_coverage_barriers(known_at);
"""


class OpportunityJournal:
    """Append-only first decisions plus append-only causal updates.

    One writer, transactional source cursor, no external side effects.
    The initial decision is immutable. Overlap/coalescing is explicitly scoped
    to ONE wallet/instrument/direction and matching entry/increase family.
    """

    def __init__(self, path: Path | str, policy: OpportunityPolicy | None = None) -> None:
        self.policy = policy or OpportunityPolicy()
        self.db = sqlite3.connect(str(path), timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=15000")
        with self.db:
            self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def cursor(self, ledger_id: str) -> int:
        row = self.db.execute(
            "SELECT last_seq FROM opportunity_state WHERE ledger_id=?", (ledger_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def seal_cursor(self, ledger_id: str, seq: int) -> None:
        """Advance cursor as part of caller's SQLite transaction."""
        current = self.cursor(ledger_id)
        if seq < current:
            raise ValueError("opportunity cursor cannot move backward")
        self.db.execute(
            "INSERT INTO opportunity_state VALUES (?,?) ON CONFLICT(ledger_id) "
            "DO UPDATE SET last_seq=excluded.last_seq", (ledger_id, seq)
        )

    def record_source_gap(
        self, *, ledger_id: str, ledger_seq: int, source_id: str,
        known_at: datetime, reason: str,
    ) -> None:
        """Persist a source-coverage barrier atomically with the ledger cursor.

        This does not mark any wallet flat, rewrite a historical decision, or
        claim missing public trades have been recovered. It only prevents
        earlier directional votes being presented as currently valid.
        """
        if known_at.tzinfo is None or not reason or ledger_seq <= 0:
            raise ValueError("invalid source coverage boundary")
        with self.db:
            last_seq = self.cursor(ledger_id)
            if ledger_seq <= last_seq:
                return
            if last_seq > 0 and ledger_seq != last_seq + 1:
                raise ValueError("ledger gap: coverage boundary must preserve source order")
            self.db.execute(
                "INSERT INTO source_coverage_barriers VALUES (?,?,?,?,?)",
                (ledger_id,ledger_seq,source_id,known_at.isoformat(),reason),
            )
            self.seal_cursor(ledger_id,ledger_seq)

    def _last_update(self, opportunity_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM opportunity_updates WHERE opportunity_id=? "
            "ORDER BY ledger_seq DESC,rowid DESC LIMIT 1", (opportunity_id,)
        ).fetchone()

    def _unclosed_direction(
        self, change: TradeTransition, direction: int,
    ) -> list[str]:
        """Find all current entries independently of the ADD-coalescing time window.

        Also include prior policy revisions: a policy change must not strand a live
        opportunity when its source wallet later exits. All queries are scoped to
        exactly one wallet, instrument and source direction.
        """
        rows = self.db.execute(
            "SELECT opportunity_id FROM opportunity_decisions "
            "WHERE wallet_id=? AND instrument_id=? AND copy_direction=? "
            "AND status='ACTIVE' ORDER BY first_ledger_seq",
            (change.wallet_id, change.instrument_id, direction),
        ).fetchall()
        return [
            str(row["opportunity_id"]) for row in rows
            if not (last := self._last_update(str(row["opportunity_id"])))
            or last["status"] not in ("INVALIDATED", "EXPIRED")
        ]

    def _active_same_direction(
        self, change: TradeTransition, direction: int,
    ) -> str | None:
        rows = self.db.execute(
            "SELECT opportunity_id,first_known_at FROM opportunity_decisions "
            "WHERE wallet_id=? AND instrument_id=? AND copy_direction=? "
            "AND status='ACTIVE' AND policy_revision=? "
            "ORDER BY first_ledger_seq DESC LIMIT 8",
            (change.wallet_id, change.instrument_id, direction, self.policy.revision),
        )
        for row in rows:
            last = self._last_update(row["opportunity_id"])
            if last and last["status"] in ("INVALIDATED", "EXPIRED"):
                continue
            previous = datetime.fromisoformat(
                str(last["known_at"] if last else row["first_known_at"])
            )
            gap = change.known_at - previous
            if timedelta() <= gap <= self.policy.coalesce_window:
                return str(row["opportunity_id"])
        return None

    def observe(
        self, change: TradeTransition, *, observation_at: datetime | None = None,
    ) -> OpportunityDecision | None:
        """Accept one accepted ledger event idempotently.

        The whole decision/update/exclusion write is atomic with the cursor.
        The stream must be consumed in ledger sequence. Gaps must be surfaced.
        """
        if change.ledger_seq <= 0:
            raise ValueError("invalid ledger cursor")
        if change.source_quality.lower() != "valid" or change.cause != "ECONOMIC_CHANGE":
            blocker = "INVALID_OR_NON_ECONOMIC_SOURCE"
        elif change.known_at - change.event_at > self.policy.max_observation_delay:
            blocker = "SOURCE_TOO_LATE"
        else:
            blocker = None
        now = observation_at or change.known_at
        if now.tzinfo is None or now < change.known_at:
            raise ValueError("cannot observe future knowledge")
        if now - change.known_at > self.policy.max_observation_delay:
            blocker = "DELAYED_LEDGER_CONSUMPTION"
        kind, action, direction = classify_copy_action(
            change.quantity_before, change.quantity_after
        )
        verified_kind = classify_intent(change.quantity_before, change.quantity_after)
        if verified_kind is None or change.source_kind != verified_kind.value:
            raise ValueError("source action conflicts with position path")
        if abs(change.quantity_after - change.quantity_before) * change.price < (
            self.policy.min_notional
        ):
            blocker = "BELOW_MATERIALITY_FLOOR"
        if change.coverage not in ("SUPPORTED", "Seeded", "KnownFlat"):
            blocker = blocker or (
                "SOURCE_COVERAGE_UNVERIFIED"
                if change.coverage in ("UNSPECIFIED", "UNKNOWN")
                else "PARTIAL_SOURCE_COVERAGE"
            )
        earliest = max(change.known_at, now) + self.policy.action_latency
        key = change.source_id + "|" + self.policy.revision
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:28]
        opp_id = "copy:" + digest

        with self.db:
            last_seq = self.cursor(change.ledger_id)
            if change.ledger_seq <= last_seq:
                return None
            if last_seq > 0 and change.ledger_seq != last_seq + 1:
                raise ValueError("ledger gap: refuse to infer uninterrupted behavior")
            existing = None
            if direction is not None and kind == CopyKind.INCREASE:
                existing = self._active_same_direction(change, direction)
            if kind in (CopyKind.EXIT, CopyKind.REVERSE):
                old_direction = 1 if change.quantity_before > 0 else -1
                # A late CLOSE/FLIP terminates every outstanding entry for this
                # wallet/instrument/direction, not just a fill within 10 minutes.
                # Initial decisions and fixed-horizon outcomes remain immutable.
                for active in self._unclosed_direction(change, old_direction):
                    self.db.execute(
                        "INSERT INTO opportunity_updates VALUES (?,?,?,?,?,?,?,?,?)",
                        (change.source_id + ":close:" + active, active,
                         change.ledger_seq, change.event_at.isoformat(),
                         change.known_at.isoformat(), action,
                         OpportunityStatus.INVALIDATED.value,
                         blocker or "SOURCE_POSITION_CLOSED_OR_FLIPPED", "{}"),
                    )
            if kind in (CopyKind.DE_RISK, CopyKind.EXIT) or blocker:
                reason = blocker or "DE_RISK_NOT_OPPOSITE_ENTRY"
                self.db.execute(
                    "INSERT INTO opportunity_exclusions VALUES (?,?,?,?)",
                    (change.source_id, change.ledger_seq, reason, change.known_at.isoformat()),
                )
                if kind in (CopyKind.DE_RISK, CopyKind.EXIT) and not blocker:
                    # De-risking is retained as a distinct, no-copy opportunity.
                    existing = None
                    direction = None
                    status = OpportunityStatus.NO_COPY
                else:
                    self.seal_cursor(change.ledger_id, change.ledger_seq)
                    return None
            else:
                status = OpportunityStatus.ACTIVE
            if existing is not None:
                self.db.execute(
                    "INSERT INTO opportunity_updates VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        change.source_id, existing, change.ledger_seq,
                        change.event_at.isoformat(), change.known_at.isoformat(),
                        action, OpportunityStatus.UPDATED.value, None,
                        json.dumps({"before":str(change.quantity_before),
                                    "after":str(change.quantity_after)},sort_keys=True),
                    ),
                )
                self.seal_cursor(change.ledger_id, change.ledger_seq)
                return None
            decision = OpportunityDecision(
                opportunity_id=opp_id, wallet_id=change.wallet_id,
                instrument_id=change.instrument_id, source_action=action,
                family=kind.value, copy_direction=direction, status=status,
                first_event_at=change.event_at, first_known_at=change.known_at,
                earliest_actionable_at=earliest, first_source_id=change.source_id,
                first_ledger_seq=change.ledger_seq,
                source_quantity_before=change.quantity_before,
                source_quantity_after=change.quantity_after, source_price=change.price,
                source_coverage=change.coverage, revision=self.policy.revision,
                blocker=blocker or ("DE_RISK_NOT_OPPOSITE_ENTRY" if direction is None else None),
            )
            self.db.execute(
                "INSERT INTO opportunity_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision.opportunity_id, decision.wallet_id,
                    decision.instrument_id, decision.first_source_id,
                    decision.first_ledger_seq, decision.first_event_at.isoformat(),
                    decision.first_known_at.isoformat(),
                    decision.earliest_actionable_at.isoformat(),
                    decision.family, decision.source_action,
                    decision.copy_direction, decision.status.value, decision.revision,
                    decision.blocker, json.dumps(decision.as_dict(),sort_keys=True),
                ),
            )
            self.db.execute(
                "INSERT INTO opportunity_updates VALUES (?,?,?,?,?,?,?,?,?)",
                (change.source_id, opp_id, change.ledger_seq,
                 change.event_at.isoformat(), change.known_at.isoformat(),
                 action, decision.status.value, decision.blocker,
                 json.dumps({"before":str(change.quantity_before),
                             "after":str(change.quantity_after)},sort_keys=True)),
            )
            self.seal_cursor(change.ledger_id, change.ledger_seq)
            return decision

    def get(self, opportunity_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT payload_json FROM opportunity_decisions WHERE opportunity_id=?",
            (opportunity_id,),
        ).fetchone()
        if row is None:
            return None
        result = json.loads(str(row[0]))
        updates = self.db.execute(
            "SELECT source_action,status,known_at,reason FROM opportunity_updates "
            "WHERE opportunity_id=? ORDER BY ledger_seq LIMIT 31", (opportunity_id,),
        ).fetchall()
        result["updates"] = [dict(r) for r in updates[:30]]
        result["updates_truncated"] = len(updates) > 30
        last = self._last_update(opportunity_id)
        result["latest_update"] = dict(last) if last is not None else None
        raw_status = str(last["status"] if last else result["status"])
        result["current_status"] = (
            "ACTIVE" if raw_status == OpportunityStatus.UPDATED.value else raw_status
        )
        result["authority"] = "SHADOW_ONLY_NOT_PREDICTIVE_QUALIFIED"
        result["expected_return"] = None
        return result

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT payload_json FROM opportunity_decisions "
            "ORDER BY first_ledger_seq DESC LIMIT ?", (min(max(limit, 0), 100),)
        ).fetchall()
        return [json.loads(str(r[0])) for r in rows]

    def stats(self) -> dict[str, Any]:
        counts = self.db.execute(
            "SELECT family,count(*) AS n FROM opportunity_decisions GROUP BY family"
        ).fetchall()
        exclusions = self.db.execute(
            "SELECT reason,count(*) AS n FROM opportunity_exclusions GROUP BY reason"
        ).fetchall()
        return {
            "opportunities": {str(r[0]):int(r[1]) for r in counts},
            "exclusions": {str(r[0]):int(r[1]) for r in exclusions},
            "status": "SHADOW_ONLY_NOT_PREDICTIVE_QUALIFIED",
        }
