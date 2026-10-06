"""Persistent shadow/advisory runtime for the Hyperliquid expert ensemble."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .consensus import ConsensusTarget, compute_equal_budget_consensus
from .posture import compute_posture
from .projection import ProjectionStore

LOG = logging.getLogger("ensemble.runtime")
USER_AGENT = "OpenAI File Downloader, XaiImageApiFetch/1.0"


@dataclass(slots=True)
class RuntimeSettings:
    ledger_db: Path
    projection_db: Path
    health_path: Path
    experts: list[str]
    poll_interval_s: float = 1.0
    snapshot_refresh_s: float = 45.0
    market_refresh_s: float = 5.0
    target_change_threshold: Decimal = Decimal("0.05")
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_chat_id_path: Path = Path("/home/quant/.local/share/copytrade/telegram-chat-id")
    telegram_update_offset_path: Path = Path(
        "/home/quant/.local/share/copytrade/telegram-update-offset"
    )
    telegram_pair_code: str | None = None

    @classmethod
    def from_env(cls) -> RuntimeSettings:
        experts = [
            item.strip().lower()
            for item in os.getenv("ENSEMBLE_EXPERTS", "").split(",")
            if item.strip()
        ]
        return cls(
            ledger_db=Path(
                os.getenv(
                    "ENSEMBLE_LEDGER_DB",
                    "/home/quant/.local/share/copytrade/observation.db",
                )
            ),
            projection_db=Path(
                os.getenv(
                    "ENSEMBLE_PROJECTION_DB",
                    "/home/quant/.local/share/copytrade/projection.db",
                )
            ),
            health_path=Path(
                os.getenv(
                    "ENSEMBLE_HEALTH_PATH",
                    "/home/quant/.local/share/copytrade/ensemble-health.json",
                )
            ),
            experts=experts,
            poll_interval_s=float(os.getenv("ENSEMBLE_POLL_INTERVAL_S", "1")),
            snapshot_refresh_s=float(os.getenv("ENSEMBLE_SNAPSHOT_REFRESH_S", "45")),
            market_refresh_s=float(os.getenv("ENSEMBLE_MARKET_REFRESH_S", "5")),
            target_change_threshold=Decimal(
                os.getenv("ENSEMBLE_TARGET_CHANGE_THRESHOLD", "0.05")
            ),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
            telegram_chat_id_path=Path(
                os.getenv(
                    "ENSEMBLE_TELEGRAM_CHAT_ID_PATH",
                    "/home/quant/.local/share/copytrade/telegram-chat-id",
                )
            ),
            telegram_update_offset_path=Path(
                os.getenv(
                    "ENSEMBLE_TELEGRAM_UPDATE_OFFSET_PATH",
                    "/home/quant/.local/share/copytrade/telegram-update-offset",
                )
            ),
            telegram_pair_code=os.getenv("ENSEMBLE_TELEGRAM_PAIR_CODE") or None,
        )


def _post_json(url: str, payload: dict[str, Any], timeout: float = 15.0) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_clearinghouse_state(wallet: str) -> dict[str, Any]:
    raw = _post_json(
        "https://api.hyperliquid.xyz/info",
        {"type": "clearinghouseState", "user": wallet},
    )
    if not isinstance(raw, dict):
        raise TypeError(f"clearinghouseState for {wallet} returned non-object")
    return raw


def fetch_all_mids() -> dict[str, Decimal]:
    raw = _post_json("https://api.hyperliquid.xyz/info", {"type": "allMids"})
    if not isinstance(raw, dict):
        raise TypeError("allMids returned non-object")
    mids: dict[str, Decimal] = {}
    for coin, value in raw.items():
        try:
            mids[str(coin)] = Decimal(str(value))
        except InvalidOperation:
            LOG.warning("ignoring invalid allMids value for %s", coin)
            continue
    return mids


def snapshot_wallet(store: ProjectionStore, wallet: str) -> None:
    raw = fetch_clearinghouse_state(wallet)
    margin = raw.get("marginSummary")
    if not isinstance(margin, dict) or margin.get("accountValue") is None:
        raise RuntimeError(f"clearinghouseState for {wallet} missing accountValue")
    equity = Decimal(str(margin["accountValue"]))
    rows = raw.get("assetPositions")
    if not isinstance(rows, list):
        raise TypeError(f"clearinghouseState for {wallet} missing assetPositions")

    positions: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pos = row.get("position")
        if not isinstance(pos, dict):
            continue
        coin = pos.get("coin")
        szi = pos.get("szi")
        if coin is None or szi is None:
            continue
        qty = Decimal(str(szi))
        if qty == 0:
            continue
        positions.append(
            {
                "coin": str(coin),
                "szi": str(qty),
                "entry_px": str(pos.get("entryPx") or "0"),
            }
        )

    store.replace_wallet_snapshot(wallet, positions, equity, datetime.now(UTC))


def _ledger_status(path: Path) -> dict[str, Any]:
    import sqlite3

    if not path.exists():
        return {"exists": False, "ledger_id": None, "current_seq": 0}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT ledger_id, current_seq, retention_floor FROM ledger_meta LIMIT 1"
        ).fetchone()
        if not row:
            return {"exists": True, "ledger_id": None, "current_seq": 0}
        return {
            "exists": True,
            "ledger_id": str(row[0]),
            "current_seq": int(row[1]),
            "retention_floor": int(row[2]),
        }
    finally:
        conn.close()


def _load_paired_telegram_chat(settings: RuntimeSettings) -> str | None:
    if settings.telegram_chat_id:
        return settings.telegram_chat_id
    if not settings.telegram_chat_id_path.exists():
        return None
    value = settings.telegram_chat_id_path.read_text(encoding="utf-8").strip()
    if not value:
        return None
    settings.telegram_chat_id = value
    return value


def _load_telegram_update_offset(settings: RuntimeSettings) -> int:
    if not settings.telegram_update_offset_path.exists():
        return 0
    raw = settings.telegram_update_offset_path.read_text(encoding="utf-8").strip()
    try:
        return int(raw)
    except ValueError:
        LOG.warning("invalid Telegram update offset %r; resetting to 0", raw)
        return 0


def _save_telegram_update_offset(settings: RuntimeSettings, offset: int) -> None:
    settings.telegram_update_offset_path.parent.mkdir(parents=True, exist_ok=True)
    settings.telegram_update_offset_path.write_text(str(offset), encoding="utf-8")
    os.chmod(settings.telegram_update_offset_path, 0o600)


def _send_telegram_to_chat(settings: RuntimeSettings, chat_id: str, text: str) -> bool:
    if not settings.telegram_bot_token:
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15.0) as response:
            data = json.loads(response.read().decode("utf-8"))
        return bool(data.get("ok"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        LOG.warning("telegram delivery failed: %s", exc)
        return False


def _send_telegram(settings: RuntimeSettings, text: str) -> bool:
    chat_id = _load_paired_telegram_chat(settings)
    if not chat_id:
        return False
    return _send_telegram_to_chat(settings, chat_id, text)


def _poll_telegram_commands(settings: RuntimeSettings, status_text: str) -> None:
    if not settings.telegram_bot_token:
        return

    offset = _load_telegram_update_offset(settings)
    query = urllib.parse.urlencode({"offset": offset, "timeout": 0})
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        LOG.warning("telegram command poll failed: %s", exc)
        return

    updates = data.get("result", [])
    if not isinstance(updates, list):
        return

    next_offset = offset
    for update in updates:
        if not isinstance(update, dict):
            continue
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            next_offset = max(next_offset, update_id + 1)

        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            continue
        text = str(message.get("text") or "").strip()
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("id") is None:
            continue
        chat_id = str(chat["id"])

        expected_pair = (
            f"/pair {settings.telegram_pair_code}" if settings.telegram_pair_code else None
        )
        if expected_pair and text == expected_pair:
            paired = _load_paired_telegram_chat(settings)
            if paired is None or paired == chat_id:
                settings.telegram_chat_id_path.parent.mkdir(parents=True, exist_ok=True)
                settings.telegram_chat_id_path.write_text(chat_id, encoding="utf-8")
                os.chmod(settings.telegram_chat_id_path, 0o600)
                settings.telegram_chat_id = chat_id
                _send_telegram_to_chat(
                    settings,
                    chat_id,
                    "Paired. The advisory runtime is live; financial execution is disabled. "
                    "Send /status for the current system state or /help for commands.",
                )
                LOG.info("telegram advisory destination paired and acknowledged")
            continue

        paired_chat = _load_paired_telegram_chat(settings)
        if paired_chat != chat_id:
            continue

        if text == "/status":
            _send_telegram_to_chat(settings, chat_id, status_text)
        elif text == "/help":
            _send_telegram_to_chat(
                settings,
                chat_id,
                "Commands:\n/status — current advisory runtime state\n"
                "/help — this message\n"
                "Financial execution is disabled in V1.",
            )
        elif text:
            _send_telegram_to_chat(
                settings,
                chat_id,
                "Advisory bot is online. Send /status or /help. "
                "Financial execution is disabled.",
            )

    if next_offset != offset:
        _save_telegram_update_offset(settings, next_offset)


def _format_target(target: ConsensusTarget) -> str:
    votes = ", ".join(
        f"{c.expert_id[:8]}={c.raw_posture:+.2f}"
        for c in target.contributions
    )
    actionable = "ACTIONABLE" if target.is_actionable else f"BLOCKED:{target.blocker_code}"
    return (
        f"Expert consensus · {target.coin}\n"
        f"Target {target.observed_target:+.3f}  [{target.lower_bound:+.3f}, {target.upper_bound:+.3f}]\n"
        f"Coverage missing {target.missing_mass:.3f} · {actionable}\n"
        f"{votes}"
    )


class EnsembleRuntime:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.settings.projection_db.parent.mkdir(parents=True, exist_ok=True)
        self.settings.health_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = ProjectionStore(settings.projection_db)
        self.mids: dict[str, Decimal] = {}
        self.prior_targets: dict[str, ConsensusTarget] = {}
        self.last_snapshot_at = 0.0
        self.last_market_at = 0.0
        self.last_error: str | None = None
        self.telegram_last_ok: bool | None = None

    def close(self) -> None:
        self.store.close()

    def refresh_snapshots(self) -> None:
        failures: list[str] = []
        for wallet in self.settings.experts:
            try:
                snapshot_wallet(self.store, wallet)
            except (
                urllib.error.URLError,
                TimeoutError,
                TypeError,
                ValueError,
                ArithmeticError,
            ) as exc:
                failures.append(f"{wallet}:{exc}")
        if failures:
            raise RuntimeError("; ".join(failures))

    def refresh_market(self) -> None:
        self.mids = fetch_all_mids()

    def compute_targets(self) -> dict[str, ConsensusTarget]:
        positions = self.store.list_positions()
        coins = {p.coin for p in positions} | set(self.prior_targets)
        targets: dict[str, ConsensusTarget] = {}
        now = datetime.now(UTC)

        for coin in sorted(coins):
            postures = []
            for wallet in self.settings.experts:
                pos = self.store.get_position(wallet, coin)
                equity_row = self.store.get_equity(wallet)
                equity = equity_row[0] if equity_row else None
                equity_age = (
                    max(0.0, (now - equity_row[1]).total_seconds() / 60.0)
                    if equity_row
                    else 10_000.0
                )
                qty = pos.szi if pos else Decimal(0)
                valuation = self.mids.get(
                    coin,
                    pos.avg_entry if pos is not None else Decimal(0),
                )
                postures.append(
                    compute_posture(
                        wallet,
                        coin,
                        qty,
                        valuation,
                        equity,
                        equity_age_minutes=equity_age,
                    )
                )
            targets[coin] = compute_equal_budget_consensus(coin, postures, as_of=now)
        return targets

    def maybe_notify(self, targets: dict[str, ConsensusTarget]) -> None:
        for coin, target in targets.items():
            prior = self.prior_targets.get(coin)
            changed = prior is None
            if prior is not None:
                changed = (
                    abs(target.observed_target - prior.observed_target)
                    >= self.settings.target_change_threshold
                    or target.is_actionable != prior.is_actionable
                    or target.blocker_code != prior.blocker_code
                )
            if changed:
                delivered = _send_telegram(self.settings, _format_target(target))
                if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
                    self.telegram_last_ok = delivered
                LOG.info(
                    "consensus %s target=%s actionable=%s delivered=%s",
                    coin,
                    target.observed_target,
                    target.is_actionable,
                    delivered,
                )
        self.prior_targets = targets

    def telegram_status_text(self, targets: dict[str, ConsensusTarget]) -> str:
        ledger = _ledger_status(self.settings.ledger_db)
        ledger_id = ledger.get("ledger_id")
        consumed = self.store.get_offset(str(ledger_id)) if ledger_id else 0
        ledger_seq = int(ledger.get("current_seq", 0))
        target_lines = [
            f"{coin}: {target.observed_target:+.3f}"
            for coin, target in sorted(targets.items())
        ]
        return "\n".join(
            [
                "Advisory runtime: ONLINE",
                f"Experts: {len(self.settings.experts)}",
                f"Open positions: {len(self.store.list_positions())}",
                f"Ledger: {consumed}/{ledger_seq} (lag {max(0, ledger_seq - consumed)})",
                "Targets:",
                *(target_lines or ["none"]),
                "Execution: FINANCIAL_EFFECT_FORBIDDEN",
            ]
        )

    def write_health(self, targets: dict[str, ConsensusTarget]) -> None:
        ledger = _ledger_status(self.settings.ledger_db)
        ledger_id = ledger.get("ledger_id")
        consumed = self.store.get_offset(str(ledger_id)) if ledger_id else 0
        health = {
            "schema_version": "1.0",
            "timestamp": datetime.now(UTC).isoformat(),
            "mode": "advisory",
            "authority": {
                "financial_execution": False,
                "financial_effect_guard": "FINANCIAL_EFFECT_FORBIDDEN",
                "observation": True,
            },
            "ingestion": {
                "ledger_exists": ledger.get("exists", False),
                "ledger_id": ledger_id,
                "ledger_seq": ledger.get("current_seq", 0),
                "consumed_seq": consumed,
                "lag": max(0, int(ledger.get("current_seq", 0)) - consumed),
            },
            "experts": {
                "configured": len(self.settings.experts),
                "with_equity": sum(
                    self.store.get_equity(w) is not None for w in self.settings.experts
                ),
            },
            "worldview": {
                "open_positions": len(self.store.list_positions()),
                "targets": {
                    coin: {
                        "target": str(target.observed_target),
                        "lower": str(target.lower_bound),
                        "upper": str(target.upper_bound),
                        "actionable": target.is_actionable,
                        "blocker": target.blocker_code,
                    }
                    for coin, target in targets.items()
                },
            },
            "telegram": {
                "configured": bool(
                    self.settings.telegram_bot_token and self.settings.telegram_chat_id
                ),
                "last_delivery_ok": self.telegram_last_ok,
            },
            "last_error": self.last_error,
        }
        tmp = self.settings.health_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(health, indent=2), encoding="utf-8")
        tmp.replace(self.settings.health_path)

    def step(self) -> None:
        now = time.monotonic()
        if now - self.last_snapshot_at >= self.settings.snapshot_refresh_s:
            self.refresh_snapshots()
            self.last_snapshot_at = now
        if now - self.last_market_at >= self.settings.market_refresh_s:
            self.refresh_market()
            self.last_market_at = now

        while self.store.consume_from_ledger_db(self.settings.ledger_db, batch_size=1000):
            pass

        targets = self.compute_targets()
        self.maybe_notify(targets)
        _poll_telegram_commands(self.settings, self.telegram_status_text(targets))
        self.last_error = None
        self.write_health(targets)

    def run_forever(self) -> None:
        if not self.settings.experts:
            raise RuntimeError("ENSEMBLE_EXPERTS is empty")
        LOG.info(
            "starting advisory runtime with %d experts; financial execution forbidden",
            len(self.settings.experts),
        )
        while True:
            try:
                self.step()
            except (
                urllib.error.URLError,
                TimeoutError,
                TypeError,
                ValueError,
                ArithmeticError,
                OSError,
                sqlite3.Error,
            ) as exc:
                self.last_error = str(exc)
                LOG.exception("runtime step failed")
                self.write_health(self.prior_targets)
            time.sleep(self.settings.poll_interval_s)


def run() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    runtime = EnsembleRuntime(RuntimeSettings.from_env())
    try:
        runtime.run_forever()
    except KeyboardInterrupt:
        LOG.info("stopping advisory runtime")
    finally:
        runtime.close()
