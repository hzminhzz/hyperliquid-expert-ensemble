"""Deterministic replay runner and evidence manifest management (Contracts C1, C7, Q14).

Enforces strict separation between as-known and restated historical replay cuts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .projection import ProjectedPosition, ProjectionStore


@dataclass(slots=True, frozen=True)
class ReplayManifest:
    manifest_id: str
    ledger_id: str
    clock_mode: str  # "as_known" or "restated"
    as_of: datetime
    policy_id: str
    events: list[dict[str, Any]]
    manifest_hash: str


@dataclass(slots=True, frozen=True)
class ReplayResult:
    manifest_id: str
    clock_mode: str
    events_applied: int
    positions: list[ProjectedPosition]
    decision_hash: str
    completed_at: datetime


class ReplayRunner:
    """Executes deterministic offline replay over pinned canonical event streams."""

    def __init__(self, temp_dir: Path | str) -> None:
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def create_manifest(
        ledger_id: str,
        events: list[dict[str, Any]],
        clock_mode: str = "as_known",
        as_of: datetime | None = None,
        policy_id: str = "default:v1",
    ) -> ReplayManifest:
        """Create a content-addressed input manifest pinned to an exact cut."""
        as_of_dt = as_of or datetime.now(UTC)
        raw_bytes = json.dumps(events, sort_keys=True).encode("utf-8")
        manifest_hash = hashlib.sha256(raw_bytes).hexdigest()
        manifest_id = f"mf:{ledger_id}:{clock_mode}:{manifest_hash[:16]}"

        return ReplayManifest(
            manifest_id=manifest_id,
            ledger_id=ledger_id,
            clock_mode=clock_mode,
            as_of=as_of_dt,
            policy_id=policy_id,
            events=events,
            manifest_hash=manifest_hash,
        )

    def run_replay(self, manifest: ReplayManifest) -> ReplayResult:
        """Run isolated replay into an ephemeral projection database (Q14)."""
        ephemeral_db = self.temp_dir / f"replay_{manifest.manifest_hash[:12]}.db"
        if ephemeral_db.exists():
            ephemeral_db.unlink()

        store = ProjectionStore(ephemeral_db)
        try:
            # Filter events based on clock mode
            if manifest.clock_mode == "as_known":
                # In as-known mode, events known strictly after the as_of cut are discarded
                filtered_events = [
                    e
                    for e in manifest.events
                    if datetime.fromisoformat(e.get("known_at", manifest.as_of.isoformat()))
                    <= manifest.as_of
                ]
            else:
                # Restated mode: includes corrections and restatements
                filtered_events = list(manifest.events)

            applied = store.apply_canonical_events(
                manifest.ledger_id,
                filtered_events,
                knowledge_time=manifest.as_of,
            )

            positions = store.list_positions()

            # Compute deterministic decision hash from semantic state
            semantic_repr = [
                {
                    "wallet": p.wallet_id,
                    "coin": p.coin,
                    "szi": str(p.szi),
                    "avg_entry": str(p.avg_entry),
                    "dir": p.direction,
                }
                for p in positions
            ]
            decision_bytes = json.dumps(semantic_repr, sort_keys=True).encode("utf-8")
            decision_hash = hashlib.sha256(decision_bytes).hexdigest()

            return ReplayResult(
                manifest_id=manifest.manifest_id,
                clock_mode=manifest.clock_mode,
                events_applied=applied,
                positions=positions,
                decision_hash=decision_hash,
                completed_at=datetime.now(UTC),
            )
        finally:
            store.close()
            if ephemeral_db.exists():
                ephemeral_db.unlink()
