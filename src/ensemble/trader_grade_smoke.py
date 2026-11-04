"""Real-journal dry run of the C/X Telegram overlay, without Telegram/network sends."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .copy_board import build_trade_flow_board
from .copy_digest import DATA, publish_digest
from .trader_grade_producer import build_research_grades


def run(
    *,
    journal_db: Path = DATA / "copy-opportunities-live.db",
    source_db: Path = DATA / "observation.db",
    model_sha256: str,
    policy_sha256: str,
) -> dict:
    now = datetime.now(UTC)
    board = build_trade_flow_board(journal_db=journal_db, now=now, hours=4, max_coins=20)
    recent = build_trade_flow_board(journal_db=journal_db, now=now, hours=1, max_coins=20)
    document = build_research_grades(
        journal_db=journal_db,
        board=board,
        recent_board=recent,
        now=now,
    )
    if any(
        row["grade"] not in ("C", "X")
        or row["research_r"] != "0"
        or row["order_authorized"] is not False
        for row in document["assessments"]
    ):
        raise ValueError("TI_SMOKE_UNSAFE_GRADE")
    with tempfile.TemporaryDirectory(prefix="ti-digest-smoke-") as directory:
        sent: list[str] = []
        result = publish_digest(
            journal_db=journal_db,
            source_db=source_db,
            state_db=Path(directory) / "delivery.db",
            now=now,
            token="DRYRUN_NO_NETWORK",
            chat_id="DRYRUN_NO_NETWORK",
            send=lambda _t, _c, message: sent.append(message) or True,
            capture_forward=False,
            grade_research_enabled=True,
            grade_model_sha256=model_sha256,
            grade_policy_sha256=policy_sha256,
        )
    if (
        result["status"] != "DELIVERED_DESCRIPTIVE_ONLY"
        or result["grade_status"] not in {"RESEARCH_CX_OVERLAY", "RESEARCH_CX_NO_CANDIDATE"}
        or len(sent) != 1
        or "RESEARCH" not in sent[0]
        or "NOT a copy/entry signal" not in sent[0]
        or "grade A" in sent[0]
        or "grade B" in sent[0]
    ):
        raise ValueError("TI_RESEARCH_DIGEST_SMOKE_FAILED:" + str(result.get("status")))
    return {
        "status": "PASS_NO_TELEGRAM_NETWORK",
        "grade_status": result["grade_status"],
        "candidate_count": len(document["assessments"]),
        "candidate_grades": sorted({row["grade"] for row in document["assessments"]}),
        "board_coins": board["coin_count"],
        "source_fingerprint_match": True,
        "message_chars": len(sent[0]),
        "research_only": True,
        "financial_orders": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--policy-sha256", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            run(model_sha256=args.model_sha256, policy_sha256=args.policy_sha256), sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
