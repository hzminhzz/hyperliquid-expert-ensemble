"""Prospective, restart-safe raw-intent markout recorder (shadow research only).

This records observed trade transitions and REST-mid price marks. It does NOT
create V2 WalletEvidence, infer semantic episodes, qualify alpha, or trade.
Only events first seen after activation with timely receipt are admitted.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .outcomes import (
    DEFAULT_OUTCOME_HORIZONS,
    OutcomeSource,
    OutcomeStore,
    PricePoint,
    compute_outcome_record,
    seal_outcome_manifest,
)

LOG = logging.getLogger("ensemble.live_outcomes")
_API = "https://api.hyperliquid.xyz/info"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS recorder_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ledger_id TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    last_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recorder_sources (
    source_id TEXT PRIMARY KEY,
    ledger_seq INTEGER NOT NULL UNIQUE,
    manifest_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    emitted_at TEXT NOT NULL,
    source_json TEXT NOT NULL,
    next_check_at TEXT,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recorder_sources_due ON
    recorder_sources (next_check_at) WHERE next_check_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS recorder_prices (
    instrument_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    price TEXT NOT NULL,
    PRIMARY KEY (instrument_id, observed_at)
);
CREATE TABLE IF NOT EXISTS recorder_censored (
    source_id TEXT NOT NULL,
    horizon TEXT NOT NULL,
    reason TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (source_id, horizon)
);
CREATE TABLE IF NOT EXISTS recorder_skipped (
    ledger_seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(s: str) -> datetime:
    value = datetime.fromisoformat(s)
    if value.tzinfo is None:
        raise ValueError("unqualified timestamp (missing UTC offset)")
    return value.astimezone(UTC)


def fetch_all_mids() -> dict[str, str]:
    request = urllib.request.Request(
        _API,
        data=b'{"type":"allMids"}',
        headers={"Content-Type": "application/json", "User-Agent": "copytrade-outcomes/1"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15.0) as response:
        raw = json.loads(response.read().decode("utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("allMids response is not a mapping")
    return {str(k): str(v) for k, v in raw.items()}


@dataclass(frozen=True)
class RecorderSettings:
    ledger_db: Path
    outcome_db: Path
    poll_seconds: float = 5.0
    latency_ms: int = 2000
    cost_bps: Decimal = Decimal(10)
    price_lateness_s: int = 25
    source_delay_s: int = 25
    max_batch: int = 1000

    @classmethod
    def from_env(cls) -> RecorderSettings:
        return cls(
            ledger_db=Path(os.getenv(
                "ENSEMBLE_LEDGER_DB", "/home/quant/.local/share/copytrade/observation.db"
            )),
            outcome_db=Path(os.getenv(
                "ENSEMBLE_OUTCOME_DB", "/home/quant/.local/share/copytrade/outcomes.db"
            )),
            poll_seconds=float(os.getenv("ENSEMBLE_OUTCOME_POLL_S", "5")),
            latency_ms=int(os.getenv("ENSEMBLE_OUTCOME_LATENCY_MS", "2000")),
            cost_bps=Decimal(os.getenv("ENSEMBLE_OUTCOME_COST_BPS", "10")),
            price_lateness_s=int(os.getenv("ENSEMBLE_OUTCOME_PRICE_LATENESS_S", "25")),
            source_delay_s=int(os.getenv("ENSEMBLE_OUTCOME_SOURCE_DELAY_S", "25")),
        )


class LiveOutcomeRecorder:
    def __init__(
        self,
        settings: RecorderSettings,
        *,
        now: Callable[[], datetime] = utcnow,
        mids_fetcher: Callable[[], dict[str, str]] = fetch_all_mids,
    ) -> None:
        if settings.poll_seconds <= 0 or settings.source_delay_s <= 0:
            raise ValueError("collector cadence and source delay must be positive")
        if settings.latency_ms < 0 or settings.cost_bps < 0:
            raise ValueError("negative latency/cost not allowed")
        if settings.price_lateness_s < settings.poll_seconds:
            raise ValueError("lateness tolerance must exceed price sampling period")
        self.settings = settings
        self.now = now
        self.mids_fetcher = mids_fetcher
        settings.outcome_db.parent.mkdir(parents=True, exist_ok=True)
        self.ledger = sqlite3.connect(
            f"file:{settings.ledger_db}?mode=ro", uri=True, timeout=10
        )
        self.ledger.row_factory = sqlite3.Row
        self.store = OutcomeStore(settings.outcome_db)
        self.db = self.store.conn
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        with self.db:
            self.db.executescript(_SCHEMA)
        self._init_state()

    def close(self) -> None:
        self.ledger.close()
        self.store.close()

    def _ledger_state(self) -> tuple[str, int]:
        row = self.ledger.execute(
            "SELECT ledger_id, current_seq FROM ledger_meta LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("ledger has no identity")
        return str(row["ledger_id"]), int(row["current_seq"])

    def _init_state(self) -> None:
        ledger_id, latest = self._ledger_state()
        row = self.db.execute(
            "SELECT ledger_id, activated_at, last_seq FROM recorder_state WHERE id=1"
        ).fetchone()
        if row is None:
            activated = self.now().isoformat()
            with self.db:
                self.db.execute(
                    "INSERT INTO recorder_state VALUES (1, ?, ?, ?)",
                    (ledger_id, activated, latest),
                )
            LOG.info(
                "prospective recording activated at %s from seq %d; prior history excluded",
                activated, latest,
            )
        elif str(row[0]) != ledger_id:
            raise RuntimeError("ledger identity changed; refuse mixing outcome datasets")

    def _cursor(self) -> int:
        row = self.db.execute(
            "SELECT last_seq FROM recorder_state WHERE id=1"
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _source(self, row: sqlite3.Row) -> tuple[OutcomeSource | None, str | None]:
        if row["kind"] != "POSITION_CHANGE":
            return None, None
        if str(row["quality"]).lower() != "valid":
            return None, "INVALID_EVENT"
        instrument = str(row["instrument_id"])
        if not instrument.startswith("hyperliquid:mainnet:perp:default:"):
            return None, "UNSUPPORTED_VENUE"
        known = as_utc(str(row["known_at"]))
        now = self.now()
        if (now - known).total_seconds() > self.settings.source_delay_s:
            return None, "LATE_LEDGER_CONSUMPTION"
        if (known - now).total_seconds() > self.settings.source_delay_s:
            return None, "FUTURE_KNOWLEDGE_TIME"
        payload = json.loads(row["payload"])
        try:
            price = Decimal(str(payload.get("px")))
            delta = Decimal(str(payload.get("delta")))
        except (InvalidOperation, TypeError, ValueError):
            return None, "INVALID_PRICE_OR_DELTA"
        if price <= 0 or not price.is_finite() or delta == 0:
            return None, "INVALID_PRICE_OR_DELTA"
        if not delta.is_finite():
            return None, "INVALID_PRICE_OR_DELTA"
        transition = str(payload.get("transition", "UNKNOWN"))
        if transition not in {"OPEN", "ADD", "REDUCE", "CLOSE", "FLIP"}:
            return None, "UNSUPPORTED_TRANSITION"
        return OutcomeSource(
            source_id=str(row["event_id"]),
            coin=instrument,
            emitted_at=known,
            signal_price=price,
            direction=1 if delta > 0 else -1,
            context=(
                ("event_type", "RAW_POSITION_CHANGE_NOT_SEMANTIC_EPISODE"),
                ("transition", transition),
                ("wallet_id", str(row["wallet_id"]).lower()),
                ("source_event_time", str(row["event_time"])),
                ("ledger_seq", str(row["seq"])),
            ),
            coverage=(
                ("price_source", "REST_allMids_as_received"),
                ("instrument_id", instrument),
                ("latency_proxy", "REST_sampled"),
                ("forward_qualification", "NOT_QUALIFIED"),
            ),
        ), None

    def ingest_ledger(self) -> tuple[int, int]:
        last = self._cursor()
        rows = self.ledger.execute(
            """SELECT seq,event_id,kind,instrument_id,wallet_id,event_time,
                      known_at,quality,payload
                 FROM outbox_events WHERE seq>? ORDER BY seq LIMIT ?""",
            (last, self.settings.max_batch),
        ).fetchall()
        admitted = 0
        for row in rows:
            seq = int(row["seq"])
            source, reason = self._source(row)
            if source is not None:
                # If the process crashed after registering but before advancing
                # the source cursor, reuse the originally sealed created_at.
                provisional = seal_outcome_manifest(sources=[source], created_at=self.now(),
                    latency_ms=self.settings.latency_ms, cost_bps=self.settings.cost_bps,
                    max_price_lateness=timedelta(seconds=self.settings.price_lateness_s))
                old = self.db.execute(
                    "SELECT payload_json FROM outcome_manifests WHERE manifest_id=?",
                    (provisional.manifest_id,),
                ).fetchone()
                created_at = (
                    as_utc(json.loads(old[0])["created_at"]) if old
                    else self.now()
                )
                manifest = seal_outcome_manifest(
                    sources=[source],
                    created_at=created_at,
                    latency_ms=self.settings.latency_ms,
                    cost_bps=self.settings.cost_bps,
                    max_price_lateness=timedelta(
                        seconds=self.settings.price_lateness_s
                    ),
                )
                self.store.register_manifest(manifest)
                payload = {
                    "source_id": source.source_id,
                    "coin": source.coin,
                    "emitted_at": source.emitted_at.isoformat(),
                    "signal_price": str(source.signal_price),
                    "direction": source.direction,
                    "context": list(source.context),
                    "coverage": list(source.coverage),
                }
                first_horizon = source.emitted_at + DEFAULT_OUTCOME_HORIZONS[0][1]
                with self.db:
                    self.db.execute(
                        """INSERT OR IGNORE INTO recorder_sources
                        (source_id,ledger_seq,manifest_id,instrument_id,emitted_at,
                         source_json,next_check_at,status)
                        VALUES (?,?,?,?,?,?,?,'PENDING')""",
                        (source.source_id, seq, manifest.manifest_id, source.coin,
                         source.emitted_at.isoformat(), json.dumps(payload),
                         first_horizon.isoformat()),
                    )
                    self.db.execute(
                        "UPDATE recorder_state SET last_seq=? WHERE id=1", (seq,)
                    )
                admitted += 1
            else:
                with self.db:
                    if reason:
                        self.db.execute(
                            "INSERT OR IGNORE INTO recorder_skipped VALUES (?,?,?,?)",
                            (seq, str(row["event_id"]), reason, self.now().isoformat()),
                        )
                    self.db.execute(
                        "UPDATE recorder_state SET last_seq=? WHERE id=1", (seq,)
                    )
        return len(rows), admitted

    def poll_prices(self) -> int:
        active = {
            row[0]
            for row in self.db.execute(
                "SELECT DISTINCT instrument_id FROM recorder_sources "
                "WHERE next_check_at IS NOT NULL"
            )
        }
        if not active:
            return 0
        mids = self.mids_fetcher()
        at = self.now().isoformat()
        inserted = 0
        with self.db:
            for instrument in active:
                # Default-perpetual symbol only. HIP-3 requires venue-aware feed.
                symbol = instrument.rsplit(":", 1)[-1]
                raw = mids.get(symbol)
                try:
                    price = Decimal(str(raw))
                except (ValueError, InvalidOperation, TypeError):
                    continue
                if price <= 0 or not price.is_finite():
                    continue
                self.db.execute(
                    "INSERT OR IGNORE INTO recorder_prices VALUES (?,?,?)",
                    (instrument, at, str(price)),
                )
                inserted += 1
        return inserted

    def mature_sources(self) -> tuple[int, int, int]:
        now = self.now()
        due = self.db.execute(
            """SELECT source_id,manifest_id,instrument_id,source_json
               FROM recorder_sources
               WHERE next_check_at IS NOT NULL AND next_check_at<=?
               ORDER BY next_check_at LIMIT ?""",
            (now.isoformat(), self.settings.max_batch),
        ).fetchall()
        recorded = censored = 0
        for sid, mid, instrument, raw in due:
            payload = json.loads(raw)
            source = OutcomeSource(
                source_id=payload["source_id"],
                coin=payload["coin"],
                emitted_at=as_utc(payload["emitted_at"]),
                signal_price=Decimal(payload["signal_price"]),
                direction=int(payload["direction"]),
                context=tuple(tuple(x) for x in payload["context"]),
                coverage=tuple(tuple(x) for x in payload["coverage"]),
            )
            manifest = seal_outcome_manifest(
                sources=[source],
                created_at=now,
                latency_ms=self.settings.latency_ms,
                cost_bps=self.settings.cost_bps,
                max_price_lateness=timedelta(seconds=self.settings.price_lateness_s),
            )
            if manifest.manifest_id != mid:
                raise RuntimeError("manifest hash/config drift: refuse to rewrite records")
            censored_horizons = {
                r[0] for r in self.db.execute(
                    "SELECT horizon FROM recorder_censored WHERE source_id=?", (sid,)
                )
            }
            pending = set(self.store.pending_pairs(manifest))
            prices = [
                PricePoint(at=as_utc(at), price=Decimal(px), known_at=as_utc(at))
                for at, px in self.db.execute(
                    """SELECT observed_at,price FROM recorder_prices
                       WHERE instrument_id=? AND observed_at>=? AND observed_at<=?
                       ORDER BY observed_at""",
                    (
                        instrument,
                        source.emitted_at.isoformat(),
                        (now + timedelta(seconds=self.settings.price_lateness_s)).isoformat(),
                    ),
                )
            ]
            next_at: datetime | None = None
            for horizon, seconds in manifest.horizons:
                if (sid, horizon) not in pending or horizon in censored_horizons:
                    continue
                maturity_at = source.emitted_at + timedelta(seconds=seconds)
                if now < maturity_at:
                    next_at = min(next_at, maturity_at) if next_at else maturity_at
                    continue
                record = compute_outcome_record(
                    manifest=manifest, source=source, horizon=horizon, prices=prices
                )
                if record is not None:
                    if self.store.append(record):
                        recorded += 1
                    continue
                deadline = maturity_at + timedelta(seconds=self.settings.price_lateness_s)
                if now >= deadline:
                    with self.db:
                        self.db.execute(
                            "INSERT OR IGNORE INTO recorder_censored VALUES (?,?,?,?)",
                            (sid, horizon, "PRICE_MISSING_OR_TOO_LATE", now.isoformat()),
                        )
                    censored += 1
                else:
                    retry = now + timedelta(seconds=self.settings.poll_seconds)
                    next_at = min(next_at, retry) if next_at else retry
            with self.db:
                self.db.execute(
                    "UPDATE recorder_sources SET next_check_at=?,status=? WHERE source_id=?",
                    (
                        next_at.isoformat() if next_at else None,
                        "PENDING" if next_at else "SETTLED",
                        sid,
                    ),
                )
        return len(due), recorded, censored

    def step(self) -> dict[str, int]:
        fetched, admitted = self.ingest_ledger()
        samples = self.poll_prices()
        due, recorded, censored = self.mature_sources()
        # Price history is only needed while 24h outcome horizon is pending.
        cutoff = (self.now() - timedelta(hours=26)).isoformat()
        with self.db:
            self.db.execute(
                "DELETE FROM recorder_prices WHERE observed_at < ?", (cutoff,)
            )
        return {
            "ledger_events_seen": fetched,
            "sources_admitted": admitted,
            "price_points": samples,
            "sources_due": due,
            "outcomes_added": recorded,
            "censored": censored,
        }

    def stats(self) -> dict[str, Any]:
        db = self.db
        count = lambda sql: int(db.execute(sql).fetchone()[0])
        return {
            "registered_sources": count("SELECT count(*) FROM recorder_sources"),
            "pending_sources": count(
                "SELECT count(*) FROM recorder_sources WHERE next_check_at IS NOT NULL"
            ),
            "outcomes": count("SELECT count(*) FROM outcome_records"),
            "censored": count("SELECT count(*) FROM recorder_censored"),
            "skipped_sources": count("SELECT count(*) FROM recorder_skipped"),
            "price_points": count("SELECT count(*) FROM recorder_prices"),
            "cursor": self._cursor(),
        }


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = RecorderSettings.from_env()
    recorder = LiveOutcomeRecorder(settings)
    LOG.info(
        "outcome shadow recorder started; cost_bps=%s latency_ms=%d cadence_s=%.1f",
        settings.cost_bps, settings.latency_ms, settings.poll_seconds,
    )
    try:
        while True:
            try:
                detail = recorder.step()
                if any((detail["sources_admitted"], detail["outcomes_added"], detail["censored"])):
                    LOG.info("outcome collection step %s", detail)
            except (OSError, sqlite3.Error, urllib.error.URLError,
                    TimeoutError, ValueError, TypeError, InvalidOperation):
                LOG.exception("outcome collector error; will retry without cursor reset")
            time.sleep(settings.poll_seconds)
    finally:
        recorder.close()


if __name__ == "__main__":
    run()
