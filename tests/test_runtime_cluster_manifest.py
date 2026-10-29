"""Deployment regression: private cluster manifest controls live runtime weights."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from ensemble.posture import EligibilityState
from ensemble.runtime import EnsembleRuntime, RuntimeSettings

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
INSTRUMENT = "hyperliquid:mainnet:perp:default:BTC"


def snapshot(seq: int, wallet: str, qty: str) -> dict:
    return {
        "cursor": {"ledger_id": "ledger", "seq": seq},
        "kind": "ACCOUNT_SNAPSHOT",
        "instrument_id": f"hyperliquid:mainnet:account:{wallet}",
        "wallet_id": wallet,
        "event_time": T0.isoformat(),
        "received_at": T0.isoformat(),
        "known_at": T0.isoformat(),
        "after_revision": 1,
        "payload": {
            "coverage": "Seeded",
            "partial_history": True,
            "equity": {
                "account_value": "100",
                "observed_at": T0.isoformat(),
            },
            "positions": [
                {
                    "instrument_id": INSTRUMENT,
                    "coin": "BTC",
                    "quantity_after": qty,
                    "avg_entry": "100",
                    "revision": 1,
                    "generation": 1,
                    "coverage": "Seeded",
                    "opened_at_known": False,
                }
            ],
        },
    }



def test_feed_gap_invalidates_previously_known_flat_wallet(tmp_path: Path) -> None:
    wallet = "0x" + "ab" * 20
    active = "0x" + "cd" * 20
    runtime = EnsembleRuntime(RuntimeSettings(
        ledger_db=tmp_path/"ledger.db",
        projection_db=tmp_path/"projection.db",
        health_path=tmp_path/"health.json",
        experts=[wallet,active],
    ))
    try:
        flat=snapshot(1,wallet,"0")
        long=snapshot(2,active,"1")
        current=datetime.now(UTC).isoformat()
        flat["payload"]["equity"]["observed_at"]=current
        long["payload"]["equity"]["observed_at"]=current
        runtime.store.apply_canonical_events("ledger", [flat,long])
        runtime.mids={"BTC":Decimal(100)}
        before=runtime.compute_targets()[INSTRUMENT]
        assert before.contributions[0].state==EligibilityState.KNOWN_FLAT
        runtime.store.apply_canonical_events("ledger", [{
            "cursor":{"ledger_id":"ledger","seq":3},
            "kind":"COVERAGE_CHANGE","wallet_id":"","instrument_id":"",
            "known_at":datetime.now(UTC).isoformat(),
            "payload":{"status":"DISCONNECTED_GAP_UNVERIFIED"},
        }])
        after=runtime.compute_targets()[INSTRUMENT]
        assert after.contributions[0].state==EligibilityState.UNAVAILABLE
        assert not after.is_actionable
    finally:
        runtime.close()


def test_runtime_uses_cluster_budget_not_wallet_count(tmp_path: Path) -> None:
    experts = ["0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20]
    manifest = tmp_path / "clusters.json"
    manifest.write_text(
        json.dumps({"clusters": [[experts[0], experts[1]], [experts[2]]]}),
        encoding="utf-8",
    )

    runtime = EnsembleRuntime(
        RuntimeSettings(
            ledger_db=tmp_path / "ledger.db",
            projection_db=tmp_path / "projection.db",
            health_path=tmp_path / "health.json",
            experts=experts,
            cluster_manifest_path=manifest,
        )
    )
    try:
        runtime.store.apply_canonical_events(
            "ledger",
            [
                snapshot(1, experts[0], "1"),
                snapshot(2, experts[1], "1"),
                snapshot(3, experts[2], "-1"),
            ],
        )
        runtime.mids = {"BTC": Decimal(100)}
        target = runtime.compute_targets()[INSTRUMENT]
        assert target.observed_target == Decimal(0)
        assert len(target.contributions) == 3
        assert target.contributions[0].weight == target.contributions[1].weight

        runtime.store.apply_canonical_events("ledger", [{
            "cursor":{"ledger_id":"ledger","seq":4},
            "kind":"COVERAGE_CHANGE","wallet_id":"","instrument_id":"",
            "known_at":T0.isoformat(),
            "payload":{"status":"DISCONNECTED_GAP_UNVERIFIED"},
        }])
        degraded=runtime.compute_targets()[INSTRUMENT]
        assert not degraded.is_actionable
        assert degraded.blocker_code=="INSUFFICIENT_COVERAGE"
        runtime.store.apply_canonical_events("ledger", [snapshot(5,experts[0],"1")])
        partially_recovered=runtime.compute_targets()[INSTRUMENT]
        assert not partially_recovered.is_actionable
    finally:
        runtime.close()
