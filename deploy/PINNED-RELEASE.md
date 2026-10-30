# Copytrade pinned observation release

This is WP1 deployment hardening, not predictive-policy approval. All services are
**observational or hypothetical**. Live execution and automatic sizing remain
disabled. The tracker is the source authority; the Python runtime is derived.

## Release contract

Each release is identified by the complete, clean Git HEADs of both repositories.
The installer archives the Python source into a separate release tree, installs
its frozen lockfile into a **release-local** Python venv, builds the tracker from
its clean Rust HEAD and copies the binary into that same release tree. It records
source SHAs, the binary hash, the Python lock hash and separate private config
hashes in `manifest.json`.

Installed user-systemd unit files are **regular files, not checkout symlinks**.
Their working directories, imported Python code and executable paths all resolve
under `~/.local/share/copytrade/releases/<version>/`. Systemd's
`EnvironmentFile` points to independently stored, mode-0600 files under
`~/.config/copytrade/`. Existing source `.env` files are not deleted.
The separate SQLite and Telegram delivery state paths remain unchanged.
Do not commit the release, environment files, raw wallet data or unit backups.

Release identity appears in `ensemble-health.json:release` and in every
installed service's `COPYTRADE_RELEASE_*` environment. A release path by itself
does not prove source coverage, validity of prices or trading alpha.

## Controlled deployment

Run from a clean feature checkout; stage does not restart or notify anything:

```bash
uv run --frozen --no-sync python deploy/pinned_release.py stage
```

The stage step defaults to offline dependency resolution. It stops rather than
fetching missing wheels. If the required public packages are not available in
the local cache, explicitly use `stage --online`. Review the printed release
path, manifest, `units/*`, and the staged Python interpreter before activating.
The stage step does **not** reuse a release if either private config has changed
or its tracker binary hash is inconsistent.

Activation is a **separate, authorized live operation**:

```bash
uv run --frozen --no-sync python deploy/pinned_release.py activate \
  --release /home/quant/.local/share/copytrade/releases/<version> --apply
```

This makes private copies of existing user units under
`~/.local/share/copytrade/release-backups/activation-*/`, installs
independent unit files, reloads the user systemd daemon, **re-enables** timers
so their `timers.target.wants` links point to those installed fixed files rather
than mutable checkout templates, and restarts the tracker and ensemble and
then all configured timers. If activation fails, it restores prior unit files
and timer enablement state, then attempts service recovery. Unrelated systemd
units are untouched. Do not confuse a successful `systemctl restart` with
qualified source coverage.

Each activation appends an epoch to
`~/.local/share/copytrade/release-epochs.jsonl`, with the before/after
ledger ID and sequence, release IDs and explicit **UNVERIFIED** source-coverage
claim. A restart cannot retroactively repair missing external trades.
Previously saved first decisions and historical labels are never modified.

## Verification gates

1. Both Git working trees remain clean; the complete Python test suite,
   Ruff and selected static checks pass.
2. No installed `copytrade*.service` or `copytrade*.timer` is symlinked into a
   research checkout. All `ExecStart` and `WorkingDirectory` paths for
   active services point inside the fixed release; config paths point outside.
3. Both long-running services and all configured timers are active. A real clean
   restart returns live code/manifest SHAs and a fresh health timestamp.
4. The source ledger identity is unchanged, the committed outbox is contiguous,
   the Python projection can resume and its offset advances to the source cut.
   There is an explicit post-restart coverage boundary; missing fills remain
   missing.
5. The opportunity collector cursor advances and outcomes continue to mature.
   An overdue backlog and price resolution are reported explicitly.
6. The hourly digest is descriptive only; delivery state prevents a duplicate
   hourly message, and no trading interface is enabled.
7. The frozen-forward research journal captures one as-known hourly cut before
   sending and attaches HTTP acknowledgement separately. The forward evaluator
   matures 15m/1h/4h mid-only labels without trading. The final one-shot
   assessment is scheduled after the protected November holdout.

Inspect with `systemctl --user show -p FragmentPath -p ExecStart`,
`systemctl --user list-timers 'copytrade*'` and
`<release>/ensemble/.venv/bin/python -m ensemble.copy_shadow health`.
Do not run a shadow backfill against the prospective live journal to
compensate for a collection outage. Never treat a mid-price label as
executable PnL.

If an emergency manual rollback is necessary, use the versioned effective unit
files from the activation backup, call `systemctl --user daemon-reload`, and
restart the services/timers with a newly recorded coverage boundary. This may
temporarily restore checkout-dependent units; it is a recovery fallback, not
the target deployment. Never delete or rewrite the event/outcome databases.
