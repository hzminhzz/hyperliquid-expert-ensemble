"""Build read-only C/X research grade documents from the existing as-known journal.

The roster is assembled at the digest cut solely for CURRENT DISPLAY, not
scientific prospective evaluation. It is not a predeclared research cohort.
Never infer a missing source book, wallet independence or follower economics.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from copytrade_pm.trader_intelligence import (
    TraderIntelligence,
    WalletIdentity,
    assessment_document_for_board,
    digest,
)
from copytrade_pm.trader_intelligence_intake import from_opportunity_journal

_ADDRESS = re.compile(r"0x[a-f0-9]{40}\Z")
_MAX_WALLETS = 512
_MAX_ROWS = 5_000


def build_research_grades(
    *,
    journal_db: Path,
    board: dict,
    recent_board: dict,
    now: datetime,
) -> dict:
    """Generate exact-board C/X assessments, never A/B or an order directive."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("TI_GRADE_NAIVE_CUT")
    if board.get("truncated") is not False or recent_board.get("truncated") is not False:
        raise ValueError("TI_GRADE_TRUNCATED_BOARD")
    cut = now.astimezone(UTC)
    cut_text = cut.isoformat()
    window_start = (cut - timedelta(hours=4)).isoformat()
    if journal_db.is_symlink() or not journal_db.is_file():
        raise ValueError("TI_GRADE_UNTRUSTED_JOURNAL")
    with closing(sqlite3.connect(f"file:{journal_db}?mode=ro", uri=True, timeout=10)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        values = db.execute(
            "SELECT DISTINCT lower(wallet_id) FROM opportunity_decisions "
            "WHERE first_known_at>? AND first_known_at<=? "
            "ORDER BY lower(wallet_id) LIMIT ?",
            (window_start, cut_text, _MAX_WALLETS + 1),
        ).fetchall()
    if len(values) > _MAX_WALLETS:
        raise ValueError("TI_GRADE_ROSTER_TOO_LARGE")
    addresses = tuple(row[0] for row in values)
    if any(not isinstance(x, str) or not _ADDRESS.fullmatch(x) for x in addresses):
        raise ValueError("TI_GRADE_UNKNOWN_WALLET_IDENTITY")
    # No historical/independent registration is implied. The current digest
    # cut is the first this *ephemeral* roster is assembled for display.
    runtime_roster_sha = digest({"display_only_wallets": addresses, "cut": cut_text})
    producer = TraderIntelligence()
    assessments = ()
    if addresses:
        wallets = tuple(
            WalletIdentity(alias=address, identity_sha256=sha256(address.encode()).hexdigest())
            for address in addresses
        )
        snapshot = from_opportunity_journal(
            journal_db=journal_db,
            decision_at=cut_text,
            roster_id="ti-telegram-display-" + runtime_roster_sha,
            roster_known_at=cut_text,
            wallets=wallets,
            maximum_source_rows=_MAX_ROWS,
            lookback_hours=4,
        )
        assessments = producer.evaluate(snapshot, cut_text)
    expected = {
        (
            f"hyperliquid/mainnet:perp:default/{row['coin']}",
            "LONG" if row["observed_directional_bias"] == "LONG_WATCH" else "SHORT",
        )
        for row in board["coins"]
        if row["observed_directional_bias"] in {"LONG_WATCH", "SHORT_WATCH"}
    }
    scoped = tuple(
        replace(
            assessment,
            reasons=tuple(
                sorted(set(assessment.reasons) | {"DYNAMIC_ROSTER_NOT_INDEPENDENTLY_REGISTERED"})
            ),
        )
        for assessment in assessments
        if (assessment.instrument, assessment.direction) in expected
    )
    if len(scoped) > 40 or any(a.grade not in {"C", "X"} or a.research_r != "0" for a in scoped):
        raise ValueError("TI_GRADE_ILLEGAL_PROMOTION")
    return assessment_document_for_board(
        scoped,
        decision_at=cut_text,
        four_hour_board=board,
        one_hour_board=recent_board,
    )
