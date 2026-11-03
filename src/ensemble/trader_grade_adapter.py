"""Optional read-only Trader Intelligence digest overlay.

The existing descriptive watch remains the production default. This adapter
never fetches a broker account, sends Telegram, computes a grade, or trades.
An invalid snapshot returns None and the caller preserves its original text.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from html import escape
from pathlib import Path
from typing import Any

SCHEMA = "trader_intelligence_v1"
HEX64 = re.compile(r"[a-f0-9]{64}\Z")
MARKET = re.compile(r"hyperliquid/mainnet:perp:default/([A-Za-z0-9_.-]+)\Z")
GRADE_R = {"A": "1", "B": "0.5", "C": "0", "X": "0"}
ORIGINS = {"HISTORICAL_PROXY", "PROSPECTIVE_OBSERVED", "PROSPECTIVE_VALIDATED"}


def _utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError("INVALID_GRADE_CLOCK")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("INVALID_GRADE_CLOCK") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("NAIVE_GRADE_CLOCK")
    return dt.astimezone(UTC)


def _unique_json(pairs: list[tuple[str, Any]]) -> dict:
    result: dict[str, Any] = {}
    for k, v in pairs:
        if k in result:
            raise ValueError("DUPLICATE_GRADE_JSON_FIELD")
        result[k] = v
    return result


def source_fingerprint(board: dict, recent_board: dict) -> str:
    """Same byte-stable SHA256 scheme as copytrade-pm's canonical JSON."""
    wire = json.dumps(
        {"four_hour": board, "one_hour": recent_board},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(wire).hexdigest()


def _load_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("GRADE_ARTIFACT_UNTRUSTED_OR_OVERSIZED")
    # Reject symlink replacement races between inspection and open.
    import os

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("GRADE_ARTIFACT_OVERSIZED")
    obj = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_json,
        parse_constant=lambda x: (_ for _ in ()).throw(ValueError("NONFINITE_GRADE_JSON")),
    )
    if not isinstance(obj, dict):
        raise TypeError("GRADE_ARTIFACT_NOT_OBJECT")
    return obj


