"""Telegram notification adapter and meaningful-change filter (Contracts R6, Q08, Q12).

Formats bounded account advice into readable HTML cards, filtering noise and micro-fluctuations.
"""

from __future__ import annotations

from decimal import Decimal

from .adviser import AccountAdvice, AdviceStatus


def should_notify_advice(
    new_advice: AccountAdvice,
    prior_advice: AccountAdvice | None = None,
    meaningful_change_pct: Decimal = Decimal("0.05"),
) -> bool:
    """Check if advice constitutes a meaningful actionable change (R5, Q08).

    Rules:
      1. First advice always notifies.
      2. Status transitions (e.g. SIZED -> BLOCKED) always notify.
      3. Position flips or full closes always notify.
      4. Small size adjustments (< meaningful_change_pct) are filtered to prevent spam.
    """
    if prior_advice is None:
        return True

    if new_advice.status != prior_advice.status:
        return True

    # If blocked, notify only if blocker code changed
    if new_advice.status == AdviceStatus.BLOCKED:
        return new_advice.blocker_code != prior_advice.blocker_code

    new_qty = new_advice.target_quantity or Decimal("0.0")
    prior_qty = prior_advice.target_quantity or Decimal("0.0")

    # Closes or flips
    if (new_qty == Decimal("0.0") and prior_qty != Decimal("0.0")) or (
        (new_qty > Decimal("0.0") and prior_qty < Decimal("0.0"))
        or (new_qty < Decimal("0.0") and prior_qty > Decimal("0.0"))
    ):
        return True

    # Size change percentage relative to prior
    if prior_qty != Decimal("0.0"):
        change_pct = abs(new_qty - prior_qty) / abs(prior_qty)
        return change_pct >= meaningful_change_pct

    return new_qty != Decimal("0.0")


def format_advice_html(advice: AccountAdvice) -> str:
    """Render account advice as an HTML Telegram notification card (Q12)."""
    status_emoji = {
        AdviceStatus.SIZED: "🎯",
        AdviceStatus.UNSIZED: "ℹ️",
        AdviceStatus.BLOCKED: "🛑",
    }.get(advice.status, "📌")

    lines = [
        f"{status_emoji} <b>Account Advice: {advice.coin}</b> (<code>{advice.account_id}</code>)",
        f"Status: <b>{advice.status.value}</b>",
        f"Consensus Posture: <code>{advice.target_posture:+.2f}</code>",
    ]

    if advice.status == AdviceStatus.SIZED:
        lines.append(f"Target Quantity: <b>{advice.target_quantity} {advice.coin}</b>")
        if advice.action_delta is not None:
            lines.append(f"Action Delta: <code>{advice.action_delta:+}</code>")
        else:
            lines.append("Action Delta: <i>[Account position unknown; show target only]</i>")

        if advice.allowable_risk_usd is not None:
            lines.append(f"Allocated Risk: <code>${advice.allowable_risk_usd:.2f}</code>")
    elif advice.status == AdviceStatus.UNSIZED:
        lines.append("<i>Informational only: missing stop loss model or broker spec.</i>")
    elif advice.status == AdviceStatus.BLOCKED:
        lines.append(f"Blocker: <code>{advice.blocker_code}</code>")

    lines.append(f"Expires: <code>{advice.expires_at.strftime('%H:%M:%S UTC')}</code>")
    return "\n".join(lines)
