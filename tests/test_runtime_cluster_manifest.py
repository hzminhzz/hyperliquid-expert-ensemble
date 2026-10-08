"""Deployment regression: private cluster manifest controls live runtime weights."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

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
    finally:
        runtime.close()
