"""Risk-normalized whole-book shadow follower, with independently priced market control.

All fills are HYPOTHETICAL. One immutable portfolio-level multiplier scales
the full source book; absent prices or liquidity block the *entire* scenario
rather than quietly dropping the missing hedge leg. Never sends orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Protocol, Sequence

from .pm_portfolio import PortfolioDecision, _decimal


ZERO = Decimal("0")
TEN_K = Decimal("10000")
SYMBOL = re.compile(r"^[A-Za-z0-9_.-]+$")


class MarketDataBlocked(Exception):
    """A missing, changed, unsupported, or unverified price source."""


@dataclass(frozen=True)
class MinuteBar:
    timestamp: datetime
    open: Decimal
    quote_volume: Decimal
    source_sha256: str


class MarketSource(Protocol):
    def get(self, instrument: str, action_at: datetime) -> MinuteBar: ...


class VerifiedHyperliquidMinuteSource:
    """Read-only, lazy per-day CryptoHFTData Hyperliquid 1m OHLCV adapter.

    Validates every consumed Parquet day against its source.json SHA.
    Rejects Parquet/sidecar mismatches, including prior HIP-3 index defects;
    never falls back to Binance or unrelated instrument histories.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.cache: dict[tuple[str, str], dict[datetime, MinuteBar]] = {}
        self.source_days: dict[str, str] = {}
        self.liquidity_proxy_days: set[str] = set()

    @staticmethod
    def _family(instrument: str) -> Path:
        parts = instrument.split(":")
        if len(parts) != 5 or parts[:3] != ["hyperliquid", "mainnet", "perp"]:
            raise MarketDataBlocked("UNSUPPORTED_INSTRUMENT_SCOPE")
        dex, symbol = parts[3:]
        if not SYMBOL.fullmatch(symbol) or symbol in {".", ".."}:
            raise MarketDataBlocked("UNSUPPORTED_MARKET_SYMBOL")
        if dex == "default":
            return Path("crypto_futures_ohlcv_1m_HYPERLIQUID_" + symbol + "_PERP")
        if dex == "xyz":
            return Path("crypto_futures_ohlcv_1m_HYPERLIQUID_XYZ_PERP") / ("symbol=" + symbol)
        raise MarketDataBlocked("UNSUPPORTED_HIP3_DEX")

    @staticmethod
    def _minute(dt: datetime) -> datetime:
        if dt.utcoffset() is None:
            raise ValueError("unaware fill clock")
        dt = dt.astimezone(UTC)
        minute = dt.replace(second=0, microsecond=0)
        return minute if dt == minute else minute + timedelta(minutes=1)

    def get(self, instrument: str, action_at: datetime) -> MinuteBar:
        minute = self._minute(action_at)
        relative = self._family(instrument)
        day = minute.date().isoformat()
        key = (instrument, day)
        if key not in self.cache:
            self._load_day(instrument, day, relative, minute)
        bar = self.cache[key].get(minute)
        if bar is None:
            raise MarketDataBlocked("MISSING_EXACT_1M_BAR")
        if bar.open <= ZERO:
            raise MarketDataBlocked("INVALID_MARKET_OPEN")
        return bar

    def _load_day(self, instrument: str, day: str, family: Path, minute: datetime) -> None:
        directory = (
            self.root
            / family
            / (f"year={minute.year:04d}/month={minute.month:02d}/day={minute.day:02d}")
        )
        path = directory / "data.parquet"
        meta = directory / "source.json"
        if not path.is_file() or not meta.is_file():
            raise MarketDataBlocked("MISSING_MARKET_DAY")
        try:
            source = json.loads(meta.read_text(encoding="utf-8"))
            if source.get("exchange") not in {"hyperliquid_futures", "hyperliquid", "HYPERLIQUID"}:
                raise MarketDataBlocked("WRONG_MARKET_VENUE")
            dex, expected_symbol = instrument.split(":")[3:5]
            if dex == "xyz":
                expected_symbol = "xyz:" + expected_symbol
            if source.get("source_symbol") != expected_symbol:
                raise MarketDataBlocked("WRONG_MARKET_SYMBOL")
            expected = source.get("sha256")
            if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
                raise MarketDataBlocked("MARKET_SHA_MISSING")
            actual = sha256(path.read_bytes()).hexdigest()
            if expected != actual:
                raise MarketDataBlocked("MARKET_PARQUET_HASH_MISMATCH")
            import polars as pl

            columns = pl.scan_parquet(path).collect_schema()
            required = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}
            if not required.issubset(columns):
                raise MarketDataBlocked("MISSING_NATIVE_1M_OHLCV_COLUMNS")
            if source.get("source_type") not in {"trades", "candles"}:
                raise MarketDataBlocked("UNVERIFIED_NATIVE_1M_SOURCE_TYPE")
            has_quote = "quote_volume" in columns
            if not has_quote:
                # No order-book liquidity is inferred from market bar volume.
                self.liquidity_proxy_days.add(instrument + "@" + day)
            keys = sorted(required | ({"quote_volume"} if has_quote else set()))
            frame = pl.read_parquet(path, columns=keys)
            rows: dict[datetime, MinuteBar] = {}
            for row in frame.iter_rows(named=True):
                ts = row["timestamp"]
                if (
                    ts.tzinfo is None
                    or ts.date().isoformat() != day
                    or ts.second != 0
                    or ts.microsecond != 0
                    or ts in rows
                ):
                    raise MarketDataBlocked("DUPLICATE_OR_MALFORMED_1M_BARS")
                source_name = expected_symbol.split(":")[-1]
                if row["symbol"] != source_name:
                    raise MarketDataBlocked("WRONG_NATIVE_1M_ROW_SYMBOL")
                open_px = _decimal(row["open"], "market open")
                high = _decimal(row["high"], "market high")
                low = _decimal(row["low"], "market low")
                close = _decimal(row["close"], "market close")
                base_volume = _decimal(row["volume"], "base volume")
                if (
                    low <= ZERO
                    or low > min(open_px, close)
                    or high < max(open_px, close)
                    or base_volume < ZERO
                ):
                    raise MarketDataBlocked("INVALID_NATIVE_1M_OHLCV")
                quote_volume = (
                    _decimal(row["quote_volume"], "quote volume")
                    if has_quote
                    else base_volume * open_px
                )
                if quote_volume < ZERO:
                    raise MarketDataBlocked("INVALID_NATIVE_1M_VOLUME")
                bar = MinuteBar(ts.astimezone(UTC), open_px, quote_volume, expected)
                rows[bar.timestamp] = bar
            if not rows:
                raise MarketDataBlocked("EMPTY_MARKET_DAY")
        except MarketDataBlocked:
            raise
        except Exception as exc:
            # A malformed Parquet schema is a SOURCE BLOCKER, not permission
            # to drop a missing book leg or interpolate a different market.
            if not (
                isinstance(exc, (ValueError, OSError, KeyError, TypeError, ImportError))
                or type(exc).__module__.startswith("polars.")
            ):
                raise
            raise MarketDataBlocked("INVALID_MARKET_DAY:" + type(exc).__name__) from exc
        self.cache[(instrument, day)] = rows
        self.source_days[instrument + "@" + day] = expected