def inspect_grade_artifact(
    path: Path,
    *,
    board: dict,
    recent_board: dict,
    now: datetime,
    expected_model_sha256: str,
    expected_policy_sha256: str,
) -> dict[tuple[str, str], dict] | None:
    """Strict all-or-nothing grade consumer; None means descriptive fallback.

    An operator must separately pin the exact model and policy hashes. A file
    cannot opt itself into trusted A/B by claiming VALIDATED or PROVISIONAL.
    """
    try:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("NAIVE_DECISION_CLOCK")
        if not all(
            isinstance(x, str) and HEX64.fullmatch(x)
            for x in (expected_model_sha256, expected_policy_sha256)
        ):
            raise ValueError("UNPINNED_GRADE_MODEL_OR_POLICY")
        obj = _load_json(path)
        if set(obj) != {
            "schema",
            "decision_at",
            "source_sha256",
            "research_only",
            "order_authorized",
            "assessments",
        }:
            raise ValueError("INCOMPATIBLE_GRADE_DOCUMENT_KEYS")
        if (
            obj["schema"] != SCHEMA
            or obj["research_only"] is not True
            or obj["order_authorized"] is not False
        ):
            raise ValueError("INCOMPATIBLE_GRADE_DOCUMENT")
        cut = _utc(obj["decision_at"])
        if cut != now.astimezone(UTC):
            raise ValueError("STALE_OR_FUTURE_GRADE_DOCUMENT")
        if cut != _utc(board["as_of"]) or cut != _utc(recent_board["as_of"]):
            raise ValueError("BOARD_DECISION_CUT_MISMATCH")
        if obj["source_sha256"] != source_fingerprint(board, recent_board):
            raise ValueError("SOURCE_BOARD_FINGERPRINT_MISMATCH")
        values = obj["assessments"]
        if not isinstance(values, list) or len(values) > 40:
            raise ValueError("GRADE_CARDINALITY_INVALID")
        by_market = {c["coin"]: c for c in board["coins"]}
        result: dict[tuple[str, str], dict] = {}
        seen_keys: set[tuple[str, str]] = set()
        for a in values:
            if not isinstance(a, dict):
                raise TypeError("INVALID_GRADE_ROW")
            required = {
                "schema",
                "candidate_id",
                "instrument",
                "direction",
                "decision_at",
                "input_sha256",
                "model_sha256",
                "policy_sha256",
                "grade",
                "evidence_status",
                "claim_status",
                "reasons",
                "research_r",
                "independent_clusters",
                "qualified_contributors",
                "first_known_at",
                "latest_source_event_at",
                "follower_net_r",
                "follower_lcb_r",
                "research_only",
                "order_authorized",
            }
            if set(a) != required:
                raise ValueError("INCOMPATIBLE_GRADE_ROW_KEYS")
            if (
                a["schema"] != SCHEMA
                or a["research_only"] is not True
                or a["order_authorized"] is not False
                or _utc(a["decision_at"]) != cut
                or a["model_sha256"] != expected_model_sha256
                or a["policy_sha256"] != expected_policy_sha256
            ):
                raise ValueError("GRADE_MODEL_POLICY_CUT_CONFLICT")
            if not all(
                isinstance(a[k], str) and HEX64.fullmatch(a[k])
                for k in ("candidate_id", "input_sha256", "model_sha256", "policy_sha256")
            ):
                raise ValueError("INVALID_GRADE_HASH")
            match = MARKET.fullmatch(str(a["instrument"]))
            if match is None:
                raise ValueError("INVALID_GRADE_INSTRUMENT_NAMESPACE")
            coin = match.group(1)
            direction = a["direction"]
            if direction not in ("LONG", "SHORT"):
                raise ValueError("INVALID_GRADE_DIRECTION")
            if a["grade"] not in GRADE_R or a["research_r"] != GRADE_R[a["grade"]]:
                raise ValueError("INVALID_GRADE_R_BUDGET")
            if a["evidence_status"] not in ORIGINS or a["claim_status"] not in (
                "PROVISIONAL",
                "VALIDATED",
                "NOT_QUALIFIED",
            ):
                raise ValueError("INVALID_GRADE_EVIDENCE_STATUS")
            if a["claim_status"] == "VALIDATED" and (
                a["evidence_status"] != "PROSPECTIVE_VALIDATED"
            ):
                raise ValueError("UNSUPPORTED_VALIDATED_STATUS")
            if a["grade"] in ("A", "B"):
                # A user-writable JSON file and publicly known policy hashes
                # do not attest a calibrated independently observed outcome.
                # Positive grade display requires a distinct reviewed signed
                # producer/verifier; none is authorized by this shadow adapter.
                raise ValueError("POSITIVE_GRADE_PRODUCER_NOT_ATTESTED")
            if (
                not isinstance(a["reasons"], list)
                or len(a["reasons"]) > 16
                or any(not isinstance(r, str) or len(r) > 120 for r in a["reasons"])
            ):
                raise ValueError("INVALID_GRADE_REASON_CODES")
            for clock_name in ("first_known_at", "latest_source_event_at"):
                if a[clock_name] is not None and _utc(a[clock_name]) > cut:
                    raise ValueError("FUTURE_GRADE_EVIDENCE")
            for number_name in ("independent_clusters", "qualified_contributors"):
                count = a[number_name]
                if count is not None and (type(count) is not int or count < 0 or count > 100000):
                    raise ValueError("INVALID_GRADE_EVIDENCE_COUNT")
            if a["follower_net_r"] is not None or a["follower_lcb_r"] is not None:
                from decimal import Decimal

                for name in ("follower_net_r", "follower_lcb_r"):
                    if a[name] is not None and not Decimal(str(a[name])).is_finite():
                        raise ValueError("NONFINITE_FOLLOWER_ECONOMICS")
            key = (coin, direction)
            if key in seen_keys:
                raise ValueError("CONFLICTING_GRADE_REVISION")
            seen_keys.add(key)
            # An overlay must match an actual descriptive directional watch.
            # Stale assets and opposite-direction labels never appear.
            board_coin = by_market.get(coin)
            bias = "LONG_WATCH" if direction == "LONG" else "SHORT_WATCH"
            if board_coin is None or board_coin["observed_directional_bias"] != bias:
                continue
            result[key] = a
        return result
    except ValueError, TypeError, KeyError, OSError, UnicodeError, json.JSONDecodeError:
        return None


def grade_appendix(assessments: dict[tuple[str, str], dict]) -> str:
    """Escaped, bounded research overlay; never changes current decision policy."""
    if not assessments:
        return ""
    lines = ["\n<b>EXPERIMENTAL TRADER GRADES — RESEARCH ONLY</b>"]
    for (coin, direction), row in sorted(assessments.items())[:8]:
        if row["grade"] in ("A", "B") and row["claim_status"] != "VALIDATED":
            claim = "PROVISIONAL"
        else:
            claim = row["claim_status"]
        reasons = ", ".join(escape(x) for x in row["reasons"][:2]) or "No reason supplied"
        clusters = (
            "unknown" if row["independent_clusters"] is None else str(row["independent_clusters"])
        )
        lines.append(
            f"• {escape(coin)} {direction}: grade {row['grade']} "
            f"({row['research_r']}R research max), {claim}; "
            f"independent clusters {clusters}; {reasons}"
        )
    lines.append("Not an order, stop, account-level risk approval, or live trading signal.")
    return "\n".join(lines)
