"""Immutable forward-outcome ledger for V2 research."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

DEFAULT_OUTCOME_HORIZONS: tuple[tuple[str, timedelta], ...] = (
    ("1m", timedelta(minutes=1)),
    ("5m", timedelta(minutes=5)),
    ("15m", timedelta(minutes=15)),
    ("1h", timedelta(hours=1)),
    ("4h", timedelta(hours=4)),
    ("24h", timedelta(hours=24)),
)


@dataclass(slots=True, frozen=True)
class PricePoint:
    at: datetime
    price: Decimal


@dataclass(slots=True, frozen=True)
class OutcomeSource:
    source_id: str
    coin: str
    emitted_at: datetime
    signal_price: Decimal
    context: tuple[tuple[str, str], ...] = ()
    coverage: tuple[tuple[str, str], ...] = ()
    restated_after_emission: bool = False


@dataclass(slots=True, frozen=True)
class OutcomeManifest:
    manifest_id: str
    manifest_hash: str
    sources: tuple[OutcomeSource, ...]
    horizons: tuple[tuple[str, int], ...]
    latency_ms: int
    cost_bps: Decimal
    created_at: datetime


@dataclass(slots=True, frozen=True)
class OutcomeRecord:
    manifest_id: str
    source_id: str
    coin: str
    horizon: str
    emitted_at: datetime
    signal_price: Decimal
    latency_price: Decimal
    horizon_price: Decimal
    raw_return: Decimal
    latency_return: Decimal
    net_return: Decimal
    mfe: Decimal
    mae: Decimal
    context: tuple[tuple[str, str], ...]
    coverage: tuple[tuple[str, str], ...]
    restated_after_emission: bool
    outcome_known_at: datetime


def _jsonable_source(source: OutcomeSource) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "coin": source.coin,
        "emitted_at": source.emitted_at.isoformat(),
        "signal_price": str(source.signal_price),
        "context": list(source.context),
        "coverage": list(source.coverage),
        "restated_after_emission": source.restated_after_emission,
    }


def seal_outcome_manifest(
    *,
    sources: Sequence[OutcomeSource],
    created_at: datetime,
    horizons: Sequence[tuple[str, timedelta]] = DEFAULT_OUTCOME_HORIZONS,
    latency_ms: int = 0,
    cost_bps: Decimal = Decimal(0),
) -> OutcomeManifest:
    """Create a deterministic sealed manifest for reproducible outcome attachment."""
    normalized_sources = tuple(sorted(sources, key=lambda source: source.source_id))
    normalized_horizons = tuple(
        (label, int(delta.total_seconds())) for label, delta in horizons
    )
    payload = {
        "sources": [_jsonable_source(source) for source in normalized_sources],
        "horizons": list(normalized_horizons),
        "latency_ms": latency_ms,
        "cost_bps": str(cost_bps),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return OutcomeManifest(
        manifest_id=f"outcome:{digest[:16]}",
        manifest_hash=digest,
        sources=normalized_sources,
        horizons=normalized_horizons,
        latency_ms=latency_ms,
        cost_bps=cost_bps,
        created_at=created_at,
    )


def _first_at_or_after(prices: Sequence[PricePoint], when: datetime) -> PricePoint | None:
    return next((point for point in prices if point.at >= when), None)


def compute_outcome_record(
    *,
    manifest: OutcomeManifest,
    source: OutcomeSource,
    horizon: str,
    prices: Sequence[PricePoint],
) -> OutcomeRecord | None:
    """Compute one horizon outcome once enough future prices are knowable."""
    horizon_map = dict(manifest.horizons)
    if horizon not in horizon_map:
        raise ValueError(f"horizon {horizon!r} is not sealed in manifest")

    ordered = tuple(sorted(prices, key=lambda point: point.at))
    horizon_delta = timedelta(seconds=horizon_map[horizon])
    latency_delta = timedelta(milliseconds=manifest.latency_ms)
    latency_at = source.emitted_at + latency_delta
    horizon_at = source.emitted_at + horizon_delta

    latency_point = _first_at_or_after(ordered, latency_at)
    horizon_point = _first_at_or_after(ordered, horizon_at)
    if latency_point is None or horizon_point is None:
        return None
    if source.signal_price <= 0 or latency_point.price <= 0:
        raise ValueError("outcome prices must be positive")

    path = [
        point
        for point in ordered
        if latency_point.at <= point.at <= horizon_point.at
    ]
    if not path:
        return None

    raw_return = horizon_point.price / source.signal_price - Decimal(1)
    latency_return = horizon_point.price / latency_point.price - Decimal(1)
    cost_fraction = manifest.cost_bps / Decimal(10_000)
    net_return = latency_return - cost_fraction
    path_returns = [
        point.price / latency_point.price - Decimal(1)
        for point in path
    ]

    return OutcomeRecord(
        manifest_id=manifest.manifest_id,
        source_id=source.source_id,
        coin=source.coin,
        horizon=horizon,
        emitted_at=source.emitted_at,
        signal_price=source.signal_price,
        latency_price=latency_point.price,
        horizon_price=horizon_point.price,
        raw_return=raw_return,
        latency_return=latency_return,
        net_return=net_return,
        mfe=max(path_returns),
        mae=min(path_returns),
        context=source.context,
        coverage=source.coverage,
        restated_after_emission=source.restated_after_emission,
        outcome_known_at=horizon_point.at,
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS outcome_manifests (
    manifest_id TEXT PRIMARY KEY,
    manifest_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcome_records (
    manifest_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (manifest_id, source_id, horizon)
);
"""


