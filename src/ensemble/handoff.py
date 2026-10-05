"""Compact agent handoffs and knowledge retention runbooks (Contracts O6, Q18).

Maintains inspectable handoff records and incident reproducer fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True, frozen=True)
class CompactHandoff:
    revision: int
    applied_state: dict[str, Any]
    unresolved_blockers: list[str]
    active_jobs: list[str]
    remaining_budget: int
    next_actionable_slice: str
    timestamp: datetime

    def to_brief(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "applied_state": self.applied_state,
            "unresolved_blockers": self.unresolved_blockers,
            "active_jobs": self.active_jobs,
            "remaining_budget": self.remaining_budget,
            "next_actionable_slice": self.next_actionable_slice,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass(slots=True, frozen=True)
class IncidentRunbook:
    incident_id: str
    title: str
    defect_category: str
    regression_fixture: str
    verification_command: str
    resolution: str
    superseded_by: str | None = None


class RunbookRegistry:
    """Registry of verified incident fixtures and regression runbooks (Q18)."""

    def __init__(self) -> None:
        self.runbooks: dict[str, IncidentRunbook] = {}

    def register(self, runbook: IncidentRunbook) -> None:
        self.runbooks[runbook.incident_id] = runbook

    def lookup(self, incident_id: str) -> IncidentRunbook | None:
        return self.runbooks.get(incident_id)

    def is_superseded(self, incident_id: str) -> bool:
        rb = self.runbooks.get(incident_id)
        return rb is not None and rb.superseded_by is not None


def generate_compact_handoff(
    revision: int,
    applied_state: dict[str, Any],
    unresolved_blockers: list[str],
    active_jobs: list[str],
    remaining_budget: int = 1000,
    next_slice: str = "S6",
) -> CompactHandoff:
    """Generate a compact handoff brief for subsequent sessions (O6)."""
    return CompactHandoff(
        revision=revision,
        applied_state=applied_state,
        unresolved_blockers=unresolved_blockers,
        active_jobs=active_jobs,
        remaining_budget=remaining_budget,
        next_actionable_slice=next_slice,
        timestamp=datetime.now(UTC),
    )
