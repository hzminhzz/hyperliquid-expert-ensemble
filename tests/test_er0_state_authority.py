from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ensemble.projection import ProjectionStore


def test_account_snapshot_is_rust_owned_and_preserves_instrument_scope(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path / "projection.db")
    try:
        event = {
            "cursor": {"ledger_id": "ledger-er0", "seq": 1},
            "kind": "ACCOUNT_SNAPSHOT",
            "instrument_id": "hyperliquid:mainnet:account:0xabc",
            "wallet_id": "0xabc",
            "event_time": "2026-10-06T12:00:00+00:00",
            "received_at": "2026-10-06T12:00:01+00:00",
            "known_at": "2026-10-06T12:00:01+00:00",
            "after_revision": 7,
            "payload": {
                "coverage": "Seeded",
                "partial_history": True,
                "equity": {
                    "account_value": "10000",
                    "observed_at": "2026-10-06T12:00:00+00:00",
                },
                "positions": [
                    {
                        "instrument_id": "hyperliquid:mainnet:perp:default:BTC",
                        "coin": "BTC",
                        "quantity_after": "1.0",
                        "avg_entry": "60000",
                        "revision": 7,
                        "generation": 2,
                        "coverage": "Seeded",
                        "opened_at_known": False,
                    },
                    {
                        "instrument_id": "hyperliquid:mainnet:perp:hip3:BTC",
                        "coin": "BTC",
                        "quantity_after": "-2.0",
                        "avg_entry": "61000",
                        "revision": 7,
                        "generation": 2,
                        "coverage": "Seeded",
                        "opened_at_known": False,
                    },
                ],
            },
        }
        assert store.apply_canonical_events("ledger-er0", [event]) == 1

        positions = store.list_positions()
        assert {p.instrument_id for p in positions} == {
            "hyperliquid:mainnet:perp:default:BTC",
            "hyperliquid:mainnet:perp:hip3:BTC",
        }
        assert all(p.opened_at is None for p in positions)
        assert store.get_equity("0xabc") == (
            Decimal(10000),
            datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
        )
        with pytest.raises(ValueError, match="ambiguous coin"):
            store.get_position("0xabc", "BTC")

        add = {
            "cursor": {"ledger_id": "ledger-er0", "seq": 2},
            "kind": "POSITION_CHANGE",
            "instrument_id": "hyperliquid:mainnet:perp:default:BTC",
            "wallet_id": "0xabc",
            "event_time": "2026-10-06T12:01:00+00:00",
            "received_at": "2026-10-06T12:01:01+00:00",
            "known_at": "2026-10-06T12:01:01+00:00",
            "after_revision": 8,
            "payload": {
                "transition": "ADD",
                "quantity_after": "1.5",
                "avg_entry": "60200",
                "generation": 2,
                "coverage": "Seeded",
            },
        }
        store.apply_canonical_events("ledger-er0", [add])
        default = store.get_position("0xabc", "hyperliquid:mainnet:perp:default:BTC")
        hip3 = store.get_position("0xabc", "hyperliquid:mainnet:perp:hip3:BTC")
        assert default is not None and default.szi == Decimal("1.5")
        assert default.opened_at is None
        assert hip3 is not None and hip3.szi == Decimal("-2.0")
    finally:
        store.close()


def test_direct_python_snapshot_replacement_is_disabled(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path / "projection.db")
    try:
        with pytest.raises(RuntimeError, match="Rust ACCOUNT_SNAPSHOT"):
            store.replace_wallet_snapshot(
                "0xabc", [], Decimal(1), datetime.now(UTC)
            )
    finally:
        store.close()
