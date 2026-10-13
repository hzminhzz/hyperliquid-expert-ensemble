"""Cost-aware shadow markouts and paired-control evidence (DI3-DI4).

All returns are hypothetical mid-to-mid unless a separately qualified
executable quote/fill source exists. A market-price trajectory is not evidence
that the wallet ensemble predicted it: market-only controls are mandatory
before ANY predictive promotion.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .copy_opportunities import OpportunityDecision

DEFAULT_HORIZONS = (
    ("5m", timedelta(minutes=5)),
    ("15m", timedelta(minutes=15)),
    ("1h", timedelta(hours=1)),
    ("4h", timedelta(hours=4)),
    ("24h", timedelta(hours=24)),
)


@dataclass(frozen=True, slots=True)
class RecordedMid:
    instrument_id: str
    known_at: datetime
    price: Decimal

    def __post_init__(self) -> None:
        if self.known_at.tzinfo is None or self.price <= 0 or not self.price.is_finite():
            raise ValueError("invalid quote")


@dataclass(frozen=True, slots=True)
class CopyMarkout:
    opportunity_id: str
    instrument_id: str
    horizon: str
    direction: int
    status: str
    first_actionable_at: datetime
    entry_at: datetime | None
    exit_at: datetime | None
    entry_mid: Decimal | None
    exit_mid: Decimal | None
    signed_gross_return: Decimal | None
    hypothetical_net_return: Decimal | None
    cost_bps: Decimal
    reason: str | None
    price_model: str = "MID_TO_MID_HYPOTHETICAL_NOT_EXECUTABLE"

    def jsonable(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            k: v.isoformat() if isinstance(v, datetime)
            else str(v) if isinstance(v, Decimal) else v
            for k, v in data.items()
        }


@dataclass(frozen=True, slots=True)
class PairedControl:
    """Independent point-in-time-frozen market-only candidate.

    label is attached after maturity. The control-selection algorithm MUST
    have been frozen BEFORE seeing evaluation outcomes.
    """
    opportunity_id: str
    horizon: str
    market_only_net_return: Decimal
    control_family: str
    control_revision: str
    eligible_at: datetime


def copy_markout(
    opportunity: OpportunityDecision,
    horizon: str,
    price_points: Sequence[RecordedMid],
    *,
    cost_bps: Decimal = Decimal(10),
    max_quote_lateness: timedelta = timedelta(seconds=90),
    now: datetime | None = None,
) -> CopyMarkout:
    if cost_bps < 0 or max_quote_lateness < timedelta():
        raise ValueError("invalid cost or price lateness")
    delays = dict(DEFAULT_HORIZONS)
    if horizon not in delays:
        raise ValueError("unregistered horizon")
    direction = opportunity.copy_direction
    ready_at = opportunity.earliest_actionable_at
    if direction not in (-1, 1):
        return CopyMarkout(
            opportunity.opportunity_id, opportunity.instrument_id,
            horizon, 0, "NO_COPY", ready_at, None, None,
            None, None, None, None, cost_bps,
            "REDUCE_OR_EXIT_IS_NOT_AN_OPPOSITE_ENTRY",
        )
    if opportunity.blocker:
        return CopyMarkout(
            opportunity.opportunity_id, opportunity.instrument_id,
            horizon, direction, "CENSORED", ready_at, None, None,
            None, None, None, None, cost_bps, opportunity.blocker,
        )
    maturity = ready_at + delays[horizon]
    if now is not None and now < maturity + max_quote_lateness:
        return CopyMarkout(
            opportunity.opportunity_id, opportunity.instrument_id,
            horizon, direction, "PENDING", ready_at, None, None,
            None, None, None, None, cost_bps, "OUTCOME_NOT_YET_MATURE",
        )
    quotes = sorted(
        (p for p in price_points if p.instrument_id == opportunity.instrument_id
         and p.known_at <= (now or datetime.max.replace(tzinfo=UTC))),
        key=lambda p: p.known_at,
    )
    entry = next((p for p in quotes if p.known_at >= ready_at), None)
    exit_price = next((p for p in quotes if p.known_at >= maturity), None)
    if (
        entry is None or exit_price is None
        or entry.known_at > ready_at + max_quote_lateness
        or exit_price.known_at > maturity + max_quote_lateness
    ):
        return CopyMarkout(
            opportunity.opportunity_id, opportunity.instrument_id,
            horizon, direction, "CENSORED", ready_at,
            entry.known_at if entry else None, exit_price.known_at if exit_price else None,
            entry.price if entry else None, exit_price.price if exit_price else None,
            None, None, cost_bps, "PRICE_MISSING_OR_TOO_LATE",
        )
    gross = Decimal(direction) * (exit_price.price / entry.price - 1)
    return CopyMarkout(
        opportunity.opportunity_id, opportunity.instrument_id,
        horizon, direction, "VALID_HYPOTHETICAL", ready_at,
        entry.known_at, exit_price.known_at, entry.price, exit_price.price,
        gross, gross - cost_bps / Decimal(10_000), cost_bps, None,
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS copy_markouts (
 opportunity_id TEXT NOT NULL,
 horizon TEXT NOT NULL,
 status TEXT NOT NULL,
 value_json TEXT NOT NULL,
 outcome_known_at TEXT NOT NULL,
 PRIMARY KEY(opportunity_id,horizon)
);
CREATE TABLE IF NOT EXISTS copy_controls (
 opportunity_id TEXT NOT NULL, horizon TEXT NOT NULL,
 control_revision TEXT NOT NULL,
 value_json TEXT NOT NULL,
 PRIMARY KEY(opportunity_id,horizon,control_revision)
);
"""


