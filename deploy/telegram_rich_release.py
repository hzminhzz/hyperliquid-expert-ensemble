"""Deploy a pinned presentation-only hourly Telegram digest overlay.

The source-of-truth production release and Trader Intelligence C/X model/policy
remain immutable. This overlays only the two HTML formatting modules for the
copytrade-flow-digest oneshot, not tracker, ensemble, or any broker.
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

SOURCE = Path(__file__).resolve().parents[1]
BASE = Path("/home/quant/.local/share/copytrade/releases/ti-digest-ens-1c591080f5e9-pm-d72f9545f587")
RELEASES = Path("/home/quant/.local/share/copytrade/releases")
UNIT = "copytrade-flow-digest.service"
DROPIN = Path("/home/quant/.config/systemd/user/copytrade-flow-digest.service.d/92-rich-telegram-20261104.conf")
PM_SOURCE = BASE / "pm/src"
BASE_SOURCE = BASE / "ensemble/src/ensemble"
PATCHED = ("copy_board.py", "trader_grade_adapter.py")
STATE = Path("/home/quant/dev/copytrade-pm/data/private/tracker_roster_20261104")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(*args: str, timeout: int = 50, env: dict[str, str] | None = None) -> str:
    run = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env, check=False)
    if run.returncode:
        raise RuntimeError(f"COMMAND_FAILED: {list(args[:3])} {run.stderr[:350]}")
    return run.stdout.strip()


def current_sha() -> str:
    result = command("git", "-C", str(SOURCE), "rev-parse", "HEAD")
    if len(result) != 40:
        raise RuntimeError("UNPINNED_GIT_COMMIT")
    return result


def release_path() -> Path:
    return RELEASES / ("ti-rich-telegram-" + current_sha()[:12])


def ensure_scope() -> None:
    if not (BASE_SOURCE / "copy_board.py").exists():
        raise RuntimeError("PINNED_SOURCE_MISSING")
    changes = []
    base_py = {p.name for p in BASE_SOURCE.glob("*.py")}
    source_py = {p.name for p in (SOURCE / "src/ensemble").glob("*.py")}
    if base_py != source_py:
        raise RuntimeError("NOT_A_FORMAT_ONLY_CHANGE: source file set differed")
    for name in sorted(base_py):
        if sha(BASE_SOURCE / name) != sha(SOURCE / "src/ensemble" / name):
            changes.append(name)
    if changes != sorted(PATCHED):
        raise RuntimeError(f"NOT_FORMAT_ONLY_CODE_DIFF: {changes}")
    if sha(BASE_SOURCE / "copy_digest.py") != sha(SOURCE / "src/ensemble/copy_digest.py"):
        raise RuntimeError("DELIBERATE_DELIVERY_LOGIC_CHANGE_BLOCKED")
    if sha(BASE_SOURCE / "trader_grade_producer.py") != sha(SOURCE / "src/ensemble/trader_grade_producer.py"):
        raise RuntimeError("GRADE_PRODUCER_CHANGE_BLOCKED")
    if command("git", "-C", str(SOURCE), "status", "--porcelain=v1"):
        raise RuntimeError("DEPLOY_SOURCE_WORKTREE_DIRTY")
    old = json.loads((BASE / "manifest.json").read_text())
    if old.get("trading_authorized") is not False or old.get("research_only") is not True:
        raise RuntimeError("PINNED_TRAINING_MODEL_AUTHORITY_CHANGED")


def manifest_for(path: Path) -> dict:
    old = json.loads((BASE / "manifest.json").read_text())
    content = dict(old)
    content.update({
        "schema": "ti_rich_telegram_presentation_release_v1",
        "parent_manifest_path": str(BASE / "manifest.json"),
        "parent_manifest_sha256": sha(BASE / "manifest.json"),
        "ensemble_sha": current_sha(),
        "change_scope": "TWO_RENDERERS_ONLY_HTML_CARDS_EMOJI_NO_RESEARCH_OR_ORDER_LOGIC",
        "release_dir": str(path),
        "patched_files": list(PATCHED),
        "source_hashes": {p.name: sha(p) for p in sorted((path / "ensemble/src/ensemble").glob("*.py"))},
        "trading_authorized": False,
        "research_only": True,
        "grade_policy_qualification": "C_X_ONLY_0R_NO_A_B",
        "created_at_utc": datetime.now(UTC).isoformat()
    })
    old_hashes = dict(content.get("file_sha256", {}))
    for n in PATCHED:
        old_hashes["ensemble/src/ensemble/" + n] = sha(path / "ensemble/src/ensemble" / n)
    content["file_sha256"] = old_hashes
    return content


def prepare() -> dict:
    ensure_scope()
    final = release_path()
    if final.exists():
        raise RuntimeError("RELEASE_ALREADY_EXISTS")
    tmp = Path(tempfile.mkdtemp(prefix=".ti-rich-stage-", dir=RELEASES))
    os.chmod(tmp, 0o700)
    try:
        package = tmp / "ensemble/src/ensemble"
        shutil.copytree(BASE_SOURCE, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for filename in PATCHED:
            shutil.copyfile(SOURCE / "src/ensemble" / filename, package / filename)
        m = manifest_for(tmp)
        # The actual final root is stable after rename; record that, not temp paths.
        m["release_dir"] = str(final)
        with (tmp / "manifest.json").open("w") as f:
            json.dump(m, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp / "manifest.json", 0o600)
        (tmp / "source-commit.txt").write_text(current_sha() + "\n")
        os.chmod(tmp / "source-commit.txt", 0o600)
        if sha(package / "copy_board.py") != sha(SOURCE / "src/ensemble/copy_board.py"):
            raise RuntimeError("STAGED_RENDERER_HASH_MISMATCH")
        if sha(package / "trader_grade_adapter.py") != sha(SOURCE / "src/ensemble/trader_grade_adapter.py"):
            raise RuntimeError("STAGED_GRADE_RENDERER_HASH_MISMATCH")
        if sha(package / "copy_digest.py") != sha(BASE_SOURCE / "copy_digest.py"):
            raise RuntimeError("SOURCE_DELIVERY_BEHAVIOR_CHANGED")
        os.rename(tmp, final)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    return validate(final)


def validate(root: Path) -> dict:
    if not root.exists() or root.parent != RELEASES:
        raise RuntimeError("RELEASE_ROOT_INVALID")
    m = json.loads((root / "manifest.json").read_text())
    if m["ensemble_sha"] != current_sha() or m["trading_authorized"] is not False:
        raise RuntimeError("RELEASE_MANIFEST_MISMATCH")
    if m["source_hashes"] != {p.name: sha(p) for p in sorted((root / "ensemble/src/ensemble").glob("*.py"))}:
        raise RuntimeError("RELEASE_SOURCE_TAMPER")
    venv = BASE / "ensemble/.venv/bin/python"
    env = dict(os.environ, PYTHONPATH=str(root / "ensemble/src") + os.pathsep + str(PM_SOURCE))
    check = command(str(venv), "-c",
        "from ensemble.copy_board import render_flow_digest;"
        "from ensemble.trader_grade_adapter import grade_appendix;"
        "import ensemble.copy_board,ensemble.copy_digest,ensemble.trader_grade_producer;"
        "import json;print(json.dumps({'board':ensemble.copy_board.__file__,"
        "'digest':ensemble.copy_digest.__file__}))", env=env, timeout=20)
    payload = json.loads(check)
    if not payload["board"].startswith(str(root)):
        raise RuntimeError("RENDERER_NOT_LOADED_FROM_NEW_RELEASE")
    if not payload["digest"].startswith(str(root)):
        raise RuntimeError("DIGEST_NOT_LOADED_FROM_NEW_RELEASE")
    # Render as-known real observed flow from journal, without SEND or state change.
    preview = command(str(venv), "-m", "ensemble.copy_digest", env=env, timeout=30)
    if "📡 <b>HYPERLIQUID" not in preview or "🔒 <b>Predictive edge:" not in preview:
        raise RuntimeError("RICH_TEMPLATING_NOT_ACTIVE")
    if "NOT QUALIFIED" not in preview:
        raise RuntimeError("UNVERIFIED_ALPHA_PROMOTED")
    return {"status": "PINNED_FORMAT_READY", "release": str(root),
        "source_commit": m["ensemble_sha"], "preview_bytes": len(preview.encode()),
        "source_file_count": len(m["source_hashes"]), "financial_execution": False}


def activate(root: Path) -> dict:
    report = validate(root)
    if DROPIN.exists():
        raise RuntimeError("FORMAT_OVERRIDE_ALREADY_PRESENT")
    # A separate roster drop-in (91) attests the active env; do not remove it.
    existing = command("systemctl", "--user", "show", UNIT, "-p", "Environment")
    if "COPYTRADE_TI_MODEL_SHA256=" not in existing or "COPYTRADE_TI_POLICY_SHA256=" not in existing:
        raise RuntimeError("TI_RESEARCH_ATTESTATION_MISSING")
    if command("systemctl", "--user", "is-active", "copytrade-flow-digest.timer") != "active":
        raise RuntimeError("DIGEST_TIMER_NOT_ACTIVE")
    content = (
        "[Service]\n"
        "# Presentation-only release. C/X research policy, source inputs and send cadence unchanged.\n"
        f"Environment=PYTHONPATH={root / 'ensemble/src'}:{PM_SOURCE}\n"
        f"Environment=COPYTRADE_RELEASE_ENSEMBLE_SHA={current_sha()}\n"
        f"Environment=COPYTRADE_RELEASE_MANIFEST={root / 'manifest.json'}\n"
    )
    DROPIN.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=DROPIN.parent, prefix=".rich-telegram-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o644)
        os.replace(name, DROPIN)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    try:
        command("systemctl", "--user", "daemon-reload")
        env_str = command("systemctl", "--user", "show", UNIT, "-p", "Environment")
        if str(root / "ensemble/src") not in env_str or current_sha() not in env_str:
            raise RuntimeError("SYSTEMD_PRESENTATION_OVERRIDE_UNVERIFIED")
        if "COPYTRADE_TI_MODEL_SHA256=" not in env_str or "COPYTRADE_CONFIG_SHA256=" not in env_str:
            raise RuntimeError("SYSTEMD_RESEARCH_OR_ROSTER_OVERRIDDEN")
        state = {"schema": "copytrade_rich_telegram_presentation_activation_v1",
            **report,
            "activated_at_utc": datetime.now(UTC).isoformat(),
            "unit": UNIT, "dropin": str(DROPIN),
            "send_schedule_changed": False,
            "previous_digest_release": str(BASE),
            "telegram_notice_message_7535_edited_separately": True,
            "fallback": "Remove only 92-rich-telegram-20261104.conf and daemon-reload"}
        STATE.mkdir(parents=True, exist_ok=True)
        p = STATE / "RICH_TELEGRAM_RELEASE_20261104.json"
        p.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.chmod(p, 0o600)
        return state
    except Exception:
        if DROPIN.exists():
            DROPIN.unlink()
        command("systemctl", "--user", "daemon-reload")
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "verify", "activate"))
    parser.add_argument("--release", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        out = prepare()
    else:
        path = args.release or release_path()
        out = validate(path) if args.action == "verify" else activate(path)
    print(json.dumps(out, sort_keys=True))

if __name__ == "__main__":
    main()
