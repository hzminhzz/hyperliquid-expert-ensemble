"""PM-02 immutable research decision artifacts and as-of parent-chain validation.

Content addressing detects accidental change and supports deterministic replay.
A filesystem hash alone is NOT an external proof of first-known receipt time.
"""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

SCHEMA = "pm02_manager_governance_v1"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: object) -> str:
    return sha256(canonical(value)).hexdigest()


def verify_report(report: dict) -> str:
    if not isinstance(report, dict) or report.get("schema") != SCHEMA:
        raise ValueError("UNKNOWN_DECISION_REPORT_SCHEMA")
    actual = digest({k: v for k, v in report.items() if k != "sha256"})
    if report.get("sha256") != actual:
        raise ValueError("DECISION_REPORT_HASH_MISMATCH")
    if len(report.get("cards", [])) != 36:
        raise ValueError("DECISION_REPORT_COHORT_INCOMPLETE")
    if not report.get("live_trading_authorized") is False:
        raise ValueError("DECISION_REPORT_MUST_BE_RESEARCH_ONLY")
    return actual


def read_previous(path: Path) -> dict:
    raw = path.read_bytes()
    if len(raw) > 25_000_000:
        raise ValueError("OVERSIZED_PREVIOUS_REPORT")
    result = json.loads(raw)
    verify_report(result)
    if path.name != "pm02-" + result["sha256"] + ".json":
        raise ValueError("DECISION_FILE_CONTENT_ADDRESS_MISMATCH")
    if canonical(result) != raw:
        raise ValueError("NONCANONICAL_PREVIOUS_DECISION")
    return result


def validate_parent(previous: dict | None, current_time: str, source_hash: str) -> dict:
    """Bind exact parent hash, previous states and prior decision timestamp."""
    if previous is None:
        return {"parent_sha256": None, "sequence": 1, "states": {}}
    verify_report(previous)
    from .pm_manager import timestamp

    if timestamp(previous["asof"]) >= timestamp(current_time):
        raise ValueError("NONMONOTONIC_DECISION_HISTORY")
    if previous["pm01_source_sha256"] != source_hash:
        raise ValueError("PM01_SOURCE_REVISED_WITHOUT_EXPLICIT_NEW_CAMPAIGN")
    if previous.get("untrusted_prospective_input"):
        raise ValueError("SYNTHETIC_HISTORY_CANNOT_JOIN_LIVE_RESEARCH_CHAIN")
    states = {}
    for card in previous["cards"]:
        wallet = card["wallet"]
        if wallet in states:
            raise ValueError("DUPLICATE_PREVIOUS_MANAGER")
        states[wallet] = card["lifecycle"]["state"]
    return {
        "parent_sha256": previous["sha256"],
        "sequence": previous.get("audit_chain", {}).get("sequence", 1) + 1,
        "states": states,
    }


def immutable_report_write(directory: Path, report: dict) -> Path:
    """Create O_EXCL, 0600, fsync. Never overwrite an existing artifact."""
    digest_hex = verify_report(report)
    if not directory.exists():
        directory.mkdir(parents=True, mode=0o700)
    if not directory.is_dir() or directory.stat().st_mode & 0o077:
        raise ValueError("INSECURE_REPORT_DIRECTORY_PERMISSIONS")
    path = directory / f"pm02-{digest_hex}.json"
    payload = canonical(report)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ValueError("DECISION_ARTIFACT_TAMPER_OR_COLLISION")
        if path.stat().st_mode & 0o077:
            raise ValueError("INSECURE_EXISTING_DECISION_ARTIFACT")
        return path
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def source_file_read(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    if len(raw) > 50_000_000:
        raise ValueError("OVERSIZED_PM01_ARTIFACT")
    source_digest = sha256(raw).hexdigest()
    if not path.name.endswith(source_digest[:20] + ".json"):
        raise ValueError("UNPINNED_PM01_ARTIFACT_SHA")
    replay = json.loads(raw)
    if replay.get("schema") != "pm01_fill_only_replay_v2":
        raise ValueError("UNSUPPORTED_PM01_ARTIFACT_SCHEMA")
    if replay.get("never_claim_prospective_alpha") is not True:
        raise ValueError("UNTRUSTED_PM01_PROVENANCE")
    return replay, source_digest
