"""PM-01: fail-closed, read-only reconstruction of RUST-AUTHORED account facts.

This module does not reconstruct exchange positions from HyperX fills. It
projects authoritative ACCOUNT_SNAPSHOT and POSITION_CHANGE observations into
a hypothetical follower target. Source event, receipt, knowledge, decision and
hypothetical execution clocks are separate. No broker interface is imported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from math import isfinite
from typing import Iterable, Mapping
import re


INSTRUMENT = re.compile(r"^hyperliquid:mainnet:perp:[a-zA-Z0-9_-]+:[^:]+$")
ZERO = Decimal("0")


class Coverage(str, Enum):
    UNKNOWN = "UNKNOWN"
    HISTORICAL_ONLY = "HISTORICAL_ONLY"
    PROSPECTIVE_VERIFIED = "PROSPECTIVE_VERIFIED"


@dataclass(frozen=True)
class WalletSource:
    wallet_id: str
    cohort: str
    coverage: Coverage
    first_known_at: str | None = None

    def __post_init__(self) -> None:
        if not self.wallet_id.strip():
            raise ValueError("wallet_id required")
        if self.cohort not in {"original", "wave1", "wave2"}:
            raise ValueError("invalid cohort")
        if self.coverage is Coverage.PROSPECTIVE_VERIFIED:
            if not self.first_known_at:
                raise ValueError("prospective coverage requires first-known timestamp")
            _aware(datetime.fromisoformat(self.first_known_at.replace("Z", "+00:00")))

    @property
    def replay_eligible(self) -> bool:
        return self.coverage is Coverage.PROSPECTIVE_VERIFIED


def validate_36_wallet_cohort(wallets: Iterable[WalletSource]) -> tuple[WalletSource, ...]:
    entries = tuple(wallets)
    counts = {"original": 0, "wave1": 0, "wave2": 0}
    seen: set[str] = set()
    for wallet in entries:
        key = wallet.wallet_id.casefold()
        if key in seen:
            raise ValueError(f"duplicate wallet: {wallet.wallet_id}")
        seen.add(key)
        counts[wallet.cohort] += 1
    if counts != {"original": 6, "wave1": 10, "wave2": 20}:
        raise ValueError(f"incomplete PM cohort: {counts}")
    return entries


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return ts


def _decimal(value: object, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}") from exc
    if not number.is_finite():
        raise ValueError(f"non-finite {label}")
    return number


def _instrument(value: str) -> str:
    if not INSTRUMENT.fullmatch(value):
        raise ValueError("venue-aware instrument required")
    return value


@dataclass(frozen=True)
class KnownPosition:
    """Legacy fixture seam. New production research uses atomic AccountEvent snapshots."""

    wallet_id: str
    instrument: str
    quantity: float
    known_at: datetime
    event_at: datetime
    coverage: Coverage
    source_seq: int
    snapshot_complete: bool = False

    def __post_init__(self) -> None:
        if not self.wallet_id or not self.instrument:
            raise ValueError("wallet and venue-aware instrument required")
        _instrument(self.instrument)
        if not isfinite(self.quantity):
            raise ValueError("finite quantity required")
        _aware(self.known_at)
        _aware(self.event_at)
        if self.known_at < self.event_at:
            raise ValueError("first-known cannot precede source event")
        if self.source_seq < 0:
            raise ValueError("invalid source sequence")


@dataclass(frozen=True)
class PortfolioDecision:
    wallet_id: str
    decided_at: datetime
    targets: Mapping[str, float]
    eligible: bool
    reason: str
    source_positions: Mapping[str, Decimal] = field(default_factory=dict)
    source_equity: Decimal | None = None
    history_partial: bool = True
    book_known: bool = False
    source_cursor: int = 0
    first_known_at: datetime | None = None
    coverage_gap_seen: bool = False


def reconstruct_asof(
    wallet: WalletSource,
    observations: Iterable[KnownPosition],
    decided_at: datetime,
    risk_multiplier: float = 1.0,
) -> PortfolioDecision:
    """Compatibility seam; each row explicitly declares its own completeness.

    A position-row sequence is NOT a per-wallet exchange cursor. Globally
    allocated Rust cursor gaps are expected when filtering to one wallet.
    """
    _aware(decided_at)
    if not isfinite(risk_multiplier) or risk_multiplier < 0:
        raise ValueError("invalid risk multiplier")
    if not wallet.replay_eligible:
        return PortfolioDecision(wallet.wallet_id, decided_at, {}, False, "WALLET_NOT_PROSPECTIVE")
    rows = sorted(
        (
            x
            for x in observations
            if x.wallet_id.casefold() == wallet.wallet_id.casefold() and x.known_at <= decided_at
        ),
        key=lambda x: (x.known_at, x.source_seq),
    )
    if not rows:
        return PortfolioDecision(wallet.wallet_id, decided_at, {}, False, "NO_KNOWN_POSITIONS")
    if not all(x.snapshot_complete for x in rows):
        return PortfolioDecision(
            wallet.wallet_id, decided_at, {}, False, "INCOMPLETE_PORTFOLIO_SNAPSHOT"
        )
    if any(x.coverage is not Coverage.PROSPECTIVE_VERIFIED for x in rows):
        return PortfolioDecision(wallet.wallet_id, decided_at, {}, False, "COVERAGE_GAP")
    seen: dict[int, KnownPosition] = {}
    for x in rows:
        if x.source_seq in seen:
            return PortfolioDecision(
                wallet.wallet_id, decided_at, {}, False, "DUPLICATE_SOURCE_SEQUENCE"
            )
        seen[x.source_seq] = x
    targets = {row.instrument: row.quantity * risk_multiplier for row in rows}
    return PortfolioDecision(wallet.wallet_id, decided_at, targets, True, "OBSERVED_ONLY")


@dataclass(frozen=True)
class AccountEvent:
    """A single durable Rust outbox fact; snapshots are atomic whole-book arrays."""

    kind: str
    wallet_id: str
    ledger_id: str
    seq: int  # globally allocated; NOT per-wallet contiguous
    event_at: datetime
    received_at: datetime
    known_at: datetime
    quality: str = "valid"
    positions: Mapping[str, Decimal] | None = None
    equity: Decimal | None = None
    instrument: str | None = None
    quantity_after: Decimal | None = None
    delta: Decimal | None = None
    before_revision: int | None = None
    after_revision: int | None = None
    coverage: str = "UNKNOWN"
    partial_history: bool = True
    gap_status: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"ACCOUNT_SNAPSHOT", "POSITION_CHANGE", "COVERAGE_CHANGE"}:
            raise ValueError("unsupported source kind")
        if self.seq <= 0 or not self.ledger_id:
            raise ValueError("durable cursor required")
        _aware(self.event_at)
        _aware(self.received_at)
        _aware(self.known_at)
        if not self.event_at <= self.received_at <= self.known_at:
            raise ValueError("invalid source/receipt/knowledge clocks")
        if self.kind == "ACCOUNT_SNAPSHOT":
            if self.positions is None:
                raise ValueError("snapshot positions must be explicit (empty is valid)")
            if self.equity is None:
                raise ValueError("snapshot equity is required")
            _decimal(self.equity, "account equity")
            for key, value in self.positions.items():
                _instrument(key)
                _decimal(value, "source quantity")
        if self.kind == "POSITION_CHANGE":
            if self.instrument is None or self.quantity_after is None or self.delta is None:
                raise ValueError("source position change requires instrument/after/delta")
            _instrument(self.instrument)
            _decimal(self.quantity_after, "position after")
            _decimal(self.delta, "source delta")


def classify_transition(before: Decimal, after: Decimal) -> str:
    if after == ZERO:
        return "CLOSE" if before != ZERO else "UNCHANGED"
    if before == ZERO:
        return "OPEN"
    if (before > ZERO) != (after > ZERO):
        return "FLIP"
    return (
        "ADD"
        if abs(after) > abs(before)
        else ("REDUCE" if abs(after) < abs(before) else "UNCHANGED")
    )


@dataclass
class _Book:
    positions: dict[str, Decimal] | None = None
    equity: Decimal | None = None
    last_snapshot_at: datetime | None = None
    last_known_at: datetime | None = None
    last_seq: int = 0
    ledger_id: str | None = None
    seen: dict[int, AccountEvent] = field(default_factory=dict)
    history_partial: bool = False
    gap_seen: bool = False
    reason: str = "NO_COMPLETE_SNAPSHOT"
    first_known_at: datetime | None = None


def _apply(book: _Book, event: AccountEvent) -> None:
    if book.ledger_id is not None and event.ledger_id != book.ledger_id:
        book.positions = None
        book.reason = "LEDGER_CHANGED"
        book.gap_seen = True
        return
    book.ledger_id = event.ledger_id
    earlier = book.seen.get(event.seq)
    if earlier is not None:
        if earlier != event:
            book.positions = None
            book.reason = "CONFLICTING_DUPLICATE_CURSOR"
            book.gap_seen = True
        return
    if event.seq <= book.last_seq or (book.last_known_at and event.known_at < book.last_known_at):
        book.positions = None
        book.reason = "OUT_OF_ORDER_SOURCE"
        book.gap_seen = True
        return
    book.seen[event.seq] = event
    book.last_seq = event.seq
    book.last_known_at = event.known_at
    if book.first_known_at is None:
        book.first_known_at = event.known_at
    if event.kind == "COVERAGE_CHANGE":
        book.positions = None
        book.gap_seen = True
        book.reason = "SOURCE_COVERAGE_GAP"
        return
    if event.quality != "valid":
        book.positions = None
        book.gap_seen = True
        book.reason = "UNVERIFIED_SOURCE_QUALITY"
        return
    if event.coverage.casefold() in {"degraded", "unknown", "unseeded", "invalid"}:
        book.positions = None
        book.gap_seen = True
        book.reason = "UNVERIFIED_WALLET_POSITION_COVERAGE"
        return
    if event.kind == "ACCOUNT_SNAPSHOT":
        assert event.positions is not None
        book.positions = {k: v for k, v in event.positions.items() if v != ZERO}
        book.equity = event.equity
        book.last_snapshot_at = event.known_at
        book.history_partial = book.history_partial or event.partial_history or book.gap_seen
        book.reason = "OBSERVED_PARTIAL_HISTORY" if book.history_partial else "SOURCE_STATE_SEEDED"
        return
    if book.positions is None:
        book.reason = "UNSEEDED_POSITION_CHANGE"
        return
    assert event.instrument is not None and event.quantity_after is not None
    assert event.delta is not None
    before = book.positions.get(event.instrument, ZERO)
    if abs(before + event.delta - event.quantity_after) > Decimal("0.00000001"):
        book.positions = None
        book.gap_seen = True
        book.reason = "RUST_DELTA_STATE_CONFLICT"
        return
    if event.quantity_after == ZERO:
        book.positions.pop(event.instrument, None)
    else:
        book.positions[event.instrument] = event.quantity_after
    book.reason = "OBSERVED_PARTIAL_HISTORY" if book.history_partial else "SOURCE_STATE_SEEDED"


def _finalize(
    wallet: WalletSource,
    book: _Book,
    decided_at: datetime,
    multiplier: Decimal,
    max_snapshot_age: timedelta,
) -> PortfolioDecision:
    if book.positions is None:
        return PortfolioDecision(
            wallet.wallet_id,
            decided_at,
            {},
            False,
            book.reason,
            history_partial=True,
            source_cursor=book.last_seq,
            first_known_at=book.first_known_at,
        )
    if book.last_snapshot_at is None or decided_at - book.last_snapshot_at > max_snapshot_age:
        return PortfolioDecision(
            wallet.wallet_id,
            decided_at,
            {},
            False,
            "STALE_ACCOUNT_SNAPSHOT",
            history_partial=True,
            source_cursor=book.last_seq,
            first_known_at=book.first_known_at,
        )
    if book.equity is None or book.equity <= ZERO:
        return PortfolioDecision(
            wallet.wallet_id,
            decided_at,
            {},
            False,
            "NO_POSITIVE_ACCOUNT_EQUITY",
            source_positions=dict(book.positions),
            book_known=True,
            source_cursor=book.last_seq,
            first_known_at=book.first_known_at,
        )
    targets = {
        instrument: float(qty * multiplier) for instrument, qty in sorted(book.positions.items())
    }
    eligible = wallet.replay_eligible and not book.history_partial and not book.gap_seen
    return PortfolioDecision(
        wallet.wallet_id,
        decided_at,
        targets,
        eligible,
        "OBSERVED_ONLY" if eligible else book.reason,
        source_positions=dict(sorted(book.positions.items())),
        source_equity=book.equity,
        history_partial=book.history_partial,
        book_known=True,
        source_cursor=book.last_seq,
        first_known_at=book.first_known_at,
        coverage_gap_seen=book.gap_seen,
    )


def reconstruct_events(
    wallet: WalletSource,
    observations: Iterable[AccountEvent],
    decided_at: datetime,
    *,
    risk_multiplier: Decimal = Decimal("1"),
    max_snapshot_age: timedelta = timedelta(minutes=5),
) -> PortfolioDecision:
    """Use only first-known observations; global ledger seq gaps are expected."""
    _aware(decided_at)
    multiplier = _decimal(risk_multiplier, "risk multiplier")
    if multiplier < ZERO:
        raise ValueError("negative portfolio multiplier")
    book = _Book()
    relevant = sorted(
        (
            e
            for e in observations
            if (
                e.wallet_id.casefold() == wallet.wallet_id.casefold() or e.kind == "COVERAGE_CHANGE"
            )
            and e.known_at <= decided_at
        ),
        key=lambda e: (e.seq, e.known_at),
    )
    for event in relevant:
        _apply(book, event)
    return _finalize(wallet, book, decided_at, multiplier, max_snapshot_age)


def reconstruct_timeline(
    wallet: WalletSource,
    observations: Iterable[AccountEvent],
    cuts: Iterable[datetime],
    *,
    risk_multiplier: Decimal = Decimal("1"),
    max_snapshot_age: timedelta = timedelta(minutes=5),
) -> tuple[PortfolioDecision, ...]:
    """One linear pass across deterministic as-known research decision cuts.

    This uses the Rust outbox's global sequence. It never requires contiguous
    per-wallet seq numbers and does not interpolate a missing source state.
    """
    decision_times = tuple(cuts)
    if any(_aware(t) != t for t in decision_times):
        raise ValueError("invalid decision cut")
    if any(b <= a for a, b in zip(decision_times, decision_times[1:])):
        raise ValueError("decision cuts must increase strictly")
    multiplier = _decimal(risk_multiplier, "risk multiplier")
    if multiplier < ZERO:
        raise ValueError("negative risk multiplier")
    relevant = sorted(
        (
            e
            for e in observations
            if e.wallet_id.casefold() == wallet.wallet_id.casefold() or e.kind == "COVERAGE_CHANGE"
        ),
        key=lambda e: (e.known_at, e.seq),
    )
    book = _Book()
    cursor = 0
    decisions: list[PortfolioDecision] = []
    for cut in decision_times:
        while cursor < len(relevant) and relevant[cursor].known_at <= cut:
            _apply(book, relevant[cursor])
            cursor += 1
        decisions.append(_finalize(wallet, book, cut, multiplier, max_snapshot_age))
    return tuple(decisions)
