"""Live runtime must freeze genuine as-known cut inputs once per minute."""

from datetime import UTC, datetime
from decimal import Decimal

from ensemble.consensus import compute_equal_budget_consensus
from ensemble.posture import compute_posture
from ensemble.runtime import EnsembleRuntime, RuntimeSettings

COIN = "hyperliquid:mainnet:perp:default:BTC"


def test_runtime_capture_records_exact_processed_ledger_cut(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    settings = RuntimeSettings(
        ledger_db=tmp_path / "ledger.db",
        projection_db=tmp_path / "projection.db",
        health_path=tmp_path / "health.json",
        research_db=tmp_path / "research.db",
        telegram_chat_id_path=tmp_path / "none",
        experts=["0x" + "11" * 20],
    )
    runtime = EnsembleRuntime(settings)
    try:
        monkeypatch.setattr(
            "ensemble.runtime._ledger_status",
            lambda _path: {"exists": True, "ledger_id": "test-ledger", "current_seq": 42},
        )
        runtime.mids = {"BTC": Decimal(100)}
        runtime.market_known_at = now
        posture = compute_posture(
            "0x" + "11" * 20,
            COIN,
            Decimal(1),
            Decimal(100),
            Decimal(1000),
        )
        target = compute_equal_budget_consensus(COIN, [posture], as_of=now)
        runtime.capture_research({COIN: target})
        assert runtime.recorder is not None
        row = runtime.recorder.conn.execute(
            """
            SELECT instrument_id,ledger_id,ledger_seq,price_mid,price_known_at,
                   model_id
            FROM signal_cuts
            """
        ).fetchone()
        assert row[0] == COIN
        assert row[1] == "test-ledger"
        assert row[2] == 0  # last *consumed* sequence, not current ledger head
        assert row[3] == "100"
        assert row[4] is not None
        assert "not_er4" in row[5]

        runtime.capture_research({COIN: target})
        assert runtime.recorder.stats()["cut_rows"] == 1

        runtime.write_health({COIN: target})
        import json
        health = json.loads(settings.health_path.read_text())
        assert health["research"]["cut_rows"] == 1
        assert health["research"]["status"] == "DESCRIPTIVE_ONLY_NOT_ER4_QUALIFIED"
    finally:
        runtime.close()


def test_hip3_without_qualified_quote_mapping_never_uses_entry_price(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    settings = RuntimeSettings(
        ledger_db=tmp_path / "ledger.db",
        projection_db=tmp_path / "projection.db",
        health_path=tmp_path / "health.json",
        research_db=tmp_path / "research.db",
        telegram_chat_id_path=tmp_path / "none",
        experts=["0x" + "11" * 20],
    )
    runtime = EnsembleRuntime(settings)
    try:
        monkeypatch.setattr(
            "ensemble.runtime._ledger_status",
            lambda _path: {"exists": True, "ledger_id": "test", "current_seq": 1},
        )
        instrument = "hyperliquid:mainnet:perp:hip3:BTC"
        posture = compute_posture(
            settings.experts[0], instrument, Decimal(1), Decimal(100), Decimal(1000)
        )
        target = compute_equal_budget_consensus(instrument, [posture], as_of=now)
        runtime.market_known_at = now
        runtime.mids = {"BTC": Decimal(100)}
        runtime.capture_research({instrument: target})
        assert runtime.recorder is not None
        row = runtime.recorder.conn.execute(
            "SELECT instrument_id,price_mid FROM signal_cuts"
        ).fetchone()
        assert row == (instrument, None)
    finally:
        runtime.close()
