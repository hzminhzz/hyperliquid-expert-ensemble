"""WP4 prospective source-cut, delivery, price, and immutable-label defenses."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ensemble.copy_digest import publish_digest
from ensemble.copy_forward import (
    ForwardStore,
    FrozenEvidence,
    record_hourly,
    verify_forward,
)
from ensemble.copy_forward_outcomes import evaluate_due
from ensemble.copy_preregister import ForwardProtocol, _sha

NOW = datetime(2026, 11, 1, 12, 5, tzinfo=UTC)
PRICE_COIN = "hyperliquid:mainnet:perp:default:BTC"
ROSTER = [f"wallet{i:02d}" for i in range(1, 21)]


def _manifest(root):
    registration = root / "protocol"
    registration.mkdir()
    roster = {
        "schema": "wp3-private-wallet-universe-v1",
        "wallets": ROSTER,
        "source_ledger": "L1",
        "cut_seq": 0,
        "frozen_at": datetime(2026, 10, 30, 12, tzinfo=UTC).isoformat(),
    }
    protocol = ForwardProtocol(
        frozen_at=datetime(2026, 10, 30, 12, tzinfo=UTC),
        holdout_start=datetime(2026, 10, 31, tzinfo=UTC),
        holdout_end=datetime(2026, 11, 30, tzinfo=UTC),
        research_code_sha="a" * 40,
        source_release_ensemble_sha="b" * 40,
        source_release_tracker_sha="c" * 40,
        source_ledger_id="L1",
        ledger_seq_cut=0 if False else 1,
        opportunity_cursor_cut=0,
        universe_sha256=_sha(roster),
        universe_wallet_count=len(ROSTER),
        historical_report_sha256="d" * 64,
    ).sealed()
    manifest = registration / "protocol.json"
    manifest.write_text(json.dumps(protocol))
    (registration / "wallet-universe.PRIVATE.json").write_text(json.dumps(roster))
    return manifest


def _prices(root, *, with_outcomes=True):
    file = root / "prices.db"
    with closing(sqlite3.connect(file)) as db, db:
        db.execute(
            "CREATE TABLE reference_mids("
            "instrument_id TEXT,minute_utc TEXT,known_at TEXT,mid TEXT,"
            "PRIMARY KEY(instrument_id,minute_utc))"
        )
        pairs = [
            (NOW - timedelta(hours=1), "99"),
            (NOW - timedelta(minutes=15), "99.5"),
            (NOW - timedelta(seconds=25), "100"),
        ]
        if with_outcomes:
            pairs.extend([
                (NOW + timedelta(seconds=40), "100"),
                (NOW + timedelta(minutes=15, seconds=70), "101"),
                (NOW + timedelta(hours=1, seconds=70), "102"),
                (NOW + timedelta(hours=4, seconds=70), "104"),
            ])
        for ts, price in pairs:
            db.execute(
                "INSERT INTO reference_mids VALUES (?,?,?,?)",
                (PRICE_COIN,ts.replace(second=0).isoformat(),ts.isoformat(),price),
            )
    return file


def _sources(root, *, wallets=3, gap=False, future=False):
    journal = root / "journal.db"
    ledger = root / "ledger.db"
    with closing(sqlite3.connect(ledger)) as db, db:
        db.execute("CREATE TABLE ledger_meta(ledger_id TEXT,current_seq INTEGER)")
        db.execute("INSERT INTO ledger_meta VALUES ('L1',10)")
        db.execute("CREATE TABLE outbox_events(seq INTEGER,known_at TEXT)")
        db.execute(
            "INSERT INTO outbox_events VALUES (?,?)",
            (10,(NOW - timedelta(seconds=1)).isoformat()),
        )
    with closing(sqlite3.connect(journal)) as db, db:
        db.executescript(
            "CREATE TABLE opportunity_state(ledger_id TEXT,last_seq INTEGER);"
            "INSERT INTO opportunity_state VALUES ('L1',10);"
            "CREATE TABLE opportunity_decisions("
            "opportunity_id TEXT,wallet_id TEXT,instrument_id TEXT,"
            "first_known_at TEXT,policy_revision TEXT,blocker TEXT,payload_json TEXT,"
            "family TEXT,source_action TEXT,copy_direction INTEGER);"
            "CREATE TABLE opportunity_updates("
            "opportunity_id TEXT,known_at TEXT,ledger_seq INTEGER,status TEXT,"
            "source_action TEXT,reason TEXT,source_id TEXT);"
            "CREATE TABLE source_coverage_barriers("
            "ledger_id TEXT,ledger_seq INTEGER,known_at TEXT);"
        )
        for n in range(1, wallets+1):
            ts = NOW - timedelta(minutes=2)
            op = f"op{n}"
            db.execute(
                "INSERT INTO opportunity_decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (op,ROSTER[n-1],PRICE_COIN,ts.isoformat(),"DI2-shadow-v2",None,
                 json.dumps({"source_coverage":"Seeded"}),"ENTER","OPEN_LONG",1),
            )
            db.execute(
                "INSERT INTO opportunity_updates VALUES (?,?,?,?,?,?,?)",
                (op,ts.isoformat(),n,"ACTIVE","OPEN_LONG",None,f"evt{n}"),
            )
        if gap:
            db.execute(
                "INSERT INTO source_coverage_barriers VALUES (?,?,?)",
                ("L1",8,(NOW - timedelta(minutes=1)).isoformat()),
            )
        if future:
            db.execute(
                "INSERT INTO opportunity_decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("future",ROSTER[4],PRICE_COIN,
                 (NOW+timedelta(minutes=1)).isoformat(),
                 "DI2-shadow-v2",None,
                 json.dumps({"source_coverage":"Seeded"}),"ENTER","OPEN_SHORT",-1),
            )
            db.execute(
                "INSERT INTO opportunity_updates VALUES (?,?,?,?,?,?,?)",
                ("future",(NOW+timedelta(minutes=1)).isoformat(),10,
                 "ACTIVE","OPEN_SHORT",None,"future-event"),
            )
    return ledger,journal


def _fixture(tmp_path,*,wallets=3,gap=False,future=False):
    manifest=_manifest(tmp_path)
    ledger,journal=_sources(tmp_path,wallets=wallets,gap=gap,future=future)
    prices=_prices(tmp_path)
    forward=tmp_path/"f.db"
    return FrozenEvidence(protocol_path=manifest),ledger,journal,prices,forward


def _record(inputs,*,at=NOW):
    frozen,ledger,journal,prices,forward=inputs
    return record_hourly(
        frozen=frozen,forward_db=forward,journal_db=journal,
        source_db=ledger,price_db=prices,decided_at=at,
        physical_clock=lambda:at,
    )


def _arm(forward):
    with ForwardStore(forward) as store:
        row=store.db.execute(
            "SELECT payload_json FROM forward_arms"
        ).fetchone()
        return json.loads(row[0])


def _deliver(forward,cut_id,*,ack=NOW+timedelta(seconds=3),
             ambiguous=False):
    with ForwardStore(forward) as store:
        if ambiguous:
            store.append_delivery(
                cut_id=cut_id,attempted=NOW+timedelta(seconds=1),
                status="UNCONFIRMED",
            )
        store.append_delivery(
            cut_id=cut_id,attempted=NOW+timedelta(seconds=2),
            status="ACKNOWLEDGED",acknowledged=ack,
        )


def test_freeze_source_cut_once_and_never_include_future_updates(tmp_path):
    inputs=_fixture(tmp_path,future=True)
    first=_record(inputs)
    assert first["status"]=="RECORDED_RESEARCH_ONLY"
    assert first["candidate_count"]==1
    arm=_arm(inputs[-1])
    assert arm["direction"]==1
    assert arm["support_long_wallets"]==3
    assert arm["support_short_wallets"]==0
    assert arm["frozen_baseline_directions"]["lagged_1h_trend"]==1
    earlier_cut=arm["baseline_frozen_at"]
    second=_record(inputs,at=NOW+timedelta(minutes=1))
    assert second["status"]=="ALREADY_RECORDED"
    assert second["cut_id"]==first["cut_id"]
    assert _arm(inputs[-1])["baseline_frozen_at"]==earlier_cut
    assert verify_forward(frozen=inputs[0],forward_db=inputs[-1])["cuts"]==1


def test_source_gap_and_inadequate_support_are_abstentions(tmp_path):
    for name,kwargs,expected in (
        ("gap",{"gap":True},"SOURCE_GAP_IN_WINDOW"),
        ("low",{"wallets":2},"MIXED_OR_INSUFFICIENT_SUPPORT"),
    ):
        folder=tmp_path/name
        folder.mkdir()
        inputs=_fixture(folder,**kwargs)
        result=_record(inputs)
        assert result["candidate_count"]==0
        assert _arm(inputs[-1])["reason"]==expected
        assert _arm(inputs[-1])["direction"]==0


def test_gap_also_vetoes_strong_post_gap_wallet_votes(tmp_path):
    inputs=_fixture(tmp_path)
    with closing(sqlite3.connect(inputs[2])) as db, db:
        db.execute(
            "INSERT INTO source_coverage_barriers VALUES (?,?,?)",
            ("L1",8,(NOW-timedelta(minutes=3)).isoformat()),
        )
    result=_record(inputs)
    arm=_arm(inputs[-1])
    assert arm["support_long_wallets"]==3
    assert arm["reason"]=="SOURCE_GAP_IN_WINDOW"
    assert arm["direction"]==0
    assert result["candidate_count"]==0


def test_no_historical_backfill_outside_window_or_protocol(tmp_path):
    inputs=_fixture(tmp_path)
    assert _record(inputs,at=NOW.replace(minute=11))["status"]=="MISSED_HOURLY_DECISION_WINDOW"
    assert _record(inputs,at=datetime(2026,10,30,12,5,tzinfo=UTC))["status"]=="OUTSIDE_FROZEN_HOLDOUT"
    assert not inputs[-1].exists()
    with pytest.raises(RuntimeError,match="cannot backfill"):
        record_hourly(
            frozen=inputs[0],forward_db=inputs[-1],journal_db=inputs[2],
            source_db=inputs[1],price_db=inputs[3],decided_at=NOW,
            physical_clock=lambda:NOW+timedelta(hours=1),
        )
    with pytest.raises(ValueError,match="backfill"):
        record_hourly(
            frozen=inputs[0],forward_db=inputs[-1],journal_db=inputs[2],
            source_db=inputs[1],price_db=inputs[3],decided_at=NOW,
            period="2026-10-31T12:00Z",physical_clock=lambda:NOW,
        )


def test_delivered_sample_uses_actual_ack_and_one_hour_after_delayed_entry(tmp_path):
    inputs=_fixture(tmp_path)
    first=_record(inputs)
    _deliver(inputs[-1],first["cut_id"])
    eval_now=NOW+timedelta(hours=1,minutes=4)
    report=evaluate_due(
        frozen=inputs[0],forward_db=inputs[-1],journal_db=inputs[2],
        price_db=inputs[3],now=eval_now,
    )
    assert report["valid_hypothetical"]>=2  # 15m and 1h
    with ForwardStore(inputs[-1]) as store:
        row=store.db.execute(
            "SELECT payload_json FROM forward_evaluations "
            "WHERE evaluation_revision='wp4-mid-diagnostic-v1:1h'"
        ).fetchone()
        obs=json.loads(row[0])
        assert obs["status"]=="VALID_HYPOTHETICAL"
        assert obs["entry_ready_at"]==(NOW+timedelta(seconds=8)).isoformat()
        assert obs["entry_quote_known_at"]==(NOW+timedelta(seconds=40)).isoformat()
        assert obs["exit_target"]==(NOW+timedelta(hours=1,seconds=40)).isoformat()
        assert obs["exit_quote_known_at"]==(NOW+timedelta(hours=1,seconds=70)).isoformat()
        assert obs["net_return"]==str(Decimal("0.019"))
        assert Decimal(obs["market_baseline_nets"]["cash"])==0
        assert obs["market_baseline_nets"]["lagged_1h_trend"]==str(Decimal("0.019"))
        assert obs["qualified"] is False
    repeat=evaluate_due(
        frozen=inputs[0],forward_db=inputs[-1],journal_db=inputs[2],
        price_db=inputs[3],now=eval_now,
    )
    assert repeat["written"]==0
    with ForwardStore(inputs[-1]) as store:
        arm=_arm(inputs[-1])
        with pytest.raises(ValueError,match="supersedes"):
            store.record_evaluation(
                arm_id=arm["arm_id"],revision="manual-price-v2:1h",
                result={"status":"CENSORED"},created_at=eval_now,
            )


def test_missing_or_ambiguous_telegram_ack_is_never_filled(tmp_path):
    for name,ambiguous in (("missing",False),("ambiguous",True)):
        root=tmp_path/name
        root.mkdir()
        inputs=_fixture(root)
        first=_record(inputs)
        if ambiguous:
            _deliver(inputs[-1],first["cut_id"],ambiguous=True)
        report=evaluate_due(
            frozen=inputs[0],forward_db=inputs[-1],journal_db=inputs[2],
            price_db=inputs[3],now=NOW+timedelta(hours=2),
        )
        assert report["valid_hypothetical"]==0
        assert report["censored"]>=2
        with ForwardStore(inputs[-1]) as store:
            statuses=[
                json.loads(row[0])["reason"] for row in store.db.execute(
                    "SELECT payload_json FROM forward_evaluations"
                )
            ]
            assert all("TELEGRAM" in r for r in statuses)


def test_no_future_market_prices_and_frozen_controls_stay_frozen(tmp_path):
    inputs=_fixture(tmp_path)
    original=_record(inputs)
    before=_arm(inputs[-1])["frozen_baseline_directions"]
    with closing(sqlite3.connect(inputs[3])) as db, db:
        db.execute(
            "INSERT INTO reference_mids VALUES (?,?,?,?)",
            (PRICE_COIN,(NOW+timedelta(minutes=4)).isoformat(),
             (NOW+timedelta(minutes=4)).isoformat(),"999"),
        )
    again=_record(inputs,at=NOW+timedelta(minutes=1))
    assert again["cut_id"]==original["cut_id"]
    assert _arm(inputs[-1])["frozen_baseline_directions"]==before


def test_digest_sends_exact_forward_hypotheses_and_seals_http_ack_once(tmp_path):
    _frozen,ledger,journal,prices,forward=_fixture(tmp_path)
    sent=[]
    status=publish_digest(
        journal_db=journal,state_db=tmp_path/"delivery.db",
        source_db=ledger,now=NOW,token="dummy",chat_id="dummy",
        send=lambda _token,_chat,message:sent.append(message) or True,
        capture_forward=True,
        forward_db=forward,protocol_path=tmp_path/"protocol/protocol.json",
        price_db=prices,receipt_clock=lambda:NOW+timedelta(seconds=3),
    )
    assert status["status"]=="DELIVERED_DESCRIPTIVE_ONLY"
    assert status["forward_capture"]=="RECORDED_RESEARCH_ONLY"
    assert status["forward_ack_recorded"] is True
    assert len(sent)==1
    assert "Frozen 1h research hypotheses" in sent[0]
    assert "BTC long" in sent[0]
    assert "NOT trading signals" in sent[0]
    with ForwardStore(forward) as store:
        delivery=store.db.execute(
            "SELECT status,attempt_at,acknowledged_at FROM forward_delivery"
        ).fetchone()
        assert tuple(delivery)==(
            "ACKNOWLEDGED",
            (NOW+timedelta(seconds=3)).isoformat(),
            (NOW+timedelta(seconds=3)).isoformat(),
        )
        assert store.db.execute("SELECT COUNT(*) FROM forward_cuts").fetchone()[0]==1
    again=publish_digest(
        journal_db=journal,state_db=tmp_path/"delivery.db",
        source_db=ledger,now=NOW,token="dummy",chat_id="dummy",
        send=lambda *_:False,
        capture_forward=True,
        forward_db=forward,protocol_path=tmp_path/"protocol/protocol.json",
        price_db=prices,receipt_clock=lambda:NOW+timedelta(seconds=4),
    )
    assert again["status"]=="ALREADY_DELIVERED"
    assert len(sent)==1


def test_sqlite_storage_enforces_append_only_receipts_and_forecasts(tmp_path):
    inputs=_fixture(tmp_path)
    cut=_record(inputs)
    with ForwardStore(inputs[-1]) as store:
        with pytest.raises(sqlite3.IntegrityError,match="immutable"):
            store.db.execute(
                "UPDATE forward_cuts SET shadow_seq=999 WHERE cut_id=?",
                (cut["cut_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError,match="immutable"):
            store.db.execute(
                "DELETE FROM forward_arms WHERE cut_id=?",
                (cut["cut_id"],),
            )
        with pytest.raises(ValueError,match="frozen decision"):
            store.append_delivery(
                cut_id="fake-cut",attempted=NOW+timedelta(seconds=1),
                status="ACKNOWLEDGED",
                acknowledged=NOW+timedelta(seconds=2),
            )
        assert store.db.execute(
            "SELECT COUNT(*) FROM forward_cuts"
        ).fetchone()[0]==1


def test_protocol_or_roster_tampering_refuses_source_capture(tmp_path):
    _fixture(tmp_path)
    protocol=tmp_path/"protocol/protocol.json"
    data=json.loads(protocol.read_text())
    data["protocol"]["policy"]["hourly_support_min_distinct_wallets"]=1
    protocol.write_text(json.dumps(data))
    with pytest.raises(ValueError,match="hash conflict"):
        FrozenEvidence(protocol_path=protocol)
