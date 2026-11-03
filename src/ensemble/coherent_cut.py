"""Deterministic coherent evidence cuts and coalesced material interrupts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta


@dataclass(slots=True, frozen=True)
class EvidenceCut:
    cut_id: str
    instrument_id: str
    event_time: datetime
    knowledge_time: datetime
    watermark: str
    source_revision: int
    normalization_revision: str
    independence_revision: str
    feature_revision: str
    universe_revision: str
    cause: str


@dataclass(slots=True, frozen=True)
class PendingInterrupt:
    instrument_id: str
    cluster_id: str
    first_requested_at: datetime
    last_requested_at: datetime
    latest_evidence_id: str
    latest_revision: int


def aligned_minute_cut(
    *,
    instrument_id: str,
    event_time: datetime,
    knowledge_time: datetime,
    watermark: str,
    source_revision: int,
    normalization_revision: str,
    independence_revision: str,
    feature_revision: str,
    universe_revision: str,
    cause: str = "CLOCK",
) -> EvidenceCut:
    if knowledge_time < event_time:
        raise ValueError("knowledge_time cannot precede the economic cut")
    minute = event_time.replace(second=0, microsecond=0)
    cut_id = (
        f"cut:{instrument_id}:{minute.isoformat()}:{source_revision}:"
        f"{normalization_revision}:{independence_revision}:{feature_revision}:{universe_revision}"
    )
    return EvidenceCut(
        cut_id=cut_id,
        instrument_id=instrument_id,
        event_time=minute,
        knowledge_time=knowledge_time,
        watermark=watermark,
        source_revision=source_revision,
        normalization_revision=normalization_revision,
        independence_revision=independence_revision,
        feature_revision=feature_revision,
        universe_revision=universe_revision,
        cause=cause,
    )


class InterruptCoalescer:
    """Coalesce material changes; latest coherent contribution replaces older pending state."""

    def __init__(self, delay: timedelta = timedelta(seconds=3)) -> None:
        if delay <= timedelta(0):
            raise ValueError("delay must be positive")
        self.delay = delay
        self._pending: dict[tuple[str, str], PendingInterrupt] = {}

    def request(
        self,
        *,
        instrument_id: str,
        cluster_id: str,
        requested_at: datetime,
        evidence_id: str,
        revision: int,
    ) -> PendingInterrupt:
        key = (instrument_id, cluster_id)
        current = self._pending.get(key)
        if current is None:
            current = PendingInterrupt(
                instrument_id=instrument_id,
                cluster_id=cluster_id,
                first_requested_at=requested_at,
                last_requested_at=requested_at,
                latest_evidence_id=evidence_id,
                latest_revision=revision,
            )
        elif revision >= current.latest_revision:
            current = replace(
                current,
                last_requested_at=requested_at,
                latest_evidence_id=evidence_id,
                latest_revision=revision,
            )
        self._pending[key] = current
        return current

    def cancel(self, *, instrument_id: str, cluster_id: str) -> None:
        self._pending.pop((instrument_id, cluster_id), None)

    def ready(self, now: datetime) -> tuple[PendingInterrupt, ...]:
        ready = [
            item
            for item in self._pending.values()
            if now - item.first_requested_at >= self.delay
        ]
        for item in ready:
            self._pending.pop((item.instrument_id, item.cluster_id), None)
        return tuple(
            sorted(ready, key=lambda item: (item.instrument_id, item.cluster_id))
        )

    def pending(self) -> tuple[PendingInterrupt, ...]:
        return tuple(
            sorted(
                self._pending.values(),
                key=lambda item: (item.instrument_id, item.cluster_id),
            )
        )
