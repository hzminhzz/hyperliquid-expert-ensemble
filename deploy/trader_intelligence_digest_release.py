"""Deploy ONLY the C/X Telegram digest from pinned ensemble + PM source.

Stages an immutable new digest interpreter and installs one reversible systemd
drop-in on the existing flow-digest service. Never restarts the ensemble,
tracker, source collectors, broker services or their timers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .pinned_release import archive_source, git_head, sha256

RELEASES = Path.home() / ".local/share/copytrade/releases"
USER_UNITS = Path.home() / ".config/systemd/user"
OVERLAY = (
    Path.home()
    / ".config/systemd/user/copytrade-flow-digest.service.d"
    / "90-trader-intelligence-research.conf"
)
TIMER = "copytrade-flow-digest.timer"
SERVICE = "copytrade-flow-digest.service"
HEADER = "# Managed by trader_intelligence_digest_release.py (C/X research only)."
MODEL_SHA = hashlib.sha256(b"unfitted-ti-v1").hexdigest()
POLICY_SHA = hashlib.sha256(b"research-only-ti-v1").hexdigest()
SEALED = (
    "ensemble/src/ensemble/copy_digest.py",
    "ensemble/src/ensemble/trader_grade_adapter.py",
    "ensemble/src/ensemble/trader_grade_producer.py",
    "ensemble/src/ensemble/trader_grade_smoke.py",
    "pm/src/copytrade_pm/trader_intelligence.py",
    "pm/src/copytrade_pm/trader_intelligence_intake.py",
)


def call(*args: str, timeout: int = 90, env: dict | None = None) -> str:
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=timeout, env=env
    ).stdout.strip()


def release_from_repos(ensemble_repo: Path, pm_repo: Path) -> Path:
    return RELEASES / (
        "ti-digest-ens-" + git_head(ensemble_repo)[:12] + "-pm-" + git_head(pm_repo)[:12]
    )


def validate(release: Path) -> dict:
    release = release.resolve(strict=True)
    if release.parent != RELEASES.resolve():
        raise ValueError("TI_RELEASE_OUTSIDE_PRIVATE_STORE")
    manifest = json.loads((release / "manifest.json").read_text())
    if (
        manifest.get("schema") != "ti_pinned_digest_v1"
        or manifest.get("research_only") is not True
        or manifest.get("trading_authorized") is not False
        or manifest.get("ti_model_sha256") != MODEL_SHA
        or manifest.get("ti_policy_sha256") != POLICY_SHA
    ):
        raise ValueError("INVALID_TI_RELEASE_MANIFEST")
    if any(sha256(release / relative) != manifest["file_sha256"][relative] for relative in SEALED):
        raise ValueError("TI_RELEASE_FILE_CHANGED")
    if not (release / "ensemble/.venv/bin/python").is_file():
        raise ValueError("TI_RELEASE_PYTHON_MISSING")
    return manifest


def environment(release: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(release / "pm/src"),
    }


def smoke(release: Path) -> dict:
    manifest = validate(release)
    result = json.loads(
        call(
            str(release / "ensemble/.venv/bin/python"),
            "-m",
            "ensemble.trader_grade_smoke",
            "--model-sha256",
            manifest["ti_model_sha256"],
            "--policy-sha256",
            manifest["ti_policy_sha256"],
            timeout=100,
            env=environment(release),
        )
    )
    if (
        result["status"] != "PASS_NO_TELEGRAM_NETWORK"
        or result["grade_status"] not in {"RESEARCH_CX_OVERLAY", "RESEARCH_CX_NO_CANDIDATE"}
        or result.get("financial_orders") is not False
    ):
        raise ValueError("TI_RELEASE_LIVE_JOURNAL_SMOKE_FAILED")
    return result


def stage(ensemble_repo: Path, pm_repo: Path) -> Path:
    ensemble_sha, pm_sha = git_head(ensemble_repo), git_head(pm_repo)
    RELEASES.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = RELEASES / ("ti-digest-ens-" + ensemble_sha[:12] + "-pm-" + pm_sha[:12])
    if target.exists():
        manifest = validate(target)
        if (manifest["ensemble_sha"], manifest["pm_sha"]) != (ensemble_sha, pm_sha):
            raise ValueError("TI_RELEASE_SHA_COLLISION")
        print(json.dumps({"status": "ALREADY_STAGED", "release": str(target)}))
        return target
    temporary = Path(tempfile.mkdtemp(prefix=".stage-ti-digest-", dir=RELEASES))
    try:
        archive_source(ensemble_repo, ensemble_sha, temporary / "ensemble")
        archive_source(pm_repo, pm_sha, temporary / "pm")
        if git_head(ensemble_repo) != ensemble_sha or git_head(pm_repo) != pm_sha:
            raise ValueError("TI_SOURCE_CHANGED_DURING_STAGE")
        manifest = {
            "schema": "ti_pinned_digest_v1",
            "ensemble_sha": ensemble_sha,
            "pm_sha": pm_sha,
            "ti_model_sha256": MODEL_SHA,
            "ti_policy_sha256": POLICY_SHA,
            "file_sha256": {relative: sha256(temporary / relative) for relative in SEALED},
            "staged_at": datetime.now(UTC).isoformat(),
            "research_only": True,
            "trading_authorized": False,
        }
        temporary.rename(target)
        try:
            call(
                "uv",
                "sync",
                "--frozen",
                "--no-dev",
                "--offline",
                "--project",
                str(target / "ensemble"),
                timeout=180,
            )
            (target / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            (target / "manifest.json").chmod(0o600)
            validate(target)
        except BaseException:
            shutil.rmtree(target)
            raise
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    print(
        json.dumps(
            {
                "status": "STAGED",
                "release": str(target),
                "ensemble_sha": ensemble_sha,
                "pm_sha": pm_sha,
            }
        )
    )
    return target


def override_text(release: Path, manifest: dict) -> str:
    py = release / "ensemble/.venv/bin/python"
    lines = [
        HEADER,
        "[Service]",
        f"WorkingDirectory={release / 'ensemble'}",
        f"Environment=PYTHONPATH={release / 'pm/src'}",
        f"Environment=COPYTRADE_TI_MODEL_SHA256={manifest['ti_model_sha256']}",
        f"Environment=COPYTRADE_TI_POLICY_SHA256={manifest['ti_policy_sha256']}",
        f"Environment=COPYTRADE_RELEASE_ENSEMBLE_SHA={manifest['ensemble_sha']}",
        f"Environment=COPYTRADE_TI_PM_SHA={manifest['pm_sha']}",
        f"Environment=COPYTRADE_RELEASE_MANIFEST={release / 'manifest.json'}",
        "ExecStart=",
        f"ExecStart={py} -m ensemble.copy_digest --send --capture-forward --ti-research-overlay",
    ]
    return "\n".join(lines) + "\n"


def _atomic_private(path: Path, payload: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".ti-stage-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _restore(old: bytes | None) -> None:
    if old is None:
        OVERLAY.unlink(missing_ok=True)
    else:
        _atomic_private(OVERLAY, old)


def activate(release: Path) -> dict:
    release = release.resolve(strict=True)
    manifest = validate(release)
    before = smoke(release)  # full genuine journal + synthetic (no network) sender
    current_unit = USER_UNITS / SERVICE
    if not current_unit.is_file() or "ensemble.copy_digest" not in current_unit.read_text():
        raise ValueError("UNKNOWN_ACTIVE_TELEGRAM_DIGEST_SERVICE")
    old = OVERLAY.read_bytes() if OVERLAY.exists() else None
    if old is not None and not old.decode("utf-8", "strict").startswith(HEADER):
        raise ValueError("UNMANAGED_DIGEST_OVERRIDE_PRESENT")
    if call("systemctl", "--user", "is-active", TIMER) != "active":
        raise ValueError("DIGEST_TIMER_NOT_ACTIVE")
    if call("systemctl", "--user", "is-active", "copytrade-ensemble.service") != "active":
        raise ValueError("ENSEMBLE_RUNTIME_UNHEALTHY")
    if call("systemctl", "--user", "is-active", "copytrade-tracker.service") != "active":
        raise ValueError("TRACKER_RUNTIME_UNHEALTHY")
    previous_ensemble_pid = call(
        "systemctl", "--user", "show", "copytrade-ensemble.service", "-p", "MainPID", "--value"
    )
    previous_tracker_pid = call(
        "systemctl", "--user", "show", "copytrade-tracker.service", "-p", "MainPID", "--value"
    )
    current_service = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if current_service.stdout.strip() == "active":
        raise ValueError("DIGEST_IN_FLIGHT_TRY_AGAIN_WITHOUT_INTERRUPTING")
    backup = release / "prior-override.txt"
    if old is not None and not backup.exists():
        _atomic_private(backup, old, mode=0o600)
    try:
        call("systemctl", "--user", "stop", TIMER)
        _atomic_private(OVERLAY, override_text(release, manifest).encode())
        call("systemctl", "--user", "daemon-reload")
        call("systemctl", "--user", "start", TIMER)
        loaded = call("systemctl", "--user", "show", SERVICE, "-p", "ExecStart", "--value")
        if str(release / "ensemble/.venv/bin/python") not in loaded or (
            "--ti-research-overlay" not in loaded
        ):
            raise ValueError("TI_NEW_DIGEST_EXECSTART_NOT_LOADED")
        if call("systemctl", "--user", "is-active", TIMER) != "active":
            raise ValueError("TI_DIGEST_TIMER_NOT_ACTIVE_AFTER_CUTOVER")
        if (
            call(
                "systemctl",
                "--user",
                "show",
                "copytrade-ensemble.service",
                "-p",
                "MainPID",
                "--value",
            )
            != previous_ensemble_pid
            or call(
                "systemctl",
                "--user",
                "show",
                "copytrade-tracker.service",
                "-p",
                "MainPID",
                "--value",
            )
            != previous_tracker_pid
        ):
            raise ValueError("TI_UNEXPECTED_RUNTIME_RESTART")
    except BaseException:
        _restore(old)
        call("systemctl", "--user", "daemon-reload")
        call("systemctl", "--user", "start", TIMER)
        raise
    receipt = {
        "schema": "ti_digest_activation_v1",
        "installed_at": datetime.now(UTC).isoformat(),
        "release": str(release),
        "ensemble_sha": manifest["ensemble_sha"],
        "pm_sha": manifest["pm_sha"],
        "research_preview": before,
        "prior_override": old is not None,
        "service": SERVICE,
        "timer": TIMER,
        "ensemble_pid_unchanged": True,
        "tracker_pid_unchanged": True,
        "trading_authorized": False,
    }
    _atomic_private(
        release / "activation.json",
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
        mode=0o600,
    )
    print(json.dumps({"status": "ACTIVATED", **receipt}, sort_keys=True))
    return receipt


def rollback(release: Path) -> None:
    release = release.resolve(strict=True)
    validate(release)
    if not OVERLAY.exists() or str(release) not in OVERLAY.read_text():
        raise ValueError("TI_ROLLBACK_RELEASE_NOT_ACTIVE")
    old = (
        (release / "prior-override.txt").read_bytes()
        if (release / "prior-override.txt").exists()
        else None
    )
    call("systemctl", "--user", "stop", TIMER)
    _restore(old)
    call("systemctl", "--user", "daemon-reload")
    call("systemctl", "--user", "start", TIMER)
    print(json.dumps({"status": "ROLLED_BACK", "release": str(release)}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "smoke", "activate", "rollback"))
    parser.add_argument("--ensemble-repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--pm-repo",
        type=Path,
        default=Path("/home/quant/.devspace/worktrees/copytrade-pm-6307ed53"),
    )
    parser.add_argument("--release", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.action == "stage":
        stage(args.ensemble_repo, args.pm_repo)
    else:
        if args.release is None:
            parser.error("--release required")
        if args.action == "smoke":
            print(json.dumps(smoke(args.release), sort_keys=True))
        elif args.action == "activate":
            if not args.apply:
                parser.error("activation requires --apply")
            activate(args.release)
        else:
            if not args.apply:
                parser.error("rollback requires --apply")
            rollback(args.release)


if __name__ == "__main__":
    main()
