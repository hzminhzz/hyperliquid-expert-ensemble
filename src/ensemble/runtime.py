"""Persistent shadow/advisory runtime for the Hyperliquid expert ensemble."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .consensus import (
    ConsensusTarget,
    compute_equal_budget_consensus,
    compute_hierarchical_cluster_consensus,
)
from .posture import compute_posture
from .projection import ProjectionStore
from .research_recorder import CutInput, ResearchRecorder, minute_bucket

LOG = logging.getLogger("ensemble.runtime")
USER_AGENT = "OpenAI File Downloader, XaiImageApiFetch/1.0"
TELEGRAM_SAFE_MAX_CHARS = 3500


@dataclass(slots=True, frozen=True)
class AdvisoryAssessment:
    direction: str
    grade: str
    follow_state: str
    shadow_risk_cap: Decimal
    coverage: Decimal
    agreement: Decimal | None
    support_clusters: int
    oppose_clusters: int
    flat_clusters: int
    rationale: str


@dataclass(slots=True)
class RuntimeSettings:
    ledger_db: Path
    projection_db: Path
    health_path: Path
    experts: list[str]
    cluster_manifest_path: Path | None = None
    research_db: Path | None = None
    research_assumed_cost_bps: Decimal = Decimal(10)
    telegram_min_interval_s: float = 10.0
    telegram_per_instrument_cooldown_s: float = 900.0
    telegram_backoff_until: float = 0.0
    poll_interval_s: float = 1.0
    market_refresh_s: float = 5.0
    target_change_threshold: Decimal = Decimal("0.05")
    notify_initial_targets: bool = True
    notify_warmup_s: float = 180.0
    notify_max_per_minute: int = 4
    notify_per_coin_cooldown_s: float = 600.0
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
            research_db=(
                Path(os.environ["ENSEMBLE_RESEARCH_DB"])
                if os.getenv("ENSEMBLE_RESEARCH_DB")
                else Path("/home/quant/.local/share/copytrade/prospective-research.db")
            ),
            research_assumed_cost_bps=Decimal(
                os.getenv("ENSEMBLE_RESEARCH_COST_BPS", "10")
            ),
            telegram_min_interval_s=float(
                os.getenv("ENSEMBLE_TELEGRAM_MIN_INTERVAL_S", "10")
            ),
            telegram_per_instrument_cooldown_s=float(
                os.getenv("ENSEMBLE_TELEGRAM_COIN_COOLDOWN_S", "900")
            ),
            cluster_manifest_path=(
                Path(os.environ["ENSEMBLE_CLUSTER_MANIFEST"])
                if os.getenv("ENSEMBLE_CLUSTER_MANIFEST")
                else None
            ),
            poll_interval_s=float(os.getenv("ENSEMBLE_POLL_INTERVAL_S", "1")),
            market_refresh_s=float(os.getenv("ENSEMBLE_MARKET_REFRESH_S", "5")),
            target_change_threshold=Decimal(
                os.getenv("ENSEMBLE_TARGET_CHANGE_THRESHOLD", "0.05")
            ),
            notify_initial_targets=(
                os.getenv("ENSEMBLE_NOTIFY_INITIAL_TARGETS", "true").strip().lower()
                in {"1", "true", "yes", "on"}
            ),
            notify_warmup_s=float(os.getenv("ENSEMBLE_NOTIFY_WARMUP_S", "180")),
            notify_max_per_minute=int(os.getenv("ENSEMBLE_NOTIFY_MAX_PER_MINUTE", "4")),
            notify_per_coin_cooldown_s=float(
                os.getenv("ENSEMBLE_NOTIFY_PER_COIN_COOLDOWN_S", "600")
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


def _load_cluster_manifest(
    path: Path | None, experts: list[str]
) -> list[list[str]] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_clusters = payload.get("clusters") if isinstance(payload, dict) else None
    if not isinstance(raw_clusters, list) or not raw_clusters:
        raise ValueError("cluster manifest must contain a non-empty clusters list")

    expected = set(experts)
    seen: set[str] = set()
    clusters: list[list[str]] = []
    for raw_cluster in raw_clusters:
        if not isinstance(raw_cluster, list) or not raw_cluster:
            raise ValueError("cluster manifest contains an empty/invalid cluster")
        cluster: list[str] = []
        for raw_wallet in raw_cluster:
            wallet = str(raw_wallet).strip().lower()
            if wallet not in expected:
                raise ValueError(f"cluster manifest contains unknown expert {wallet}")
            if wallet in seen:
                raise ValueError(f"cluster manifest duplicates expert {wallet}")
            seen.add(wallet)
            cluster.append(wallet)
        clusters.append(cluster)

    missing = expected - seen
    if missing:
        raise ValueError(
            f"cluster manifest omits {len(missing)} configured expert(s)"
        )
    return clusters


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
    if time.monotonic() < settings.telegram_backoff_until:
        return False
    if len(text) > TELEGRAM_SAFE_MAX_CHARS:
        LOG.warning(
            "telegram message truncated from %d to %d characters",
            len(text),
            TELEGRAM_SAFE_MAX_CHARS,
        )
        text = text[: TELEGRAM_SAFE_MAX_CHARS - 1] + "…"
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
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            delay = 90.0
            try:
                body = json.loads(exc.read().decode("utf-8"))
                retry = body.get("parameters", {}).get("retry_after")
                if isinstance(retry, (int, float)) and retry > 0:
                    delay = max(delay, float(retry))
            except (ValueError, TypeError, OSError, AttributeError):
                pass
            settings.telegram_backoff_until = time.monotonic() + delay
            LOG.warning("Telegram rate-limited; suspending outbound sends for %.0fs", delay)
        else:
            LOG.warning("Telegram HTTP delivery failed with status %s", exc.code)
            settings.telegram_backoff_until = time.monotonic() + 30.0
        exc.close()
        return False
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        LOG.warning("telegram delivery failed: %s", exc)
        settings.telegram_backoff_until = time.monotonic() + 30.0
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
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
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


def _cluster_values(
    target: ConsensusTarget, clusters: list[list[str]] | None
) -> list[Decimal]:
    by_expert = {c.expert_id: c for c in target.contributions}
    if clusters is None:
        return [c.weighted_contribution for c in target.contributions]
    values: list[Decimal] = []
    for cluster in clusters:
        values.append(
            sum(
                (
                    by_expert[expert_id].weighted_contribution
                    for expert_id in cluster
                    if expert_id in by_expert
                ),
                start=Decimal(0),
            )
        )
    return values


def _assess_target(
    target: ConsensusTarget, clusters: list[list[str]] | None
) -> AdvisoryAssessment:
    direction_sign = 1 if target.observed_target > 0 else -1 if target.observed_target < 0 else 0
    direction = "LONG" if direction_sign > 0 else "SHORT" if direction_sign < 0 else "FLAT"
    values = _cluster_values(target, clusters)
    if direction_sign > 0:
        support = [value for value in values if value > 0]
        oppose = [value for value in values if value < 0]
    elif direction_sign < 0:
        support = [value for value in values if value < 0]
        oppose = [value for value in values if value > 0]
    else:
        support, oppose = [], []
    flat_clusters = len(values) - len(support) - len(oppose)
    support_signal = sum((abs(v) for v in support), start=Decimal(0))
    oppose_signal = sum((abs(v) for v in oppose), start=Decimal(0))
    directional_signal = support_signal + oppose_signal
    agreement = (
        support_signal / directional_signal if directional_signal > 0 else None
    )
    coverage = max(Decimal(0), min(Decimal(1), Decimal(1) - target.missing_mass))

    if not target.is_actionable:
        reason = {
            "AMBIGUOUS_BOUNDS": "uncertainty interval crosses zero",
            "INSUFFICIENT_COVERAGE": "too much cluster weight is unavailable",
        }.get(target.blocker_code or "", target.blocker_code or "consensus gate blocked")
        return AdvisoryAssessment(
            direction=direction,
            grade="BLOCKED",
            follow_state="NO-FOLLOW",
            shadow_risk_cap=Decimal(0),
            coverage=coverage,
            agreement=agreement,
            support_clusters=len(support),
            oppose_clusters=len(oppose),
            flat_clusters=flat_clusters,
            rationale=reason,
        )

    if direction_sign == 0 or agreement is None:
        return AdvisoryAssessment(
            direction=direction,
            grade="D",
            follow_state="IGNORE",
            shadow_risk_cap=Decimal(0),
            coverage=coverage,
            agreement=agreement,
            support_clusters=len(support),
            oppose_clusters=len(oppose),
            flat_clusters=flat_clusters,
            rationale="no directional cluster consensus",
        )

    strength = abs(target.observed_target)
    if (
        strength >= Decimal("0.12")
        and agreement >= Decimal("0.80")
        and coverage >= Decimal("0.95")
        and len(support) >= 4
    ):
        grade, state, cap, rationale = (
            "A",
            "PAPER-FOLLOW",
            Decimal("0.50"),
            "strong cluster-adjusted consensus with broad directional agreement",
        )
    elif (
        strength >= Decimal("0.07")
        and agreement >= Decimal("0.70")
        and coverage >= Decimal("0.90")
        and len(support) >= 3
    ):
        grade, state, cap, rationale = (
            "B",
            "PAPER-FOLLOW",
            Decimal("0.25"),
            "moderate cluster-adjusted consensus with good directional agreement",
        )
    elif (
        strength >= Decimal("0.035")
        and agreement >= Decimal("0.60")
        and coverage >= Decimal("0.85")
        and len(support) >= 2
    ):
        grade, state, cap, rationale = (
            "C",
            "WATCH",
            Decimal("0.10"),
            "weak but coherent cluster consensus; monitor rather than chase",
        )
    else:
        grade, state, cap, rationale = (
            "D",
            "IGNORE",
            Decimal(0),
            "consensus is too small, narrow, or conflicted",
        )

    return AdvisoryAssessment(
        direction=direction,
        grade=grade,
        follow_state=state,
        shadow_risk_cap=cap,
        coverage=coverage,
        agreement=agreement,
        support_clusters=len(support),
        oppose_clusters=len(oppose),
        flat_clusters=flat_clusters,
        rationale=rationale,
    )


def _price_text(price: Decimal | None) -> str:
    if price is None or price <= 0:
        return "n/a"
    if price >= Decimal(100):
        return f"${price:,.2f}"
    if price >= Decimal(1):
        return f"${price:,.4f}"
    return f"${price:,.6f}"


def _format_target(
    target: ConsensusTarget,
    clusters: list[list[str]] | None,
    mark: Decimal | None,
) -> str:
    assessment = _assess_target(target, clusters)
    symbol = target.coin.rsplit(":", 1)[-1]
    emoji = "🟢" if assessment.direction == "LONG" else "🔴" if assessment.direction == "SHORT" else "⚪"
    agreement = (
        f"{assessment.agreement * Decimal(100):.0f}%"
        if assessment.agreement is not None
        else "n/a"
    )
    coverage = f"{assessment.coverage * Decimal(100):.0f}%"
    shadow = (
        f"{assessment.shadow_risk_cap:.2f}R"
        if assessment.shadow_risk_cap > 0
        else "0R"
    )
    return "\n".join(
        [
            f"{emoji} CLUSTER CONSENSUS · {symbol} · {assessment.direction}",
            f"Status: {assessment.follow_state} · Grade {assessment.grade}",
            f"Shadow risk cap: {shadow} · Live size: NOT QUALIFIED",
            "",
            f"Consensus: {target.observed_target:+.3f} · Mark: {_price_text(mark)}",
            f"Coverage: {coverage} · Agreement: {agreement}",
            (
                "Clusters: "
                f"{assessment.support_clusters} support / "
                f"{assessment.oppose_clusters} oppose / "
                f"{assessment.flat_clusters} flat"
            ),
            f"Uncertainty: [{target.lower_bound:+.3f}, {target.upper_bound:+.3f}]",
            f"Why: {assessment.rationale}.",
            "Edge status: prospective predictive edge is not yet qualified (ER4/ER5 pending).",
            "R = your predefined maximum-loss research unit; no dollar notional is valid until a stop/invalidation model exists.",
        ]
    )


def _allmids_key(instrument_id: str) -> str | None:
    """Only map the qualified default-perp namespace; HIP-3 stays unpriced."""
    parts = instrument_id.split(":")
    if len(parts) == 5 and parts[:4] == ["hyperliquid", "mainnet", "perp", "default"]:
        return parts[4]
    return None


class EnsembleRuntime:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.settings.projection_db.parent.mkdir(parents=True, exist_ok=True)
        self.settings.health_path.parent.mkdir(parents=True, exist_ok=True)
        _load_paired_telegram_chat(self.settings)
        self.store = ProjectionStore(settings.projection_db)
        self.clusters = _load_cluster_manifest(
            settings.cluster_manifest_path, settings.experts
        )
        self.mids: dict[str, Decimal] = {}
        self.market_known_at: datetime | None = None
        self.prior_targets: dict[str, ConsensusTarget] = {}
        self.notice_baseline: dict[str, tuple[str, str, Decimal]] = {}
        self.last_notified_at: dict[str, float] = {}
        self.next_telegram_at = 0.0
        self.notifications_bootstrapped = False
        self.last_market_at = 0.0
        self.last_error: str | None = None
        self.telegram_last_ok: bool | None = None
        self.recorder = (
            ResearchRecorder(
                settings.research_db,
                assumed_cost_bps=settings.research_assumed_cost_bps,
            )
            if settings.research_db is not None
            else None
        )
        manifest_bytes = (
            settings.cluster_manifest_path.read_bytes()
            if settings.cluster_manifest_path is not None
            else ",".join(settings.experts).encode("utf-8")
        )
        self.universe_revision = hashlib.sha256(manifest_bytes).hexdigest()
        self.last_research_minute: datetime | None = None
        self.research_stats: dict[str, object] | None = None
        self.notify_started_at = time.monotonic()
        self.notification_times: deque[float] = deque()
        self.last_notification_by_coin: dict[str, float] = {}

    def close(self) -> None:
        if self.recorder is not None:
            self.recorder.close()
        self.store.close()

    def refresh_market(self) -> None:
        self.mids = fetch_all_mids()
        self.market_known_at = datetime.now(UTC)

    def compute_targets(self) -> dict[str, ConsensusTarget]:
        positions = self.store.list_positions()
        instruments = {p.instrument_id for p in positions} | set(self.prior_targets)
        targets: dict[str, ConsensusTarget] = {}
        now = datetime.now(UTC)

        for instrument_id in sorted(instruments):
            postures = []
            sample = next(
                (p for p in positions if p.instrument_id == instrument_id),
                None,
            )
            coin = sample.coin if sample is not None else instrument_id.rsplit(":", 1)[-1]
            for wallet in self.settings.experts:
                pos = self.store.get_position(wallet, instrument_id)
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
                        instrument_id,
                        qty,
                        valuation,
                        equity,
                        equity_age_minutes=equity_age,
                    )
                )
            if self.clusters is None:
                targets[instrument_id] = compute_equal_budget_consensus(
                    instrument_id, postures, as_of=now
                )
            else:
                targets[instrument_id] = compute_hierarchical_cluster_consensus(
                    instrument_id, postures, self.clusters, as_of=now
                )
        return targets

    def capture_research(self, targets: dict[str, ConsensusTarget]) -> None:
        """Freeze one as-known descriptive cut per instrument and minute.

        The immutable ledger cursor and HTTP-mid receipt time are recorded together.
        This is observational V1 consensus, never a qualified ER4 forecast.
        """
        if self.recorder is None:
            return
        known_at = datetime.now(UTC)
        minute = minute_bucket(known_at)
        if self.last_research_minute == minute:
            return

        ledger = _ledger_status(self.settings.ledger_db)
        ledger_id = ledger.get("ledger_id")
        if not isinstance(ledger_id, str):
            return
        seq = self.store.get_offset(ledger_id)
        cuts: list[CutInput] = []
        for instrument_id, target in sorted(targets.items()):
            assessment = _assess_target(target, self.clusters)
            key = _allmids_key(instrument_id)
            mid = self.mids.get(key) if key is not None else None
            direction = (
                1 if target.observed_target > 0
                else -1 if target.observed_target < 0 else 0
            )
            cuts.append(
                CutInput(
                    instrument_id=instrument_id,
                    consensus=target.observed_target,
                    lower_bound=target.lower_bound,
                    upper_bound=target.upper_bound,
                    direction=direction,
                    grade=assessment.grade,
                    coverage=assessment.coverage,
                    supporting_clusters=assessment.support_clusters,
                    opposing_clusters=assessment.oppose_clusters,
                    flat_clusters=assessment.flat_clusters,
                    mid=mid,
                    mid_known_at=self.market_known_at if mid is not None else None,
                )
            )
        self.recorder.capture(
            decision_at=known_at,
            ledger_id=ledger_id,
            ledger_seq=seq,
            universe_revision=self.universe_revision,
            cuts=cuts,
        )
        self.recorder.settle_due(now=known_at)
        self.research_stats = self.recorder.stats()
        self.last_research_minute = minute

    def maybe_notify(self, targets: dict[str, ConsensusTarget]) -> None:
        """Throttle advisory delivery globally and by asset; never block ledger ingestion.

        Startup revisions are deliberately suppressed and NOT replayed as a backlog.
        Messages are informational, never execution instructions. The strongest
        absolute targets get the limited delivery slots first.
        """
        now = time.monotonic()
        if self.settings.notify_max_per_minute <= 0:
            raise ValueError("ENSEMBLE_NOTIFY_MAX_PER_MINUTE must be positive")
        while self.notification_times and now - self.notification_times[0] >= 60.0:
            self.notification_times.popleft()
        warming_up = now - self.notify_started_at < self.settings.notify_warmup_s
        for coin, target in sorted(
            targets.items(), key=lambda item: abs(item[1].observed_target), reverse=True
        ):
            prior = self.prior_targets.get(coin)
            changed = prior is None and self.settings.notify_initial_targets
            if prior is not None:
                changed = (
                    abs(target.observed_target - prior.observed_target)
                    >= self.settings.target_change_threshold
                    or target.is_actionable != prior.is_actionable
                    or target.blocker_code != prior.blocker_code
                )
            if not changed or warming_up:
                continue
            assessment = _assess_target(target, self.clusters)
            if not target.is_actionable or assessment.grade not in ("A", "B"):
                continue
            if (
                self.notification_times
                and now - self.notification_times[-1]
                < self.settings.telegram_min_interval_s
            ):
                break
            if (
                len(self.notification_times) >= self.settings.notify_max_per_minute
                or now - self.last_notification_by_coin.get(coin, -float("inf"))
                < self.settings.notify_per_coin_cooldown_s
            ):
                LOG.info("advisory notification coalesced for %s by rate/cooldown", coin)
                continue
            symbol = target.coin.rsplit(":", 1)[-1]
            delivered = _send_telegram(
                self.settings,
                _format_target(target, self.clusters, self.mids.get(symbol)),
            )
            if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
                self.telegram_last_ok = delivered
            if delivered:
                self.notification_times.append(now)
                self.last_notification_by_coin[coin] = now
            else:
                # Back off even on transport/429 failures; do not flood retries.
                self.notification_times.append(now)
                self.last_notification_by_coin[coin] = now
            LOG.info(
                "consensus %s target=%s actionable=%s delivered=%s",
                coin,
                target.observed_target,
                target.is_actionable,
                delivered,
            )
        # Suppressed/coalesced observations are intentionally not replayed later.
        self.prior_targets = targets

    def telegram_status_text(self, targets: dict[str, ConsensusTarget]) -> str:
        ledger = _ledger_status(self.settings.ledger_db)
        ledger_id = ledger.get("ledger_id")
        consumed = self.store.get_offset(str(ledger_id)) if ledger_id else 0
        ledger_seq = int(ledger.get("current_seq", 0))
        ranked = sorted(
            targets.items(),
            key=lambda item: abs(item[1].observed_target),
            reverse=True,
        )[:8]
        target_lines = []
        for coin, target in ranked:
            assessment = _assess_target(target, self.clusters)
            symbol = coin.rsplit(":", 1)[-1]
            target_lines.append(
                f"{symbol}: {assessment.direction} {target.observed_target:+.3f} · "
                f"{assessment.follow_state} · {assessment.grade}"
            )
        return "\n".join(
            [
                "Advisory runtime: ONLINE",
                f"Experts: {len(self.settings.experts)}",
                f"Clusters: {len(self.clusters) if self.clusters is not None else 'equal-wallet'}",
                f"Open positions: {len(self.store.list_positions())}",
                f"Ledger: {consumed}/{ledger_seq} (lag {max(0, ledger_seq - consumed)})",
                f"Top targets ({len(targets)} total):",
                *(target_lines or ["none"]),
                "Live sizing: NOT QUALIFIED · Execution: FINANCIAL_EFFECT_FORBIDDEN",
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
                "cluster_mode": "hierarchical" if self.clusters is not None else "equal-wallet",
                "clusters": len(self.clusters) if self.clusters is not None else None,
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
            "research": (
                self.research_stats
                if self.research_stats is not None
                else {"status": "NOT_YET_CAPTURED"}
            ),
            "last_error": self.last_error,
        }
        tmp = self.settings.health_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(health, indent=2), encoding="utf-8")
        tmp.replace(self.settings.health_path)

    def step(self) -> None:
        now = time.monotonic()
        if now - self.last_market_at >= self.settings.market_refresh_s:
            self.refresh_market()
            self.last_market_at = now

        while self.store.consume_from_ledger_db(self.settings.ledger_db, batch_size=1000):
            pass

        targets = self.compute_targets()
        self.capture_research(targets)
        self.maybe_notify(targets)
        _poll_telegram_commands(self.settings, self.telegram_status_text(targets))
        self.last_error = None
        self.write_health(targets)

    def run_forever(self) -> None:
        if not self.settings.experts:
            raise RuntimeError("ENSEMBLE_EXPERTS is empty")
        LOG.info(
            "starting advisory runtime with %d experts across %s clusters; financial execution forbidden",
            len(self.settings.experts),
            len(self.clusters) if self.clusters is not None else "equal-wallet",
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
