"""Pinned deployment must not inherit mutable research checkouts or secret configs."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from deploy.pinned_release import ENSEMBLE_UNITS, TIMERS, install_config, render_unit

from ensemble.copy_health import _service_statuses

ENSEMBLE_REPO = Path("/home/quant/dev/hyperliquid-expert-ensemble")
TRACKER_REPO = Path("/home/quant/dev/hyperliquid-trader-tracker")
RELEASE = Path("/home/quant/.local/share/copytrade/releases/ens-abc_trk-def")
CONFIG = Path("/home/quant/.config/copytrade")
IDENTITY = {
    "ensemble_repo": ENSEMBLE_REPO,
    "tracker_repo": TRACKER_REPO,
    "release": RELEASE,
    "config": CONFIG,
    "ensemble_sha": "a" * 40,
    "tracker_sha": "b" * 40,
    "ensemble_config_hash": "c" * 64,
    "tracker_config_hash": "d" * 64,
}


def test_ensemble_unit_is_pinned_to_release_not_checkout():
    template = (
        "[Unit]\nDescription=Engine\n"
        "[Service]\nType=simple\n"
        f"WorkingDirectory={ENSEMBLE_REPO}\n"
        f"EnvironmentFile={ENSEMBLE_REPO / '.env'}\n"
        f"ExecStart={ENSEMBLE_REPO / '.venv/bin/ensemble'} run\n"
    )
    rendered = render_unit(template, **IDENTITY)
    assert str(ENSEMBLE_REPO) not in rendered
    assert f"WorkingDirectory={RELEASE / 'ensemble'}" in rendered
    assert f"EnvironmentFile={CONFIG / 'ensemble.env'}" in rendered
    assert f"ExecStart={RELEASE / 'ensemble/.venv/bin/ensemble'} run" in rendered
    assert "COPYTRADE_RELEASE_ENSEMBLE_SHA=" + "a" * 40 in rendered
    assert "COPYTRADE_CONFIG_SHA256=" + "c" * 64 in rendered


def test_tracker_unit_is_pinned_to_built_binary_and_external_env():
    template = (
        "[Service]\nType=simple\n"
        f"WorkingDirectory={TRACKER_REPO}\n"
        f"EnvironmentFile={TRACKER_REPO / '.env'}\n"
        f"ExecStart={TRACKER_REPO / 'target/release/hl-tracker'}\n"
    )
    rendered = render_unit(template, **IDENTITY)
    assert str(TRACKER_REPO) not in rendered
    assert f"ExecStart={RELEASE / 'tracker/bin/hl-tracker'}" in rendered
    assert f"EnvironmentFile={CONFIG / 'tracker.env'}" in rendered
    assert "COPYTRADE_CONFIG_SHA256=" + "d" * 64 in rendered


def test_installed_timer_is_an_independent_copy():
    template = (
        "[Unit]\nDescription=Hourly\n[Timer]\nOnCalendar=*-*-* *:05:00 Asia/Bangkok\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    assert render_unit(template, **IDENTITY) == template


def test_forward_evaluator_and_assessment_timers_are_installed_and_enabled():
    expected = {
        "copytrade-forward-evaluate.service",
        "copytrade-forward-evaluate.timer",
        "copytrade-forward-assess.service",
        "copytrade-forward-assess.timer",
    }
    assert expected.issubset(set(ENSEMBLE_UNITS))
    assert {"copytrade-forward-evaluate.timer",
            "copytrade-forward-assess.timer"}.issubset(set(TIMERS))
    assert len(TIMERS)==5


def test_only_post_holdout_assessment_can_replay_missed_calendar_tick():
    templates=Path(__file__).resolve().parents[1] / "deploy" / "systemd"
    assert "Persistent=true" in (
        templates / "copytrade-forward-assess.timer"
    ).read_text()
    assert "Persistent=false" in (
        templates / "copytrade-flow-digest.timer"
    ).read_text()
    assert "Persistent=false" in (
        templates / "copytrade-forward-evaluate.timer"
    ).read_text()


def test_private_env_is_not_overwritten_with_changed_contents(tmp_path):
    source = tmp_path / "source.env"
    target = tmp_path / "external" / "target.env"
    source.write_text("TELEGRAM_BOT_TOKEN=private-placeholder\n")
    install_config(source, target)
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.read_bytes() == source.read_bytes()
    install_config(source, target)
    source.write_text("TELEGRAM_BOT_TOKEN=changed-placeholder\n")
    with pytest.raises(RuntimeError, match="differs"):
        install_config(source, target)
    assert target.read_text() == "TELEGRAM_BOT_TOKEN=private-placeholder\n"


def test_component_health_disambiguates_failed_timer(monkeypatch):
    def fake_run(command, **kwargs):
        assert command[:4] == ["systemctl", "--user", "show", "-p"]
        state = "failed" if "outcomes.timer" in command[-1] else "active"
        return SimpleNamespace(returncode=0, stdout=f"ActiveState={state}\n")

    monkeypatch.setattr("ensemble.copy_health.subprocess.run", fake_run)
    states = _service_statuses()
    assert states["evaluator_timer"] == "failed"
    assert all(
        value == "active" for key, value in states.items() if key != "evaluator_timer"
    )
