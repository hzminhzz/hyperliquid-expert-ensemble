"""Rich Telegram presentation contract: HTML safety and no alpha/risk promotion."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

from ensemble.copy_board import render_flow_digest
from ensemble.trader_grade_adapter import grade_appendix


class TelegramHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        assert tag in {"b", "i", "code"}, tag
        assert attrs == []
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self.stack.pop() == tag


def example_board() -> dict:
    now = datetime(2026, 11, 4, 7, tzinfo=UTC)
    rows = []
    for ticker, side, longs, shorts in (
        ("BTC<&>", "LONG_WATCH", 4, 1),
        ("SOL", "SHORT_WATCH", 0, 5),
        ("NEAR", "CAUTION", 3, 0),
    ):
        rows.append(
            {
                "coin": ticker,
                "observed_directional_bias": side,
                "underlying_lean": "LONG_WATCH",
                "long_entry_wallets": longs,
                "short_entry_wallets": shorts,
                "de_risk_wallets": 2,
                "exit_wallets": 1,
            }
        )
    return {
        "status": "DESCRIPTIVE_ONLY_INDEPENDENCE_NOT_QUALIFIED",
        "as_of": now.isoformat(),
        "window_start": (now - timedelta(hours=4)).isoformat(),
        "truncated": False,
        "coins": rows,
        "fresh_entry_events": 8,
        "de_risk_events": 3,
    }


def test_styled_hourly_digest_has_headings_arrows_and_safe_escaped_ticker() -> None:
    text = render_flow_digest(example_board())
    assert "📡 <b>HYPERLIQUID | HOURLY DIRECTIONAL WATCH</b>" in text
    assert "🟢 <b>LONG WATCH" in text
    assert "🔴 <b>SHORT WATCH" in text
    assert "🟡 <b>CAUTION / MIXED" in text
    assert "↗ <b>BTC&lt;&amp;&gt;</b>" in text
    assert "↘ <b>SOL</b>" in text
    assert "NOT a copy/entry signal" in text
    assert "Predictive edge: NOT QUALIFIED" in text
    assert "Entry price/size: NOT AVAILABLE" in text
    assert len(text) < 3800
    p = TelegramHTML()
    p.feed(text)
    assert p.stack == []


def test_grade_badges_represent_c_x_only_without_risk_promotion() -> None:
    source = {
        ("BTC", "LONG"): {
            "grade": "C",
            "research_r": "0",
            "claim_status": "RESEARCH_ONLY",
            "independent_clusters": None,
            "reasons": ["not independently validated"],
        },
        ("SOL", "SHORT"): {
            "grade": "X",
            "research_r": "0",
            "claim_status": "REJECTED",
            "independent_clusters": None,
            "reasons": ["bad source <unsafe>"],
        },
    }
    rendered = grade_appendix(source)
    assert "⚪ <b>BTC ↗ LONG · WATCH</b>" in rendered
    assert "🔴 <b>SOL ↘ SHORT · REJECT</b>" in rendered
    assert "0R research max" in rendered
    assert "&lt;unsafe&gt;" in rendered
    assert "A/B not currently qualified" in rendered
    assert "Not an order" in rendered
    p = TelegramHTML()
    p.feed(rendered)
    assert p.stack == []


def test_missing_source_has_no_fake_long_or_short() -> None:
    assert "unavailable" in render_flow_digest({"status": "UNAVAILABLE"}).lower()
    assert "↗ LONG" not in render_flow_digest({"status": "UNAVAILABLE"})