@dataclass(frozen=True)
class Scenario:
    name: str
    latency_minutes: int
    initial_cash: Decimal = Decimal("100000")
    portfolio_multiplier: Decimal = Decimal("0.05")
    max_gross_leverage: Decimal = Decimal("1")
    taker_fee_bps: Decimal = Decimal("5")
    spread_bps: Decimal = Decimal("3")
    slippage_bps: Decimal = Decimal("5")
    funding_abs_bps_per_8h: Decimal = Decimal("1")
    max_minute_participation: Decimal = Decimal("0.05")

    def __post_init__(self) -> None:
        if self.latency_minutes not in (5, 15, 60):
            raise ValueError("scenario latency must be 5/15/60 minutes")
        if self.initial_cash <= ZERO or self.portfolio_multiplier < ZERO:
            raise ValueError("invalid follower account/risk")
        if self.max_gross_leverage <= ZERO:
            raise ValueError("invalid leverage ceiling")
        if not ZERO < self.max_minute_participation <= Decimal("1"):
            raise ValueError("invalid volume participation")
        for value in (
            self.taker_fee_bps,
            self.spread_bps,
            self.slippage_bps,
            self.funding_abs_bps_per_8h,
        ):
            if value < ZERO or not value.is_finite():
                raise ValueError("invalid cost assumption")


