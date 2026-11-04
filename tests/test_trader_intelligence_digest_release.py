"""The C/X overlay release changes only the hourly digest, never the tracker."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from deploy import trader_intelligence_digest_release as rel

MANIFEST = {
    "ensemble_sha": "a" * 40,
    "pm_sha": "b" * 40,
    "ti_model_sha256": rel.MODEL_SHA,
    "ti_policy_sha256": rel.POLICY_SHA,
}


def test_research_dropin_pins_only_digest_and_has_rollback_boundary():
    directory = Path("/private/releases/ti-digest-test")
    text = rel.override_text(directory, MANIFEST)
    assert text.startswith(rel.HEADER)
    assert text.count("ExecStart=") == 2
    assert f"WorkingDirectory={directory / 'ensemble'}" in text
    assert f"Environment=PYTHONPATH={directory / 'pm/src'}" in text
    assert "COPYTRADE_TI_MODEL_SHA256=" + rel.MODEL_SHA in text
    assert "COPYTRADE_TI_POLICY_SHA256=" + rel.POLICY_SHA in text
    assert "--ti-research-overlay" in text
    assert "copytrade-ensemble.service" not in text
    assert "copytrade-tracker.service" not in text
    assert "copytrade-opportunity-shadow.service" not in text
    assert " --send --capture-forward " in text


def test_apply_digest_override_preserves_other_services_and_rolls_back(tmp_path, monkeypatch):
    release = tmp_path / "pinned-release"
    release.mkdir()
    users = tmp_path / "units"
    users.mkdir()
    (users / rel.SERVICE).write_text(
        "[Service]\nExecStart=/old/release/python -m ensemble.copy_digest --send\n"
    )
    override = users / (rel.SERVICE + ".d") / "90-trader-intelligence-research.conf"
    monkeypatch.setattr(rel, "USER_UNITS", users)
    monkeypatch.setattr(rel, "OVERLAY", override)
    monkeypatch.setattr(rel, "validate", lambda _p: MANIFEST)
    monkeypatch.setattr(
        rel,
        "smoke",
        lambda _p: {
            "status": "PASS_NO_TELEGRAM_NETWORK",
            "grade_status": "RESEARCH_CX_OVERLAY",
            "candidate_count": 1,
        },
    )
    commands = []

    def control(*args, **_kwargs):
        commands.append(args)
        if args[2:4] == ("show", rel.SERVICE):
            return str(release / "ensemble/.venv/bin/python") + " --ti-research-overlay"
        if args[-2:] == ("MainPID", "--value"):
            return "321" if "ensemble" in args else "654"
        if args[2:3] == ("is-active",):
            return "active"
        return ""

    monkeypatch.setattr(rel, "call", control)
    monkeypatch.setattr(
        rel.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="inactive\n", returncode=3),
    )
    report = rel.activate(release)
    assert report["trading_authorized"] is False
    assert report["ensemble_pid_unchanged"] is True
    assert "COPYTRADE_TI_MODEL_SHA256=" in override.read_text()
    assert (release / "activation.json").exists()
    assert ("systemctl", "--user", "stop", rel.TIMER) in commands
    assert ("systemctl", "--user", "start", rel.TIMER) in commands
    assert not any(
        name in command
        for command in commands
        for name in ("copytrade-ensemble.service", "copytrade-tracker.service")
        if command[2:3] in (("start",), ("restart",), ("stop",))
    )
    rel.rollback(release)
    assert not override.exists()
    assert (users / rel.SERVICE).exists()


def test_invalid_release_does_not_change_systemd(tmp_path, monkeypatch):
    release = tmp_path / "other"
    release.mkdir()
    monkeypatch.setattr(rel, "validate", lambda _p: MANIFEST)

    def unavailable(_release):
        raise ValueError("INVALID_SIGNED_SOURCE")

    monkeypatch.setattr(rel, "smoke", unavailable)
    with pytest.raises(ValueError, match="INVALID_SIGNED_SOURCE"):
        rel.activate(release)
