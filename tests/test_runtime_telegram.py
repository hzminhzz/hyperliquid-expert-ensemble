"""Live-runtime Telegram command adapter regression tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

from ensemble.runtime import (
    RuntimeSettings,
    _poll_telegram_commands,
)


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        ledger_db=tmp_path / "ledger.db",
        projection_db=tmp_path / "projection.db",
        health_path=tmp_path / "health.json",
        experts=[],
        telegram_bot_token="test-token",
        telegram_chat_id_path=tmp_path / "chat-id",
        telegram_update_offset_path=tmp_path / "update-offset",
        telegram_pair_code="abcd1234",
    )


def test_pair_acknowledges_and_persists_chat_and_offset(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    sent: list[tuple[str, str]] = []

    def fake_urlopen(request, timeout=10.0):
        url = request.full_url
        if "getUpdates" in url:
            return _FakeResponse(
                {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 42,
                            "message": {
                                "text": "/pair abcd1234",
                                "chat": {"id": 123456},
                            },
                        }
                    ],
                }
            )
        raise AssertionError(url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "ensemble.runtime._send_telegram_to_chat",
        lambda _settings, chat_id, text: sent.append((chat_id, text)) or True,
    )

    _poll_telegram_commands(settings, status_text="healthy")

    assert settings.telegram_chat_id == "123456"
    assert settings.telegram_chat_id_path.read_text() == "123456"
    assert settings.telegram_update_offset_path.read_text() == "43"
    assert sent and "Paired" in sent[0][1]
    assert "financial execution is disabled" in sent[0][1].lower()


def test_status_and_help_respond_only_to_paired_chat(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.telegram_chat_id = "123456"
    settings.telegram_chat_id_path.write_text("123456")
    sent: list[tuple[str, str]] = []

    def fake_urlopen(request, timeout=10.0):
        if "getUpdates" in request.full_url:
            return _FakeResponse(
                {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 7,
                            "message": {"text": "/status", "chat": {"id": 123456}},
                        },
                        {
                            "update_id": 8,
                            "message": {"text": "/help", "chat": {"id": 999999}},
                        },
                    ],
                }
            )
        raise AssertionError(request.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "ensemble.runtime._send_telegram_to_chat",
        lambda _settings, chat_id, text: sent.append((chat_id, text)) or True,
    )

    _poll_telegram_commands(settings, status_text="runtime healthy")

    assert sent == [("123456", "runtime healthy")]
    assert settings.telegram_update_offset_path.read_text() == "9"