@dataclass
class _VirtualBook:
    positions: dict[str, Decimal] = field(default_factory=dict)
    flows: dict[str, Decimal] = field(default_factory=dict)
    fees: Decimal = ZERO
    impact: Decimal = ZERO
    funding: Decimal = ZERO
    turnover: Decimal = ZERO
    fills: int = 0

    def accrue_funding(
        self, prices: dict[str, Decimal], elapsed: timedelta, scenario: Scenario
    ) -> None:
        if elapsed.total_seconds() <= 0:
            return
        rate = (
            scenario.funding_abs_bps_per_8h
            * (Decimal(str(elapsed.total_seconds())) / Decimal("28800"))
            / TEN_K
        )
        for instrument, qty in self.positions.items():
            if qty == ZERO:
                continue
            cost = abs(qty * prices[instrument]) * rate
            self.flows[instrument] = self.flows.get(instrument, ZERO) - cost
            self.funding += cost

    def rebalance(
        self,
        target: dict[str, Decimal],
        quotes: dict[str, MinuteBar],
        scenario: Scenario,
    ) -> None:
        for instrument in sorted(set(self.positions) | set(target)):
            current = self.positions.get(instrument, ZERO)
            wanted = target.get(instrument, ZERO)
            delta = wanted - current
            if delta == ZERO:
                continue
            bar = quotes[instrument]
            notional = abs(delta * bar.open)
            if notional > bar.quote_volume * scenario.max_minute_participation:
                raise MarketDataBlocked("MINUTE_PARTICIPATION_EXCEEDED")
            if bar.quote_volume <= ZERO:
                raise MarketDataBlocked("NO_EXECUTABLE_1M_VOLUME")
            side = Decimal("1") if delta > ZERO else Decimal("-1")
            impact_bps = scenario.spread_bps / Decimal("2") + scenario.slippage_bps
            execution_price = bar.open * (Decimal("1") + side * impact_bps / TEN_K)
            fee = abs(delta * execution_price) * scenario.taker_fee_bps / TEN_K
            self.flows[instrument] = (
                self.flows.get(instrument, ZERO) - delta * execution_price - fee
            )
            if wanted == ZERO:
                self.positions.pop(instrument, None)
            else:
                self.positions[instrument] = wanted
            self.turnover += notional
            self.fees += fee
            self.impact += abs(delta * (execution_price - bar.open))
            self.fills += 1

    def pnl_by_instrument(self, prices: dict[str, Decimal]) -> dict[str, Decimal]:
        return {
            instrument: (
                flow + self.positions[instrument] * prices[instrument]
                if instrument in self.positions and self.positions[instrument] != ZERO
                else flow
            )
            for instrument, flow in sorted(self.flows.items())
        }


@dataclass(frozen=True)
class ScenarioResult:
    wallet_id: str
    name: str
    status: str
    reason: str
    decision_count: int
    hypothetical_fill_count: int
    last_hypothetical_fill_at: datetime | None
    follower_pnl: Decimal | None
    market_only_pnl: Decimal | None
    per_instrument_pnl: dict[str, Decimal]
    turnover: Decimal
    fees: Decimal
    spread_slippage: Decimal
    funding_assumed: Decimal
    open_positions: int
    accounting_reconciled: bool
    source_account_pnl: None = None
    actual_broker_pnl: None = None


