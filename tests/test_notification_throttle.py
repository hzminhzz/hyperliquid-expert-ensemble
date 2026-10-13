"""Notification admission and flood-wait guardrails for advisory-only messages."""

import io
import time
import urllib.error
from datetime import UTC, datetime
from decimal import Decimal

from ensemble.consensus import compute_equal_budget_consensus
from ensemble.posture import compute_posture
from ensemble.runtime import (
    EnsembleRuntime,
    RuntimeSettings,
    _send_telegram_to_chat,
)

COIN = "hyperliquid:mainnet:perp:default:BTC"


def target(exposure: str):
    people = ["A", "B", "C", "D"]
    return compute_equal_budget_consensus(
        COIN,
        [
            compute_posture(person, COIN, Decimal(exposure), Decimal(100), Decimal(100))
            for person in people
        ],
        as_of=datetime.now(UTC),
    )


def test_only_material_a_b_recommendations_may_notify(monkeypatch, tmp_path):
    settings = RuntimeSettings(
        ledger_db=tmp_path / "ledger.db",
        projection_db=tmp_path / "projection.db",
        health_path=tmp_path / "health.json",
        experts=["A", "B", "C", "D"],
        telegram_chat_id_path=tmp_path / "missing",
        notify_initial_targets=False,
        notify_warmup_s=0,
    )
    runtime = EnsembleRuntime(settings)
    sent: list[str] = []
    monkeypatch.setattr(runtime.store, "get_equity", lambda _wallet: (Decimal(100), datetime.now(UTC)))
    monkeypatch.setattr("ensemble.runtime._send_telegram", lambda _settings, message: sent.append(message) or True)
    try:
        runtime.maybe_notify({COIN: target("0.001")})
        assert sent == []
        # A 0.02 descriptive consensus is formally actionable in the raw B1
        # contract, but below Grade C and must never page Telegram.
        runtime.maybe_notify({COIN: target("0.02")})
        assert sent == []
        runtime.maybe_notify({COIN: target("0.4")})
        assert len(sent) == 1
        assert "Grade A" in sent[0]
        runtime.maybe_notify({COIN: target("0.4")})
        assert len(sent) == 1
        # Same market cannot re-alert immediately even on a material increase.
        runtime.maybe_notify({COIN: target("0.6")})
        assert len(sent) == 1
    finally:
        runtime.close()


def test_http_429_honors_retry_after_without_repeated_requests(monkeypatch, tmp_path):
    settings = RuntimeSettings(
        ledger_db=tmp_path / "ledger.db",
        projection_db=tmp_path / "projection.db",
        health_path=tmp_path / "health.json",
        experts=[],
        telegram_bot_token="fake-test-token",
        telegram_chat_id_path=tmp_path / "missing",
    )
    attempts = []

    def fail(_req, timeout=15):
        attempts.append(timeout)
        raise urllib.error.HTTPError(
            "https://example.invalid/api",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b'{"parameters":{"retry_after":155}}'),
        )

    monkeypatch.setattr("ensemble.runtime.urllib.request.urlopen", fail)
    before = time.monotonic()
    assert not _send_telegram_to_chat(settings, "123", "test")
    assert settings.telegram_backoff_until >= before + 155
    assert not _send_telegram_to_chat(settings, "123", "test again")
    assert len(attempts) == 1
