"""Optional cross-repository grade consumer: no policy activation by default."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from ensemble.copy_board import build_trade_flow_board
from ensemble.copy_digest import publish_digest
from ensemble.trader_grade_adapter import (
    grade_appendix,
    inspect_grade_artifact,
    source_fingerprint,
)

CUT = datetime(2026, 10, 13, 10, tzinfo=UTC)
SHA = sha256(b"grade-fixture").hexdigest()
MARKET = "hyperliquid:mainnet:perp:default:BTC"


def databases(tmp_path: Path):
    source = tmp_path / "source.db"
    journal = tmp_path / "journal.db"
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("CREATE TABLE ledger_meta(current_seq INTEGER)")
        db.execute("INSERT INTO ledger_meta VALUES (3)")
        db.execute("CREATE TABLE outbox_events(seq INTEGER,known_at TEXT)")
        db.execute("INSERT INTO outbox_events VALUES (3,?)", (CUT.isoformat(),))
    with closing(sqlite3.connect(journal)) as db, db:
        db.execute("CREATE TABLE opportunity_state(last_seq INTEGER)")
        db.execute("INSERT INTO opportunity_state VALUES (3)")
        db.execute(
            "CREATE TABLE opportunity_decisions("
            "opportunity_id TEXT,instrument_id TEXT,wallet_id TEXT,"
            "first_known_at TEXT,family TEXT,source_action TEXT,"
            "copy_direction INTEGER,blocker TEXT)"
        )
        for i in range(3):
            db.execute(
                "INSERT INTO opportunity_decisions VALUES (?,?,?,?,?,?,?,?)",
                (f"o{i}", MARKET, f"wallet{i}", CUT.isoformat(), "ENTER", "OPEN_LONG", 1, None),
            )
    return source, journal


def grade_document(journal: Path, now=CUT):
    board = build_trade_flow_board(journal_db=journal, now=now, hours=4, max_coins=20)
    recent = build_trade_flow_board(journal_db=journal, now=now, hours=1, max_coins=20)
    row = {
        "schema": "trader_intelligence_v1",
        "candidate_id": SHA,
        "instrument": "hyperliquid/mainnet:perp:default/BTC",
        "direction": "LONG",
        "decision_at": now.isoformat(),
        "input_sha256": SHA,
        "model_sha256": SHA,
        "policy_sha256": SHA,
        "grade": "C",
        "evidence_status": "PROSPECTIVE_OBSERVED",
        "claim_status": "NOT_QUALIFIED",
        "reasons": ["NO_CALIBRATED_ALPHA", "RESEARCH_ONLY"],
        "research_r": "0",
        "independent_clusters": None,
        "qualified_contributors": None,
        "first_known_at": now.isoformat(),
        "latest_source_event_at": now.isoformat(),
        "follower_net_r": None,
        "follower_lcb_r": None,
        "research_only": True,
        "order_authorized": False,
    }
    document = {
        "schema": "trader_intelligence_v1",
        "decision_at": now.isoformat(),
        "source_sha256": source_fingerprint(board, recent),
        "research_only": True,
        "order_authorized": False,
        "assessments": [row],
    }
    return board, recent, document


def stored(tmp_path, document):
    p = tmp_path / "grades.json"
    p.write_text(json.dumps(document))
    return p


def inspect(path, board, recent, now=CUT):
    return inspect_grade_artifact(
        path,
        board=board,
        recent_board=recent,
        now=now,
        expected_model_sha256=SHA,
        expected_policy_sha256=SHA,
    )


def test_exact_version_model_cut_and_read_only_grade(tmp_path):
    _, journal = databases(tmp_path)
    board, recent, doc = grade_document(journal)
    result = inspect(stored(tmp_path, doc), board, recent)
    assert result is not None
    assert result[("BTC", "LONG")]["grade"] == "C"
    appendix = grade_appendix(result)
    assert "grade C (0R research max)" in appendix
    assert "research only" in appendix.lower()


def test_invalid_grade_documents_downgrade_without_residual_a_b(tmp_path):
    _, journal = databases(tmp_path)
    board, recent, doc = grade_document(journal)
    variants = []
    d = json.loads(json.dumps(doc))
    d["source_sha256"] = "0" * 64
    variants.append(d)
    d = json.loads(json.dumps(doc))
    d["decision_at"] = (CUT - timedelta(minutes=3)).isoformat()
    variants.append(d)
    d = json.loads(json.dumps(doc))
    d["assessments"][0]["model_sha256"] = "0" * 64
    variants.append(d)
    d = json.loads(json.dumps(doc))
    d["assessments"][0]["grade"] = "A"
    d["assessments"][0]["research_r"] = "1"
    variants.append(d)
    d = json.loads(json.dumps(doc))
    d["assessments"][0]["first_known_at"] = (CUT + timedelta(seconds=1)).isoformat()
    variants.append(d)
    d = json.loads(json.dumps(doc))
    d["assessments"].append(d["assessments"][0])
    variants.append(d)
    for index, variant in enumerate(variants):
        path = tmp_path / f"g{index}.json"
        path.write_text(json.dumps(variant))
        assert inspect(path, board, recent) is None
    bad_json = tmp_path / "dup.json"
    bad_json.write_text('{"schema":"x","schema":"y"}')
    assert inspect(bad_json, board, recent) is None
    assert inspect(tmp_path / "missing.json", board, recent) is None


def test_symlink_source_never_read(tmp_path):
    _, journal = databases(tmp_path)
    board, recent, doc = grade_document(journal)
    actual = stored(tmp_path, doc)
    link = tmp_path / "symlink.json"
    link.symlink_to(actual)
    assert inspect(link, board, recent) is None


def test_telegram_shadow_mode_identical_message_to_original(tmp_path):
    source, journal = databases(tmp_path)
    _, _, doc = grade_document(journal)
    artifact = stored(tmp_path, doc)
    seen_shadow, seen_original = [], []
    opts = {
        "journal_db": journal,
        "source_db": source,
        "now": CUT,
        "token": "test",
        "chat_id": "test",
    }
    base = publish_digest(
        **opts,
        state_db=tmp_path / "base.db",
        send=lambda _t, _c, msg: seen_original.append(msg) or True,
    )
    shadow = publish_digest(
        **opts,
        state_db=tmp_path / "shadow.db",
        send=lambda _t, _c, msg: seen_shadow.append(msg) or True,
        grade_artifact=artifact,
        grade_model_sha256=SHA,
        grade_policy_sha256=SHA,
    )
    assert base["status"] == shadow["status"] == "DELIVERED_DESCRIPTIVE_ONLY"
    assert shadow["grade_status"] == "VALID_SHADOW"
    assert seen_shadow == seen_original
    assert "grade C" not in seen_shadow[0]
    assert (
        publish_digest(
            **opts,
            state_db=tmp_path / "shadow.db",
            send=lambda *_: (_ for _ in ()).throw(AssertionError("duplicate send")),
            grade_artifact=artifact,
            grade_model_sha256=SHA,
            grade_policy_sha256=SHA,
        )["status"]
        == "ALREADY_DELIVERED"
    )


def test_telegram_opt_in_test_render_and_fallback(tmp_path):
    source, journal = databases(tmp_path)
    _, _, doc = grade_document(journal)
    artifact = stored(tmp_path, doc)
    sent = []
    opts = {
        "journal_db": journal,
        "source_db": source,
        "now": CUT,
        "token": "test",
        "chat_id": "test",
        "grade_artifact": artifact,
        "grade_model_sha256": SHA,
        "grade_policy_sha256": SHA,
        "grade_display_enabled": True,
    }
    result = publish_digest(
        **opts,
        state_db=tmp_path / "preview.db",
        send=lambda _t, _c, msg: sent.append(msg) or True,
    )
    assert result["grade_status"] == "RESEARCH_OVERLAY"
    assert "grade C (0R research max)" in sent[0]
    assert "rolling 4h" in sent[0]
    assert "Last 1h" in sent[0]
    assert "NOT a copy/entry signal" in sent[0]

    doc["source_sha256"] = "0" * 64
    artifact.write_text(json.dumps(doc))
    fallback = []
    result = publish_digest(
        **opts,
        state_db=tmp_path / "fallback.db",
        send=lambda _t, _c, msg: fallback.append(msg) or True,
    )
    assert result["grade_status"] == "FALLBACK_DESCRIPTIVE"
    assert "grade C" not in fallback[0]
    assert "NOT a copy/entry signal" in fallback[0]