class CopyResearchStore:
    """Immutable outcome and preregistered market-only comparisons."""

    def __init__(self, path: Path | str) -> None:
        self.db = sqlite3.connect(str(path), timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def record(self, outcome: CopyMarkout, *, recorded_at: datetime) -> bool:
        if recorded_at.tzinfo is None:
            raise ValueError("UTC recording time required")
        if outcome.status == "PENDING":
            return False
        data = json.dumps(outcome.jsonable(), sort_keys=True, separators=(",", ":"))
        with self.db:
            current = self.db.execute(
                "SELECT value_json FROM copy_markouts WHERE opportunity_id=? AND horizon=?",
                (outcome.opportunity_id, outcome.horizon),
            ).fetchone()
            if current is not None:
                if str(current[0]) != data:
                    raise ValueError("immutable copy outcome changed")
                return False
            self.db.execute(
                "INSERT INTO copy_markouts VALUES (?,?,?,?,?)",
                (outcome.opportunity_id, outcome.horizon, outcome.status, data,
                 recorded_at.isoformat()),
            )
            return True

    def attach_control(self, control: PairedControl) -> bool:
        data = json.dumps({
            "market_only_net_return": str(control.market_only_net_return),
            "control_family":control.control_family,
            "control_revision":control.control_revision,
            "eligible_at":control.eligible_at.isoformat(),
        }, sort_keys=True, separators=(",", ":"))
        if control.eligible_at.tzinfo is None:
            raise ValueError("control eligibility timestamp needs timezone")
        with self.db:
            row = self.db.execute(
                "SELECT value_json FROM copy_controls WHERE opportunity_id=? "
                "AND horizon=? AND control_revision=?",
                (control.opportunity_id,control.horizon,control.control_revision),
            ).fetchone()
            if row:
                if str(row[0]) != data:
                    raise ValueError("control manifest cannot be rewritten")
                return False
            self.db.execute(
                "INSERT INTO copy_controls VALUES (?,?,?,?)",
                (control.opportunity_id,control.horizon,control.control_revision,data),
            )
        return True

    def summary(self, *, horizon: str, control_revision: str | None = None) -> dict[str, Any]:
        rows = self.db.execute(
            "SELECT opportunity_id,value_json FROM copy_markouts "
            "WHERE horizon=? AND status='VALID_HYPOTHETICAL' ORDER BY opportunity_id",
            (horizon,),
        ).fetchall()
        if not rows:
            return {"horizon":horizon,"n":0,"qualification":"INCONCLUSIVE_NO_MATURED_SAMPLES"}
        controls = {}
        if control_revision is not None:
            controls = {
                str(r["opportunity_id"]): json.loads(str(r["value_json"]))
                for r in self.db.execute(
                    "SELECT opportunity_id,value_json FROM copy_controls "
                    "WHERE horizon=? AND control_revision=?",
                    (horizon, control_revision),
                )
            }
        paired = []
        for r in rows:
            outcome = json.loads(str(r["value_json"]))
            base = controls.get(str(r["opportunity_id"]))
            if base is not None:
                paired.append(
                    (outcome["instrument_id"],
                     Decimal(outcome["hypothetical_net_return"])
                     - Decimal(base["market_only_net_return"]))
                )
        return {
            "horizon":horizon,
            "hypothetical_n":len(rows),
            "paired_controls":len(paired),
            "paired_mean_incremental_bps":str(
                sum((x[1] for x in paired),Decimal(0)) / Decimal(len(paired))
                * Decimal(10000)
            ) if paired else None,
            "qualification": (
                "RESEARCH_DIAGNOSTIC_ONLY_NO_FORWARD_GATE"
                if paired else "INCONCLUSIVE_NO_MATCHED_CONTROLS"
            ),
            "executable_pnl_qualified":False,
        }


def shrink_wallet_skill(
    paired_deltas: Iterable[tuple[str, datetime, Decimal]],
    *,
    evaluation_at: datetime,
    min_independent_days: int = 5,
    prior_strength: Decimal = Decimal(20),
) -> dict[str, dict[str, str | int | None]]:
    """Lagged conditional score, strictly excluding observations at/after cutoff.

    This is a research descriptive score, NOT a trade weight or p-value.
    """
    if evaluation_at.tzinfo is None:
        raise ValueError("evaluation cutoff needs timezone")
    observations: dict[str,dict[str,list[Decimal]]] = {}
    for wallet,known_at,delta in paired_deltas:
        if known_at.tzinfo is None or known_at >= evaluation_at:
            continue
        day=known_at.astimezone(UTC).date().isoformat()
        observations.setdefault(wallet,{}).setdefault(day,[]).append(delta)
    result: dict[str, dict[str, str | int | None]] = {}
    for wallet,day_map in observations.items():
        independent_days=len(day_map)
        sample_count=sum(len(v) for v in day_map.values())
        if independent_days < min_independent_days:
            result[wallet] = {
                "status":"UNKNOWN_INSUFFICIENT_INDEPENDENT_DAYS",
                "n":sample_count,"days":independent_days,"shrunk_delta_bps":None,
            }
            continue
        # A day contributes one average regardless of child-fill frequency.
        daily_means=[sum(v,Decimal(0))/Decimal(len(v)) for v in day_map.values()]
        shrunk=sum(daily_means,Decimal(0))/(Decimal(independent_days)+prior_strength)
        result[wallet] = {
            "status":"RESEARCH_DESCRIPTIVE_ONLY",
            "n":sample_count,"days":independent_days,
            "shrunk_delta_bps":str(shrunk*Decimal(10000)),
        }
    return result


def frozen_control_revision(name: str, parameters: dict[str,Any]) -> str:
    if not name:
        raise ValueError("control family required")
    payload = json.dumps({"name":name,"parameters":parameters},sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:20]
