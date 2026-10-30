"""Build and install checkout-independent, pinned copytrade releases.

Writes only to private release/config/user-systemd roots, never to source trees
or observation databases. Run stage, inspect it, then explicitly activate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ENSEMBLE_UNITS = (
    "copytrade-ensemble.service",
    "copytrade-opportunity-shadow.service",
    "copytrade-opportunity-shadow.timer",
    "copytrade-opportunity-outcomes.service",
    "copytrade-opportunity-outcomes.timer",
    "copytrade-flow-digest.service",
    "copytrade-flow-digest.timer",
)
TRACKER_UNIT = "copytrade-tracker.service"
TIMERS = (
    "copytrade-opportunity-shadow.timer",
    "copytrade-opportunity-outcomes.timer",
    "copytrade-flow-digest.timer",
)
DATA = Path.home() / ".local/share/copytrade"
RELEASES = DATA / "releases"
CONFIG = Path.home() / ".config/copytrade"
USER_UNITS = Path.home() / ".config/systemd/user"


def run(*args: str, cwd: Path | None = None, timeout: int = 300) -> str:
    completed = subprocess.run(
        args, cwd=cwd, check=True, text=True, capture_output=True, timeout=timeout
    )
    return completed.stdout.strip()


def git_head(repo: Path) -> str:
    if run("git", "status", "--porcelain", cwd=repo):
        raise RuntimeError(f"dirty checkout: {repo}; preserve concurrent work")
    return run("git", "rev-parse", "HEAD", cwd=repo)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def render_unit(
    template: str,
    *,
    ensemble_repo: Path,
    tracker_repo: Path,
    release: Path,
    config: Path,
    ensemble_sha: str,
    tracker_sha: str,
    ensemble_config_hash: str = "unknown",
    tracker_config_hash: str = "unknown",
) -> str:
    """Pin every executable, cwd, env-file and imported source away from Git checkouts."""
    if "[Service]" not in template:
        return template  # timers: installed copies, never symlinks
    text = template.replace(
        str(tracker_repo / "target/release/hl-tracker"),
        str(release / "tracker/bin/hl-tracker"),
    )
    text = text.replace(str(ensemble_repo), str(release / "ensemble"))
    text = text.replace(str(tracker_repo), str(release / "tracker"))
    text = text.replace(
        str(release / "ensemble/.env"), str(config / "ensemble.env")
    ).replace(str(release / "tracker/.env"), str(config / "tracker.env"))
    identity = (
        "Environment=COPYTRADE_RELEASE_ENSEMBLE_SHA=" + ensemble_sha + "\n"
        "Environment=COPYTRADE_RELEASE_TRACKER_SHA=" + tracker_sha + "\n"
        "Environment=COPYTRADE_RELEASE_MANIFEST=" + str(release / "manifest.json") + "\n"
        "Environment=COPYTRADE_CONFIG_SHA256=" + (
            tracker_config_hash if "tracker/bin/hl-tracker" in text else ensemble_config_hash
        ) + "\n"
    )
    text = text.replace("[Service]\n", "[Service]\n" + identity, 1)
    if str(ensemble_repo) in text or str(tracker_repo) in text:
        raise ValueError("mutable checkout dependency remains in systemd unit")
    if "WorkingDirectory=" not in text or "ExecStart=" not in text:
        raise ValueError("incomplete service unit")
    return text


def archive_source(repo: Path, sha: str, destination: Path) -> None:
    destination.mkdir()
    with subprocess.Popen(
        ["git", "-C", str(repo), "archive", "--format=tar", sha],
        stdout=subprocess.PIPE,
    ) as archive:
        assert archive.stdout is not None
        with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
            tar.extractall(destination, filter="data")
        if archive.wait() != 0:
            raise RuntimeError("git archive failed")


def stage(ensemble_repo: Path, tracker_repo: Path, *, offline: bool) -> Path:
    ensemble_sha = git_head(ensemble_repo)
    tracker_sha = git_head(tracker_repo)
    name = f"ens-{ensemble_sha[:12]}_trk-{tracker_sha[:12]}"
    ensemble_config_hash = sha256(ensemble_repo / ".env")
    tracker_config_hash = sha256(tracker_repo / ".env")
    RELEASES.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = RELEASES / name
    if target.exists():
        existing = json.loads((target / "manifest.json").read_text())
        if existing["ensemble_sha"] != ensemble_sha or existing["tracker_sha"] != tracker_sha:
            raise RuntimeError("release identity collision")
        if sha256(target / "tracker/bin/hl-tracker") != existing["tracker_binary_sha256"]:
            raise RuntimeError("release tracker binary changed")
        if (existing["ensemble_config_sha256"] != ensemble_config_hash
                or existing["tracker_config_sha256"] != tracker_config_hash):
            raise RuntimeError("same source release with different private config; restage explicitly")
        print(json.dumps({"status": "ALREADY_STAGED", "release": str(target)}))
        return target

    tmp = Path(tempfile.mkdtemp(prefix=".stage-", dir=RELEASES))
    try:
        source = tmp / "ensemble"
        archive_source(ensemble_repo, ensemble_sha, source)

        # Build from the clean, pinned Rust HEAD, never from a guessed old binary.
        run(
            "cargo", "build", "--release", "--locked", "--bin", "hl-tracker",
            cwd=tracker_repo, timeout=300,
        )
        if git_head(ensemble_repo) != ensemble_sha or git_head(tracker_repo) != tracker_sha:
            raise RuntimeError("source checkout changed while staging")
        binary = tmp / "tracker/bin/hl-tracker"
        binary.parent.mkdir(parents=True)
        shutil.copy2(tracker_repo / "target/release/hl-tracker", binary)

        units = tmp / "units"
        units.mkdir()
        for name in ENSEMBLE_UNITS + (TRACKER_UNIT,):
            repo = tracker_repo if name == TRACKER_UNIT else ensemble_repo
            template = (repo / "deploy/systemd" / name).read_text()
            final_text = render_unit(
                template,
                ensemble_repo=ensemble_repo,
                tracker_repo=tracker_repo,
                release=target,
                config=CONFIG,
                ensemble_sha=ensemble_sha,
                tracker_sha=tracker_sha,
                ensemble_config_hash=ensemble_config_hash,
                tracker_config_hash=tracker_config_hash,
            )
            (units / name).write_text(final_text)

        manifest = {
            "schema": "copytrade-pinned-release-v1",
            "ensemble_sha": ensemble_sha,
            "tracker_sha": tracker_sha,
            "tracker_binary_sha256": sha256(binary),
            "ensemble_config_sha256": ensemble_config_hash,
            "tracker_config_sha256": tracker_config_hash,
            "ensemble_uv_lock_sha256": sha256(source / "uv.lock"),
            "staged_utc": datetime.now(UTC).isoformat(),
            "source_coverage_claim": "UNVERIFIED",
            "predictive_authority": False,
            "financial_execution": False,
        }
        # Install the venv only AFTER the directory is at its final path.
        # uv creates absolute console-script shebangs and editable .pth files.
        # Renaming an installed venv would silently point it at a deleted tmp dir.
        tmp.rename(target)
        try:
            source = target / "ensemble"
            uv = ["uv", "sync", "--frozen", "--no-dev", "--project", str(source)]
            if offline:
                uv.insert(2, "--offline")
            run(*uv, cwd=source, timeout=240)
            launcher = source / ".venv/bin/ensemble"
            if not launcher.read_text().splitlines()[0].endswith(
                str(source / ".venv/bin/python")
            ):
                raise RuntimeError("console script still references a staging directory")
            py = source / ".venv/bin/python"
            smoke = run(
                str(py), "-I", "-c",
                "import ensemble, ensemble.copy_shadow, ensemble.copy_health; "
                "print(ensemble.__file__)",
                cwd=source, timeout=25,
            )
            if not smoke.startswith(str(source / "src/ensemble")):
                raise RuntimeError(f"release Python imports do not resolve locally: {smoke}")
            if git_head(ensemble_repo) != ensemble_sha or git_head(tracker_repo) != tracker_sha:
                raise RuntimeError("source checkout changed during release build")
            (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        except BaseException:
            shutil.rmtree(target)
            raise
    except BaseException:
        if tmp.exists():
            shutil.rmtree(tmp)
        raise
    print(json.dumps({"status": "STAGED", "release": str(target), **manifest}))
    return target


def ledger_cut() -> dict[str, object]:
    db_path = DATA / "observation.db"
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as db:
        row = db.execute("SELECT ledger_id, current_seq FROM ledger_meta").fetchone()
    if not row:
        raise RuntimeError("ledger identity unavailable")
    return {"ledger_id": str(row[0]), "seq": int(row[1])}


def install_config(source: Path, dest: Path) -> None:
    if not source.exists():
        raise RuntimeError(f"missing private config: {source}")
    if dest.exists():
        if dest.read_bytes() != source.read_bytes():
            raise RuntimeError(f"external private config differs; reconcile explicitly: {dest}")
        return
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(source.read_bytes())
    dest.chmod(0o600)


def replace_unit(source: Path, dest: Path) -> None:
    fd, name = tempfile.mkstemp(prefix=".copytrade-", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(source.read_bytes())
        os.chmod(name, 0o644)
        os.replace(name, dest)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def activate(release: Path, ensemble_repo: Path, tracker_repo: Path) -> None:
    release = release.resolve(strict=True)
    if release.parent != RELEASES.resolve():
        raise RuntimeError("release must be an immediate child of the release store")
    manifest = json.loads((release / "manifest.json").read_text())
    if manifest["schema"] != "copytrade-pinned-release-v1":
        raise RuntimeError("unknown release schema")
    if manifest["ensemble_sha"] != git_head(ensemble_repo):
        raise RuntimeError("ensemble checkout no longer matches staged release")
    if manifest["tracker_sha"] != git_head(tracker_repo):
        raise RuntimeError("tracker checkout no longer matches staged release")
    if sha256(release / "tracker/bin/hl-tracker") != manifest["tracker_binary_sha256"]:
        raise RuntimeError("tracker binary does not match sealed release")
    if (sha256(ensemble_repo / ".env") != manifest["ensemble_config_sha256"]
            or sha256(tracker_repo / ".env") != manifest["tracker_config_sha256"]):
        raise RuntimeError("private config changed after staging")
    for name in ENSEMBLE_UNITS + (TRACKER_UNIT,):
        unit = (release / "units" / name).read_text()
        if str(ensemble_repo) in unit or str(tracker_repo) in unit:
            raise RuntimeError("unit references mutable checkout")
    install_config(ensemble_repo / ".env", CONFIG / "ensemble.env")
    install_config(tracker_repo / ".env", CONFIG / "tracker.env")
    USER_UNITS.mkdir(parents=True, exist_ok=True)
    old_cut = ledger_cut()

    backup_root = DATA / "release-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="activation-", dir=backup_root))
    names = ENSEMBLE_UNITS + (TRACKER_UNIT,)
    for name in names:
        dest = USER_UNITS / name
        if dest.exists():
            shutil.copyfile(dest, backup / name)  # preserves old effective unit, not symlink
    def control(*args: str) -> None:
        run("systemctl", "--user", *args, timeout=100)
    def load_old_units() -> None:
        for item in backup.iterdir():
            replace_unit(item, USER_UNITS / item.name)
        control("daemon-reload")
    try:
        control("stop", *TIMERS)
        for name in names:
            replace_unit(release / "units" / name, USER_UNITS / name)
        control("daemon-reload")
        control("reset-failed", *TIMERS)
        # Controlled restart: Rust emits a new coverage boundary; no historic relabeling.
        control("restart", TRACKER_UNIT)
        control("restart", "copytrade-ensemble.service")
        control("start", *TIMERS)
        for name in (TRACKER_UNIT, "copytrade-ensemble.service") + TIMERS:
            if run("systemctl", "--user", "is-active", name) != "active":
                raise RuntimeError(f"new deployment not active: {name}")
    except BaseException:
        load_old_units()
        control("restart", TRACKER_UNIT)
        control("restart", "copytrade-ensemble.service")
        control("start", *TIMERS)
        raise

    new_cut = ledger_cut()
    epoch = {
        "schema": "copytrade-release-epoch-v1",
        "at_utc": datetime.now(UTC).isoformat(),
        "release": release.name,
        "ensemble_sha": manifest["ensemble_sha"],
        "tracker_sha": manifest["tracker_sha"],
        "before": old_cut,
        "after": new_cut,
        "historic_coverage_repaired": False,
        "new_source_coverage": "REQUIRES_VERIFIED_SEED_AND_FEED",
        "backup_units": str(backup),
    }
    with (DATA / "release-epochs.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(epoch, sort_keys=True) + "\n")
    print(json.dumps({"status": "ACTIVATED_REQUIRES_SOURCE_VERIFICATION", **epoch}))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=("stage", "activate"))
    p.add_argument("--ensemble-repo", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--tracker-repo", type=Path,
                   default=Path("/home/quant/dev/hyperliquid-trader-tracker"))
    p.add_argument("--release", type=Path)
    p.add_argument("--online", action="store_true", help="allow network dependency resolution")
    p.add_argument("--apply", action="store_true", help="explicitly install/restart units")
    args = p.parse_args()
    ensemble = args.ensemble_repo.resolve(strict=True)
    tracker = args.tracker_repo.resolve(strict=True)
    if args.operation == "stage":
        stage(ensemble, tracker, offline=not args.online)
    else:
        if not args.apply or args.release is None:
            p.error("activation requires --apply and --release")
        activate(args.release, ensemble, tracker)


if __name__ == "__main__":
    main()
