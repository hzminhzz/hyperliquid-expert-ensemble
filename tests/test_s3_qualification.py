"""S3 Qualification Suite: Q09, Q14, and Q16 Operator Task Battery."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ensemble.checkpoint import JobStore
from ensemble.cli import cmd_inspect_capabilities, cmd_inspect_system
from ensemble.projection import ProjectionStore
from ensemble.replay import ReplayRunner


def test_q09_coherent_worldview_and_blockers(tmp_path: Path):
    store = ProjectionStore(tmp_path / "proj.db")
    try:
        events = [
            {
                "schema_version": "1",
                "event_id": "ledger-1:1",
                "cursor": {"ledger_id": "ledger-1", "seq": 1},
                "kind": "POSITION_CHANGE",
                "instrument_id": "hyperliquid:mainnet:perp:default:BTC",
                "wallet_id": "0xexpert1",
                "event_time": "2026-10-06T10:00:00Z",
                "received_at": "2026-10-06T10:00:01Z",
                "known_at": "2026-10-06T10:00:01Z",
                "before_revision": 0,
                "after_revision": 1,
                "payload": {"transition": "OPEN", "quantity_after": "2.5", "px": "60000"},
            }
        ]
        applied = store.apply_canonical_events("ledger-1", events)
        assert applied == 1

        # Healthy cut
        wv = store.build_worldview("ledger-1")
        assert wv.status == "HEALTHY"
        assert wv.positions_count == 1
        assert wv.blocker_code is None

        # Quarantined coverage marks worldview as BLOCKED
        with store.conn:
            store.conn.execute("UPDATE projected_positions SET coverage = 'Quarantined'")
        wv_blocked = store.build_worldview("ledger-1")
        assert wv_blocked.status == "BLOCKED"
        assert wv_blocked.blocker_code == "STATE_QUARANTINED"
    finally:
        store.close()


def test_q14_replay_equivalence_and_isolation(tmp_path: Path):
    runner = ReplayRunner(tmp_path / "replays")
    base_time = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)

    events = [
        {
            "schema_version": "1",
            "event_id": "ledger-1:1",
            "cursor": {"ledger_id": "ledger-1", "seq": 1},
            "kind": "POSITION_CHANGE",
            "instrument_id": "hyperliquid:mainnet:perp:default:ETH",
            "wallet_id": "0xwallet_a",
            "event_time": (base_time - timedelta(minutes=10)).isoformat(),
            "known_at": (base_time - timedelta(minutes=10)).isoformat(),
            "payload": {"transition": "OPEN", "quantity_after": "5.0", "px": "3000"},
        },
        {
            "schema_version": "1",
            "event_id": "ledger-1:2",
            "cursor": {"ledger_id": "ledger-1", "seq": 2},
            "kind": "POSITION_CHANGE",
            "instrument_id": "hyperliquid:mainnet:perp:default:ETH",
            "wallet_id": "0xwallet_a",
            "event_time": (base_time - timedelta(minutes=5)).isoformat(),
            "known_at": (base_time - timedelta(minutes=5)).isoformat(),
            "payload": {"transition": "ADD", "quantity_after": "10.0", "px": "3100"},
        },
        # Late correction event known AFTER base_time
        {
            "schema_version": "1",
            "event_id": "ledger-1:3",
            "cursor": {"ledger_id": "ledger-1", "seq": 3},
            "kind": "POSITION_CHANGE",
            "instrument_id": "hyperliquid:mainnet:perp:default:ETH",
            "wallet_id": "0xwallet_a",
            "event_time": (base_time - timedelta(minutes=1)).isoformat(),
            "known_at": (base_time + timedelta(hours=2)).isoformat(),  # Arrived later
            "payload": {"transition": "REDUCE", "quantity_after": "8.0", "px": "3200"},
        },
    ]

    # As-known replay pins knowledge to base_time
    manifest_ak1 = runner.create_manifest(
        "ledger-1", events, clock_mode="as_known", as_of=base_time
    )
    manifest_ak2 = runner.create_manifest(
        "ledger-1", events, clock_mode="as_known", as_of=base_time
    )

    res_1 = runner.run_replay(manifest_ak1)
    res_2 = runner.run_replay(manifest_ak2)

    # Replay Equivalence: identical decisions and hashes
    assert res_1.decision_hash == res_2.decision_hash
    assert res_1.events_applied == 2  # The 3rd event was excluded because known_at > base_time
    assert res_1.positions[0].szi == Decimal("10.0")

    # Restated replay includes the late correction
    manifest_restated = runner.create_manifest(
        "ledger-1", events, clock_mode="restated", as_of=base_time
    )
    res_restated = runner.run_replay(manifest_restated)
    assert res_restated.events_applied == 3
    assert res_restated.positions[0].szi == Decimal("8.0")
    assert res_restated.decision_hash != res_1.decision_hash


def test_q16_task1_diagnose_paused_alerts_vs_healthy_ingestion(tmp_path: Path):
    """Task 1: Identify that a paused alert system has healthy recording, rather than restarting ingestion."""
    store = ProjectionStore(tmp_path / "proj.db")
    try:
        brief = cmd_inspect_system(store, alerts_paused=True)
        # Operator checks
        assert brief["ingestion"]["status"] == "RECORDING_HEALTHY"
        assert brief["alerts"]["status"] == "PAUSED"
        assert brief["alerts"]["delivery_paused"] is True
        # Brief confirms recording is intact without restarting ingestion
        assert "does not interrupt observation" in brief["alerts"]["note"]
    finally:
        store.close()


def test_q16_task2_explain_stale_equity_blocker(tmp_path: Path):
    """Task 2: Explain a target change caused by stale equity or a correction, not by a new expert entry."""
    store = ProjectionStore(tmp_path / "proj.db")
    try:
        brief = cmd_inspect_system(store, stale_equity_minutes=25.0)
        assert brief["worldview"]["status"] == "BLOCKED"
        assert len(brief["blockers"]) == 1
        blocker = brief["blockers"][0]
        assert blocker["code"] == "EQUITY_STALE"
        assert "refresh" in blocker["remediation"]
    finally:
        store.close()


def test_q16_task3_resume_interrupted_evaluation_within_budget(tmp_path: Path):
    """Task 3: Resume an interrupted evaluation from its checkpoint within the remaining budget."""
    job_store = JobStore(tmp_path / "jobs.db")
    try:
        # Create job: 10 steps total, cost 15 per step. Initial budget: 70 units (can only do 4 steps)
        job = job_store.create_job("eval-alpha", total_steps=10, budget_units=70)
        assert job.status == "CHECKPOINTED"
        assert job.current_step == 0

        # Run 1: budget runs out after 4 steps (60 units consumed, 10 remaining < 15 needed)
        paused_job = job_store.resume_and_execute("eval-alpha", cost_per_step=15)
        assert paused_job.status == "CHECKPOINTED"
        assert paused_job.current_step == 4
        assert paused_job.consumed_units == 60
        assert paused_job.remaining_budget == 10

        # Run 2: operator adds 90 additional budget units -> remaining budget becomes 100
        # Needs 6 more steps * 15 = 90 units
        resumed_job = job_store.resume_and_execute(
            "eval-alpha", cost_per_step=15, additional_budget=90
        )
        assert resumed_job.status == "COMPLETED"
        assert resumed_job.current_step == 10
        assert resumed_job.consumed_units == 150
    finally:
        job_store.close()


def test_capabilities_omit_unimplemented_commands():
    caps = cmd_inspect_capabilities()
    cmd_names = [c["name"] for c in caps["commands"]]
    # Implemented commands must be present
    assert "inspect capabilities" in cmd_names
    assert "inspect system" in cmd_names
    assert "inspect changes" in cmd_names
    assert "evidence lookup" in cmd_names
    assert "evaluate" in cmd_names
    # Unimplemented future commands (like real copytrade / order placement) must be absent
    assert "execute order" not in cmd_names
    assert "copytrade live" not in cmd_names
