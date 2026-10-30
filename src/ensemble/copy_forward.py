"""WP4 immutable forward hourly hypothesis and diagnostic outcome engine.

The frozen WP3 protocol is authority. These are unqualified research watches,
not trading signals. Never reconstruct an hourly forecast after the hour,
never rewrite source decisions, and never infer executable PnL from 1m mids.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

from .copy_economics import BASELINES, _quotes, control_directions
from .copy_preregister import _sha

DATA = Path("/home/quant/.local/share/copytrade")
PROTOCOL = DATA / "reports/wp3-20261030/preregistration/protocol.json"
ROSTER = PROTOCOL.with_name("wallet-universe.PRIVATE.json")
FORWARD_DB = DATA / "copy-forward-hourly.db"
EVAL_REVISION = "wp4-mid-diagnostic-v1"
ACTION_DELAY = timedelta(seconds=5)
QUOTE_LATENESS = timedelta(seconds=90)
HOLD = timedelta(hours=1)
PREFX = "hyperliquid:mainnet:perp:default:"
MAX_SOURCE_ROWS = 20_000
_SCHEMA = """
CREATE TABLE IF NOT EXISTS forward_cuts(
  period TEXT PRIMARY KEY, cut_id TEXT NOT NULL UNIQUE,
  protocol_id TEXT NOT NULL, decision_at TEXT NOT NULL,
  source_ledger_id TEXT NOT NULL, source_ledger_seq INTEGER NOT NULL,
  shadow_seq INTEGER NOT NULL, upstream_known_at TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forward_arms(
  arm_id TEXT PRIMARY KEY, cut_id TEXT NOT NULL,
  instrument_id TEXT NOT NULL, direction INTEGER NOT NULL,
  disposition TEXT NOT NULL, payload_json TEXT NOT NULL,
  UNIQUE(cut_id,instrument_id),
  FOREIGN KEY (cut_id) REFERENCES forward_cuts(cut_id)
);
CREATE INDEX IF NOT EXISTS idx_forward_arms_cut ON forward_arms(cut_id);
CREATE TABLE IF NOT EXISTS forward_delivery(
  delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
  cut_id TEXT NOT NULL, attempt_at TEXT NOT NULL, status TEXT NOT NULL,
  acknowledged_at TEXT, UNIQUE(cut_id,attempt_at),
  FOREIGN KEY (cut_id) REFERENCES forward_cuts(cut_id)
);
CREATE TABLE IF NOT EXISTS forward_evaluations(
  evaluation_id TEXT PRIMARY KEY, arm_id TEXT NOT NULL,
  evaluation_revision TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, supersedes TEXT, payload_json TEXT NOT NULL,
  UNIQUE(arm_id,evaluation_revision),
  FOREIGN KEY (arm_id) REFERENCES forward_arms(arm_id)
);
CREATE INDEX IF NOT EXISTS idx_forward_eval_arm ON forward_evaluations(arm_id);
CREATE TRIGGER IF NOT EXISTS no_update_cuts BEFORE UPDATE ON forward_cuts
BEGIN SELECT RAISE(ABORT,'immutable decision cuts'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_cuts BEFORE DELETE ON forward_cuts
BEGIN SELECT RAISE(ABORT,'immutable decision cuts'); END;
CREATE TRIGGER IF NOT EXISTS no_update_arms BEFORE UPDATE ON forward_arms
BEGIN SELECT RAISE(ABORT,'immutable forecast arms'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_arms BEFORE DELETE ON forward_arms
BEGIN SELECT RAISE(ABORT,'immutable forecast arms'); END;
CREATE TRIGGER IF NOT EXISTS no_update_delivery BEFORE UPDATE ON forward_delivery
BEGIN SELECT RAISE(ABORT,'append-only delivery'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_delivery BEFORE DELETE ON forward_delivery
BEGIN SELECT RAISE(ABORT,'append-only delivery'); END;
CREATE TRIGGER IF NOT EXISTS no_update_evaluations BEFORE UPDATE ON forward_evaluations
BEGIN SELECT RAISE(ABORT,'immutable evaluations'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_evaluations BEFORE DELETE ON forward_evaluations
BEGIN SELECT RAISE(ABORT,'immutable evaluations'); END;
"""


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return dt.astimezone(UTC)


def period_key(now: datetime) -> str:
    t = now.astimezone(UTC)
    return f"{t:%Y-%m-%dT%H}:00Z"


class FrozenEvidence:
    """All study definitions and the exact wallet universe locked before OOS."""

    def __init__(self, *, protocol_path: Path = PROTOCOL) -> None:
        if not protocol_path.exists():
            raise FileNotFoundError("frozen protocol unavailable")
        sealed = json.loads(protocol_path.read_text(encoding="utf-8"))
        if (sealed.get("content_sha256") != _sha(sealed.get("protocol"))
                or sealed.get("protocol_id") !=
                "wp3:" + str(sealed.get("content_sha256"))[:20]):
            raise ValueError("frozen protocol hash conflict")
        policy = sealed["protocol"]["policy"]
        comparison = sealed["protocol"]["comparison"]
        if (sealed["protocol"]["schema"] != "copytrade-WP3-frozen-protocol-v1"
                or policy["source_opportunity_revision"] != "DI2-shadow-v2"
                or policy["primary"] != "hourly_digest_directional_watch"
                or comparison["primary_horizon"] != "1h"
                or comparison["baseline_families"] != list(BASELINES)
                or comparison["cost_bps_round_trip_base"] != 10
                or policy["hourly_window"] != "PRECEDING_60M_KNOWN_AT"
                or policy["hourly_decision_time"] != "EVERY_HH_05_ASIA_BANGKOK"
                or comparison["price_source"] != "MINUTE_MID_DIAGNOSTIC_ONLY"):
            raise ValueError("unsupported frozen research contract; require a new study")
        roster = json.loads(protocol_path.with_name("wallet-universe.PRIVATE.json").read_text())
        if _sha(roster) != sealed["protocol"]["source_cut"]["universe_sha256"]:
            raise ValueError("frozen wallet roster changed")
        wallets = roster.get("wallets")
        if (not isinstance(wallets, list)
                or len(wallets) != sealed["protocol"]["source_cut"]["universe_wallets"]
                or wallets != sorted(set(wallets))):
            raise ValueError("invalid frozen wallet roster")
        self.sealed = sealed
        self.policy = policy
        self.comparison = comparison
        self.wallets: frozenset[str] = frozenset(wallets)
        self.protocol_id = str(sealed["protocol_id"])
        self.start = parse_utc(sealed["protocol"]["holdout"]["start_inclusive"])
        self.end = parse_utc(sealed["protocol"]["holdout"]["end_exclusive"])
        self.ledger_id = str(sealed["protocol"]["source_cut"]["ledger_id"])

    def valid_window(self, now: datetime) -> bool:
        if now.tzinfo is None:
            raise ValueError("decision clock must be aware")
        return self.start <= now.astimezone(UTC) < self.end


class ForwardStore:
    def __init__(self, path: Path = FORWARD_DB):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            self.db.executescript(_SCHEMA)
        if path.exists():
            os.chmod(path, 0o600)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def get_cut(self, period: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT payload_json FROM forward_cuts WHERE period=?", (period,)
        ).fetchone()
        return json.loads(str(row[0])) if row else None

    def record_cut(
        self, *, cut: dict[str, Any], arms: list[dict[str, Any]],
    ) -> bool:
        """A period can only ever have one frozen policy/evidence cut."""
        data = canonical(cut)
        with self.db:
            current = self.db.execute(
                "SELECT payload_json FROM forward_cuts WHERE period=?",
                (cut["period"],),
            ).fetchone()
            if current:
                # Retries must reuse the first as-known cut, not recreate history
                # with a later source cursor.
                return False
            self.db.execute(
                "INSERT INTO forward_cuts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cut["period"], cut["cut_id"], cut["protocol_id"],
                 cut["decision_at"], cut["source_ledger_id"],
                 cut["source_ledger_seq"], cut["shadow_seq"],
                 cut["upstream_known_at"], cut["evidence_sha256"], data),
            )
            for arm in arms:
                self.db.execute(
                    "INSERT INTO forward_arms VALUES (?,?,?,?,?,?)",
                    (arm["arm_id"], cut["cut_id"], arm["instrument_id"],
                     arm["direction"], arm["disposition"], canonical(arm)),
                )
        return True

    def append_delivery(
        self, *, cut_id: str, attempted: datetime, status: str,
        acknowledged: datetime | None = None,
    ) -> None:
        if status not in ("ACKNOWLEDGED", "UNCONFIRMED", "REJECTED"):
            raise ValueError("invalid delivery status")
        if attempted.tzinfo is None or (acknowledged is not None and
                                         (acknowledged.tzinfo is None or acknowledged < attempted)):
            raise ValueError("impossible delivery clock")
        if status == "ACKNOWLEDGED" and acknowledged is None:
            raise ValueError("a delivery must have actual acknowledgement time")
        with self.db:
            decision=self.db.execute(
                "SELECT decision_at FROM forward_cuts WHERE cut_id=?",
                (cut_id,),
            ).fetchone()
            if decision is None or attempted < parse_utc(str(decision[0])):
                raise ValueError("cannot record a delivery before its frozen decision")
            self.db.execute(
                "INSERT INTO forward_delivery (cut_id,attempt_at,status,acknowledged_at)"
                " VALUES (?,?,?,?)",
                (cut_id, attempted.isoformat(), status,
                 acknowledged.isoformat() if acknowledged else None),
            )

    def get_arms(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT a.*,c.decision_at,c.period,c.protocol_id "
            "FROM forward_arms a JOIN forward_cuts c ON c.cut_id=a.cut_id "
            "ORDER BY c.decision_at,a.instrument_id"
        ).fetchall()

    def record_evaluation(self, *, arm_id: str, revision: str,
                          result: dict[str, Any], created_at: datetime,
                          supersedes: str | None = None) -> bool:
        if created_at.tzinfo is None or not revision:
            raise ValueError("unqualified evaluation time or revision")
        key = "eval:" + stable_hash([arm_id, revision])[:28]
        data = canonical(result)
        with self.db:
            existing = self.db.execute(
                "SELECT payload_json FROM forward_evaluations "
                "WHERE arm_id=? AND evaluation_revision=?",
                (arm_id, revision),
            ).fetchone()
            if existing is not None:
                if existing[0] != data:
                    raise ValueError("immutable evaluation revision conflict")
                return False
            horizon = revision.rsplit(":", 1)[-1]
            if horizon not in ("15m", "1h", "4h"):
                raise ValueError("unregistered evaluation horizon")
            prior = self.db.execute(
                "SELECT evaluation_id FROM forward_evaluations WHERE arm_id=? "
                "AND evaluation_revision LIKE ? "
                "ORDER BY created_at DESC LIMIT 1",
                (arm_id,"%:" + horizon),
            ).fetchone()
            if (prior is None and supersedes is not None) or (
                prior is not None and supersedes != prior[0]
            ):
                raise ValueError("evaluation amendments require exact supersedes")
            self.db.execute(
                "INSERT INTO forward_evaluations VALUES (?,?,?,?,?,?,?)",
                (key, arm_id, revision, result["status"],
                 created_at.isoformat(), supersedes, data),
            )
            return True


def build_arm_set(
    *, frozen: FrozenEvidence, journal_db: Path, period: str,
    at: datetime, shadow_seq: int,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """One fully as-known snapshot across all eligible instruments and wallets."""
    begin = (at - timedelta(hours=1)).isoformat()
    clock = at.isoformat()
    with closing(sqlite3.connect(
        f"file:{journal_db}?mode=ro", uri=True, timeout=15,
    )) as db:
        db.row_factory = sqlite3.Row
        gap = db.execute(
            "SELECT known_at,ledger_seq FROM source_coverage_barriers "
            "WHERE ledger_id=? AND ledger_seq<=? AND known_at>? "
            "AND known_at<=? ORDER BY ledger_seq DESC LIMIT 1",
            (frozen.ledger_id, shadow_seq, begin, clock),
        ).fetchone()
        rows = db.execute(
            "SELECT d.instrument_id,d.wallet_id,d.first_known_at,"
            "d.policy_revision,d.blocker,d.payload_json,u.known_at,u.ledger_seq,"
            "u.status,u.source_action,u.reason,u.source_id "
            "FROM opportunity_updates u JOIN opportunity_decisions d "
            "ON d.opportunity_id=u.opportunity_id "
            "WHERE u.known_at>? AND u.known_at<=? AND u.ledger_seq<=? "
            "ORDER BY u.ledger_seq DESC,u.rowid DESC LIMIT ?",
            (begin, clock, shadow_seq, MAX_SOURCE_ROWS + 1),
        ).fetchall()
    if len(rows) > MAX_SOURCE_ROWS:
        raise RuntimeError("truncated one-hour source window; refuse a candidate")
    roster = frozen.wallets
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    accepted = 0
    for r in rows:
        coin = str(r["instrument_id"])
        wallet = str(r["wallet_id"]).lower()
        if (not coin.startswith(PREFX) or wallet not in roster
                or r["policy_revision"] != "DI2-shadow-v2"):
            continue
        if gap is not None and str(r["known_at"]) <= str(gap["known_at"]):
            continue
        # Underlying sources without a frozen accepted coverage assertion
        # must never cast a directional vote.
        detail = json.loads(str(r["payload_json"]))
        if detail.get("source_coverage") not in ("Seeded", "KnownFlat", "SUPPORTED"):
            continue
        scope = (wallet, coin)
        if scope in latest:
            continue
        latest[scope] = {
            "wallet": wallet, "instrument": coin,
            "action": str(r["source_action"]),
            "status": str(r["status"]),
            "known_at": str(r["known_at"]),
            "ledger_seq": int(r["ledger_seq"]),
            "blocker": r["blocker"], "reason": r["reason"],
            "initial_at": str(r["first_known_at"]),
            "source_id": str(r["source_id"]),
        }
        accepted += 1
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"long": set(), "short": set(), "unwind": set(),
                 "sources": [], "evidence_count": 0}
    )
    for v in latest.values():
        scope = grouped[v["instrument"]]
        scope["evidence_count"] += 1
        scope["sources"].append([v["source_id"], v["ledger_seq"]])
        action = v["action"]
        if action.startswith(("REDUCE_", "CLOSE_")) or v["status"] == "INVALIDATED":
            scope["unwind"].add(v["wallet"])
        elif (v["status"] in ("ACTIVE", "UPDATED")
              and v["blocker"] is None and v["reason"] is None
              and action.startswith(("OPEN_", "ADD_", "FLIP_TO_"))
              and not (gap is not None and v["initial_at"] <= str(gap["known_at"]))):
            side = "long" if action.endswith("LONG") else "short" if action.endswith("SHORT") else None
            if side:
                scope[side].add(v["wallet"])
    arms: list[dict[str, Any]] = []
    for instrument in sorted(grouped):
        s = grouped[instrument]
        long_count, short_count = len(s["long"]), len(s["short"])
        unwind_count = len(s["unwind"])
        majority = max(long_count, short_count)
        minority = min(long_count, short_count)
        meets = (majority >= 3 and majority >= 2 * max(1, minority)
                 and majority - minority >= 2)
        caution = unwind_count >= majority and unwind_count > 0
        # A feed-wide history gap can invalidate apparently coherent subsequent
        # wallet activity. Only a separate qualified recovery protocol could
        # reopen this study window; a new wallet snapshot does not repair it.
        direction = (
            (1 if long_count > short_count else -1)
            if meets and not caution and gap is None else 0
        )
        reason = ("SOURCE_GAP_IN_WINDOW" if gap is not None
                  else "UNWIND_VETO" if caution
                  else "MIXED_OR_INSUFFICIENT_SUPPORT" if direction == 0
                  else "FROZEN_UNQUALIFIED_DIRECTIONAL_HYPOTHESIS")
        arm = {
            "protocol_id": frozen.protocol_id,
            "period": period,
            "instrument_id": instrument,
            "direction": direction,
            "disposition": "CANDIDATE_RESEARCH_ONLY" if direction else "ABSTAIN",
            "reason": reason,
            "support_long_wallets": long_count,
            "support_short_wallets": short_count,
            "unwind_wallets": unwind_count,
            "raw_source_evidence_count": s["evidence_count"],
            "source_evidence_sha256": stable_hash(sorted(s["sources"])),
            "independent_cluster_count": None,
            "source_history_qualified": False,
            "expected_return": None,
            "size": None,
            "execution_authorized": False,
        }
        arm["arm_id"] = "hour:" + stable_hash(
            [frozen.protocol_id, period, instrument])[:28]
        arms.append(arm)
    if not arms:
        arms.append({
            "arm_id": "hour:" + stable_hash([frozen.protocol_id,period,"NONE"])[:28],
            "protocol_id": frozen.protocol_id,
            "period": period,
            "instrument_id": "NONE",
            "direction": 0,
            "disposition": "ABSTAIN",
            "reason": (
                "SOURCE_GAP_IN_WINDOW" if gap is not None
                else "NO_ELIGIBLE_AS_KNOWN_SOURCE_EVENTS"
            ),
            "independent_cluster_count": None,
            "source_history_qualified": False,
            "expected_return": None,
            "size": None,
            "execution_authorized": False,
        })
    evidence_hash = stable_hash([
        sorted((v["source_id"],v["ledger_seq"]) for v in latest.values()),
        gap["ledger_seq"] if gap is not None else None,
        shadow_seq, frozen.protocol_id,
    ])
    info = {
        "source_event_rows": len(rows), "eligible_latest_wallet_coin": accepted,
        "source_gap_in_window": gap is not None,
        "source_gap_ledger_seq": int(gap["ledger_seq"]) if gap is not None else None,
        "instruments": len(grouped),
        "candidate_count": sum(arm["direction"] != 0 for arm in arms),
        "abstention_count": sum(arm["direction"] == 0 for arm in arms),
        "bounded": True,
    }
    return arms, evidence_hash, info


def record_hourly(
    *, frozen: FrozenEvidence, forward_db: Path, journal_db: Path,
    source_db: Path, decided_at: datetime, period: str | None = None,
    price_db: Path = DATA / "market-reference.db",
    physical_clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Capture once, before Telegram send. Do not reconstruct missed periods."""
    if decided_at.tzinfo is None:
        raise ValueError("cannot freeze a naive time")
    now = decided_at.astimezone(UTC)
    # A missed hourly decision cannot be reconstructed by passing a historical
    # as-of time into the API: require a real-time invocation. The injectable
    # clock exists only for deterministic tests; the service and CLI use UTC now.
    actual_now = (physical_clock or (lambda: datetime.now(UTC)))()
    if (actual_now.tzinfo is None or
            abs((actual_now.astimezone(UTC) - now).total_seconds()) > 90):
        raise RuntimeError("cannot backfill a non-live hourly research decision")
    period = period or period_key(now)
    if period != period_key(now):
        raise ValueError("never backfill past hourly periods")
    if not frozen.valid_window(now):
        return {"status": "OUTSIDE_FROZEN_HOLDOUT", "period": period}
    if not 5 <= now.minute <= 8:
        return {"status": "MISSED_HOURLY_DECISION_WINDOW", "period": period}
    with ForwardStore(forward_db) as store:
        existing = store.get_cut(period)
        if existing is not None:
            saved_arms = store.db.execute(
                "SELECT instrument_id,direction FROM forward_arms WHERE cut_id=? "
                "AND direction IN (-1,1) ORDER BY instrument_id",
                (existing["cut_id"],),
            ).fetchall()
            return {
                "status": "ALREADY_RECORDED",
                "period": period, "cut_id": existing["cut_id"],
                "candidate_count": existing["counts"]["candidate_count"],
                "candidate_preview": [
                    [str(a[0]),int(a[1])] for a in saved_arms
                ],
            }
        # Read the immutable journal cursor BEFORE the moving upstream ledger
        # head. Reading source first can produce an impossible cursor>source
        # race even while both independent workers are perfectly healthy.
        ledger_id = frozen.ledger_id
        with closing(sqlite3.connect(
            f"file:{journal_db}?mode=ro", uri=True, timeout=15,
        )) as journal:
            row = journal.execute(
                "SELECT last_seq FROM opportunity_state WHERE ledger_id=?",
                (ledger_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("prospective source projection not established")
            shadow_seq = int(row[0])
        with closing(sqlite3.connect(
            f"file:{source_db}?mode=ro", uri=True, timeout=15,
        )) as source:
            meta = source.execute(
                "SELECT ledger_id,current_seq FROM ledger_meta LIMIT 1"
            ).fetchone()
            latest = source.execute(
                "SELECT known_at FROM outbox_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if meta is None or latest is None:
                raise RuntimeError("source ledger unavailable for forward cut")
            actual_id, latest_seq = str(meta[0]), int(meta[1])
            latest_known = parse_utc(str(latest[0]))
        if actual_id != ledger_id:
            raise RuntimeError("source ledger identity differs from frozen protocol")
        if (not -timedelta(minutes=2) <= now - latest_known <=
                timedelta(minutes=5)):
            raise RuntimeError("stale or future source-ledger observation")
        if shadow_seq < frozen.sealed["protocol"]["source_cut"]["shadow_cursor"]:
            raise RuntimeError("projection rewound before frozen study cut")
        if shadow_seq > latest_seq or latest_seq - shadow_seq > 200:
            raise RuntimeError("source projection lag blocks forward recording")
        arms, evidence_hash, counts = build_arm_set(
            frozen=frozen, journal_db=journal_db,
            period=period, at=now, shadow_seq=shadow_seq,
        )
        # Freeze market-only directions at the SAME knowable decision cut.
        # Reference prices are insert-only; still persist exact direction/vintage
        # so later evaluator revisions cannot alter a historical comparator.
        if not price_db.exists():
            raise RuntimeError("no known-at market price store for forward controls")
        with closing(sqlite3.connect(
            f"file:{price_db}?mode=ro", uri=True, timeout=15,
        )) as prices:
            for arm in arms:
                if arm["instrument_id"] == "NONE":
                    directions = dict.fromkeys(BASELINES, None)
                    directions["cash"] = 0
                else:
                    points = _quotes(
                        prices, instrument=arm["instrument_id"], cutoff=now
                    )
                    directions = control_directions(known_at=now, quotes=points)
                arm["frozen_baseline_directions"] = directions
                arm["baseline_frozen_at"] = now.isoformat()
                arm["baseline_vintage"] = "REFERENCE_MID_KNOWN_AT_ONLY"
        baseline_hash = stable_hash([
            [arm["arm_id"],arm["frozen_baseline_directions"]] for arm in arms
        ])
        cut = {
            "period": period,
            "cut_id": "cut:" + stable_hash([frozen.protocol_id, period])[:28],
            "protocol_id": frozen.protocol_id,
            "protocol_sha256": frozen.sealed["content_sha256"],
            "decision_at": now.isoformat(),
            "source_ledger_id": ledger_id,
            "source_ledger_seq": latest_seq,
            "shadow_seq": shadow_seq,
            "upstream_known_at": latest_known.isoformat(),
            "evidence_sha256": evidence_hash,
            "baseline_directions_sha256": baseline_hash,
            "counts": counts,
            "policy_revision": "DI2-shadow-v2",
            "implementation_commit": os.getenv(
                "COPYTRADE_RELEASE_ENSEMBLE_SHA", "DEV_UNPINNED"
            ),
            "delivery_at": None,  # obtained only from later Telegram acknowledgement
            "actual_entry_at": None,
            "price_model": "MID_ONLY_NO_EXECUTABLE_QUOTE",
            "predictive_qualified": False,
            "execution_authorized": False,
        }
        created = store.record_cut(cut=cut, arms=arms)
        if not created:
            stored=store.get_cut(period)
            if stored is None:
                raise RuntimeError("concurrent cut identity disappeared")
            original=store.db.execute(
                "SELECT instrument_id,direction FROM forward_arms "
                "WHERE cut_id=? AND direction IN (-1,1) ORDER BY instrument_id",
                (stored["cut_id"],),
            ).fetchall()
            return {
                "status": "ALREADY_RECORDED",
                "period": period, "cut_id": stored["cut_id"],
                "candidate_count": stored["counts"]["candidate_count"],
                "candidate_preview": [[str(x[0]), int(x[1])] for x in original],
            }
        return {
            "status": "RECORDED_RESEARCH_ONLY",
            "period": period, "cut_id": cut["cut_id"],
            "candidate_count": counts["candidate_count"],
            "abstention_count": counts["abstention_count"],
            "source_seq": shadow_seq,
            "candidate_preview": [
                [str(a["instrument_id"]),int(a["direction"])]
                for a in arms if a["direction"] != 0
            ],
            "evidence_sha256": evidence_hash,
            "baseline_directions_sha256": baseline_hash,
        }


def verify_forward(
    *, frozen: FrozenEvidence, forward_db: Path,
) -> dict[str, Any]:
    if not forward_db.exists():
        return {
            "status": "NOT_YET_STARTED",
            "protocol_id": frozen.protocol_id,
            "holdout_start": frozen.start.isoformat(),
            "predictive_qualified": False,
        }
    with ForwardStore(forward_db) as store:
        rows = store.db.execute(
            "SELECT COUNT(*),MIN(decision_at),MAX(decision_at) FROM forward_cuts"
        ).fetchone()
        arms = store.db.execute(
            "SELECT disposition,COUNT(*) FROM forward_arms GROUP BY disposition"
        ).fetchall()
        outcomes = store.db.execute(
            "SELECT status,COUNT(*) FROM forward_evaluations "
            "WHERE evaluation_revision LIKE ? GROUP BY status", (EVAL_REVISION+":%",),
        ).fetchall()
        delivery = store.db.execute(
            "SELECT status,COUNT(*) FROM forward_delivery GROUP BY status"
        ).fetchall()
        return {
            "status": "RESEARCH_ONLY_NOT_QUALIFIED",
            "protocol_id": frozen.protocol_id,
            "cuts": int(rows[0]), "first_at": rows[1], "last_at": rows[2],
            "arms": {str(r[0]): int(r[1]) for r in arms},
            "outcomes": {str(r[0]): int(r[1]) for r in outcomes},
            "delivery": {str(r[0]): int(r[1]) for r in delivery},
            "predictive_qualified": False,
            "executable_prices_qualified": False,
            "financial_execution": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "emit"))
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    parser.add_argument("--forward", type=Path, default=FORWARD_DB)
    parser.add_argument("--journal", type=Path, default=DATA / "copy-opportunities-live.db")
    parser.add_argument("--ledger", type=Path, default=DATA / "observation.db")
    parser.add_argument("--prices",type=Path,default=DATA/"market-reference.db")
    args = parser.parse_args()
    frozen = FrozenEvidence(protocol_path=args.protocol)
    if args.command == "verify":
        print(json.dumps(verify_forward(frozen=frozen,forward_db=args.forward),
                         sort_keys=True, indent=2))
        return
    print(json.dumps(record_hourly(
        frozen=frozen,forward_db=args.forward,journal_db=args.journal,
        source_db=args.ledger,price_db=args.prices,decided_at=datetime.now(UTC),
    ), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