def _manifest_payload(manifest: OutcomeManifest) -> str:
    payload = {
        "manifest_hash": manifest.manifest_hash,
        "sources": [_jsonable_source(source) for source in manifest.sources],
        "horizons": list(manifest.horizons),
        "latency_ms": manifest.latency_ms,
        "cost_bps": str(manifest.cost_bps),
        "created_at": manifest.created_at.isoformat(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _record_payload(record: OutcomeRecord) -> str:
    payload = asdict(record)
    for key in (
        "signal_price",
        "latency_price",
        "horizon_price",
        "raw_return",
        "latency_return",
        "net_return",
        "mfe",
        "mae",
    ):
        payload[key] = str(payload[key])
    payload["emitted_at"] = record.emitted_at.isoformat()
    payload["outcome_known_at"] = record.outcome_known_at.isoformat()
    payload["context"] = list(record.context)
    payload["coverage"] = list(record.coverage)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class OutcomeStore:
    """Append-only outcome store; stored horizon records are immutable."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        with self.conn:
            self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def register_manifest(self, manifest: OutcomeManifest) -> None:
        payload = _manifest_payload(manifest)
        row = self.conn.execute(
            "SELECT manifest_hash, payload_json FROM outcome_manifests WHERE manifest_id = ?",
            (manifest.manifest_id,),
        ).fetchone()
        if row is not None:
            if row[0] != manifest.manifest_hash or row[1] != payload:
                raise ValueError("sealed manifest identity conflict")
            return
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO outcome_manifests (
                    manifest_id, manifest_hash, payload_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    manifest.manifest_id,
                    manifest.manifest_hash,
                    payload,
                    manifest.created_at.isoformat(),
                ),
            )

    def append(self, record: OutcomeRecord) -> bool:
        payload = _record_payload(record)
        row = self.conn.execute(
            """
            SELECT payload_json FROM outcome_records
            WHERE manifest_id = ? AND source_id = ? AND horizon = ?
            """,
            (record.manifest_id, record.source_id, record.horizon),
        ).fetchone()
        if row is not None:
            if row[0] != payload:
                raise ValueError("immutable outcome record conflict")
            return False
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO outcome_records (manifest_id, source_id, horizon, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (record.manifest_id, record.source_id, record.horizon, payload),
            )
        return True

    def pending_pairs(self, manifest: OutcomeManifest) -> tuple[tuple[str, str], ...]:
        """Return deterministic work remaining after any restart."""
        done = {
            (row[0], row[1])
            for row in self.conn.execute(
                """
                SELECT source_id, horizon FROM outcome_records
                WHERE manifest_id = ?
                """,
                (manifest.manifest_id,),
            )
        }
        return tuple(
            (source.source_id, horizon)
            for source in manifest.sources
            for horizon, _seconds in manifest.horizons
            if (source.source_id, horizon) not in done
        )

    def record_count(self, manifest_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM outcome_records WHERE manifest_id = ?",
            (manifest_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0