def replay_shadow(
    decisions: Sequence[PortfolioDecision],
    prices: MarketSource,
    scenario: Scenario,
) -> ScenarioResult:
    """Model a whole-wallet follower and same-market always-long control.

    A single uncovered, missing or stale portfolio state or unsupported market
    blocks the entire scenario. Output never masquerades as genuine forward
    execution, actual source P&L, or qualified net funding economics.
    """
    if not decisions:
        raise ValueError("at least one decision cut required")
    wallet_id = decisions[0].wallet_id
    follower = _VirtualBook()
    control = _VirtualBook()
    last_time: datetime | None = None
    last_prices: dict[str, Decimal] = {}
    fills_at: datetime | None = None

    def blocked(reason: str, index: int) -> ScenarioResult:
        return ScenarioResult(
            wallet_id,
            scenario.name,
            "BLOCKED",
            reason,
            index,
            0,
            None,
            None,
            None,
            {},
            ZERO,
            ZERO,
            ZERO,
            ZERO,
            0,
            False,
        )

    for i, decision in enumerate(decisions):
        if decision.wallet_id != wallet_id:
            raise ValueError("mixed wallet decisions")
        if i and decision.decided_at <= decisions[i - 1].decided_at:
            raise ValueError("decision times not strictly increasing")
        if decision.coverage_gap_seen:
            return blocked("UNRECONCILED_SOURCE_COVERAGE_GAP", i)
        if decision.book_known and decision.history_partial:
            return blocked("UNVERIFIED_PARTIAL_SOURCE_ACCOUNT_HISTORY", i)
        if not decision.book_known or decision.source_equity is None:
            return blocked("INCOMPLETE_WHOLE_BOOK:" + decision.reason, i)
        if decision.source_equity <= ZERO:
            return blocked("UNAVAILABLE_SOURCE_EQUITY", i)
        if decision.first_known_at is None or decision.first_known_at > decision.decided_at:
            return blocked("MISSING_FIRST_KNOWN_CLOCK", i)
        action_at = decision.decided_at + timedelta(minutes=scenario.latency_minutes)
        try:
            instruments = sorted(
                set(decision.source_positions) | set(follower.positions) | set(control.positions)
            )
            quotes = {name: prices.get(name, action_at) for name in instruments}
            marks = {name: q.open for name, q in quotes.items()}
            gross_source = sum(
                (abs(qty * marks[name]) for name, qty in decision.source_positions.items()), ZERO
            )
            gross_scaled = gross_source * scenario.portfolio_multiplier / decision.source_equity
            if gross_scaled > scenario.max_gross_leverage:
                return blocked("PORTFOLIO_GROSS_RISK_EXCEEDED", i)
            scale = scenario.initial_cash * scenario.portfolio_multiplier / decision.source_equity
            target = {name: qty * scale for name, qty in decision.source_positions.items()}
            always_long = {name: abs(qty) for name, qty in target.items()}
            if last_time is not None:
                elapsed = VerifiedHyperliquidMinuteSource._minute(action_at) - last_time
                if elapsed < timedelta():
                    raise ValueError("nonmonotonic hypothetical execution clock")
                follower.accrue_funding(marks, elapsed, scenario)
                control.accrue_funding(marks, elapsed, scenario)
            # Check both books' whole-book liquidity BEFORE any virtual fills.
            for book, desired in ((follower, target), (control, always_long)):
                for name in instruments:
                    delta = desired.get(name, ZERO) - book.positions.get(name, ZERO)
                    if delta != ZERO and (
                        quotes[name].quote_volume <= ZERO
                        or abs(delta * marks[name])
                        > quotes[name].quote_volume * scenario.max_minute_participation
                    ):
                        raise MarketDataBlocked("MINUTE_PARTICIPATION_EXCEEDED")
            previous_fills = follower.fills
            follower.rebalance(target, quotes, scenario)
            control.rebalance(always_long, quotes, scenario)
            last_time = VerifiedHyperliquidMinuteSource._minute(action_at)
            last_prices = marks
            if follower.fills > previous_fills:
                fills_at = last_time
        except MarketDataBlocked as exc:
            return blocked(str(exc), i)
    attribution = follower.pnl_by_instrument(last_prices)
    control_attribution = control.pnl_by_instrument(last_prices)
    # Independently reconcile per-leg cash flows to total cash and open MTM.
    attributed_pnl = sum(attribution.values(), ZERO)
    marked_positions = sum(
        (qty * last_prices[name] for name, qty in follower.positions.items()), ZERO
    )
    cash_balance = scenario.initial_cash + sum(follower.flows.values(), ZERO)
    pnl = cash_balance + marked_positions - scenario.initial_cash
    market_pnl = sum(control_attribution.values(), ZERO)
    reconciled = pnl == attributed_pnl
    return ScenarioResult(
        wallet_id,
        scenario.name,
        "AS_KNOWN_RETROSPECTIVE_HYPOTHETICAL",
        "NO_FORWARD_POLICY_FREEZE_OR_ACTUAL_EXECUTION",
        len(decisions),
        follower.fills,
        fills_at,
        pnl,
        market_pnl,
        attribution,
        follower.turnover,
        follower.fees,
        follower.impact,
        follower.funding,
        sum(qty != ZERO for qty in follower.positions.values()),
        reconciled,
    )
