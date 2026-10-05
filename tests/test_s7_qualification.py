"""S7 Qualification Suite: Q12 Account Constraints and Q08 Notification Adapter."""

from datetime import UTC, datetime
from decimal import Decimal

from ensemble.adviser import (
    AccountRulebook,
    AccountState,
    AdviceStatus,
    InstrumentSpec,
    LossModel,
    generate_account_advice,
)
from ensemble.consensus import CausalChangeCategory, ConsensusTarget
from ensemble.notifier_adapter import format_advice_html, should_notify_advice


def _mock_target(observed: str = "1.0", is_actionable: bool = True) -> ConsensusTarget:
    return ConsensusTarget(
        coin="BTC",
        observed_target=Decimal(observed),
        missing_mass=Decimal("0.0"),
        lower_bound=Decimal(observed),
        upper_bound=Decimal(observed),
        cause=CausalChangeCategory.ECONOMIC_CHANGE,
        contributions=[],
        is_actionable=is_actionable,
        blocker_code=None if is_actionable else "AMBIGUOUS_BOUNDS",
        as_of=datetime.now(UTC),
    )


def test_q12_account_drawdown_headroom_and_trade_ceilings():
    """Q12: Risk limits aggregate existing and proposed risk; headroom respects total/daily ceilings."""
    rulebook = AccountRulebook(
        account_id="acc_live_01",
        starting_equity=Decimal(100000),
        daily_starting_equity=Decimal(98000),
        max_total_drawdown_pct=Decimal("0.10"),  # Floor = 90,000
        max_daily_drawdown_pct=Decimal("0.05"),  # Floor = 93,100
        max_risk_per_trade_pct=Decimal("0.01"),  # 1% per trade = 950
    )

    state = AccountState(
        current_equity=Decimal(95000),
        existing_open_risk=Decimal(1000),  # open risk
        positions={"BTC": Decimal("0.0")},
        as_of=datetime.now(UTC),
    )

    spec = InstrumentSpec(
        symbol="BTC",
        contract_size=Decimal("1.0"),
        min_lot=Decimal("0.01"),
        lot_step=Decimal("0.01"),
        fee_rate=Decimal("0.0005"),
        slippage_rate=Decimal("0.0005"),
    )

    loss_model = LossModel(stop_distance_pct=Decimal("0.02"))  # 2% stop

    target = _mock_target("1.0")
    advice = generate_account_advice(
        target,
        rulebook,
        state,
        spec,
        loss_model,
        valuation_price=Decimal(50000),
    )

    assert advice.status == AdviceStatus.SIZED
    # Total floor: 90,000 -> Headroom = 95,000 - 90,000 - 1,000 = 4,000
    # Daily floor: 93,100 -> Headroom = 95,000 - 93,100 - 1,000 = 900
    # Max per-trade risk: 95,000 * 0.01 = 950
    # Allowable risk = min(950, 4000, 900) = 900
    assert advice.allowable_risk_usd == Decimal(900)
    assert advice.target_quantity is not None
    assert advice.target_quantity > Decimal("0.0")

    # If daily headroom is completely exhausted:
    state_exhausted = AccountState(
        current_equity=Decimal(94000),
        existing_open_risk=Decimal(1500),  # 94,000 - 93,100 - 1,500 < 0
        positions={"BTC": Decimal("0.0")},
        as_of=datetime.now(UTC),
    )
    advice_blocked = generate_account_advice(
        target,
        rulebook,
        state_exhausted,
        spec,
        loss_model,
        valuation_price=Decimal(50000),
    )
    assert advice_blocked.status == AdviceStatus.BLOCKED
    assert advice_blocked.blocker_code == "RISK_HEADROOM_EXHAUSTED"


def test_q12_downward_lot_rounding_and_min_lot():
    """Q12: Lot rounding rounds downward to respect ceilings and enforces minimum lot."""
    rulebook = AccountRulebook(
        account_id="acc_02",
        starting_equity=Decimal(100000),
        daily_starting_equity=Decimal(100000),
        max_total_drawdown_pct=Decimal("0.10"),
        max_daily_drawdown_pct=Decimal("0.05"),
        max_risk_per_trade_pct=Decimal("0.01"),  # 1,000 risk
    )

    state = AccountState(
        current_equity=Decimal(100000),
        existing_open_risk=Decimal(0),
        positions={"BTC": Decimal("0.0")},
        as_of=datetime.now(UTC),
    )

    # Coarse lot step: 0.1 BTC, min lot: 0.2 BTC
    spec_coarse = InstrumentSpec(
        symbol="BTC",
        contract_size=Decimal("1.0"),
        min_lot=Decimal("0.2"),
        lot_step=Decimal("0.1"),
        fee_rate=Decimal("0.001"),
        slippage_rate=Decimal("0.001"),
    )

    loss_model = LossModel(stop_distance_pct=Decimal("0.05"))  # unit risk = 0.052

    target = _mock_target("1.0")
    advice = generate_account_advice(
        target,
        rulebook,
        state,
        spec_coarse,
        loss_model,
        valuation_price=Decimal(60000),
    )

    assert advice.status == AdviceStatus.SIZED
    # Max notional = 1000 / 0.052 = ~19230.76 -> max units = 19230.76 / 60000 = 0.3205
    # Rounded DOWN to 0.1 step -> 0.3 BTC
    assert advice.target_quantity == Decimal("0.3")


def test_q12_missing_data_produces_unsized_fallback():
    """Q12: Missing account / contract / risk data produces unsized informational output."""
    rulebook = AccountRulebook(
        account_id="acc_03",
        starting_equity=Decimal(100000),
        daily_starting_equity=Decimal(100000),
        max_total_drawdown_pct=Decimal("0.10"),
        max_daily_drawdown_pct=Decimal("0.05"),
        max_risk_per_trade_pct=Decimal("0.01"),
    )
    state = AccountState(
        current_equity=Decimal(100000),
        existing_open_risk=Decimal(0),
        positions={"BTC": Decimal("0.0")},
        as_of=datetime.now(UTC),
    )
    target = _mock_target("1.0")

    # Missing loss model entirely
    advice_no_model = generate_account_advice(
        target,
        rulebook,
        state,
        spec=None,
        loss_model=None,
        valuation_price=Decimal(50000),
    )
    assert advice_no_model.status == AdviceStatus.UNSIZED
    assert advice_no_model.target_quantity is None
    assert advice_no_model.action_delta is None
    assert advice_no_model.blocker_code == "LOSS_MODEL_MISSING"

    # Loss model with None stop distance
    loss_no_stop = LossModel(stop_distance_pct=None)
    advice_no_stop = generate_account_advice(
        target,
        rulebook,
        state,
        spec=None,
        loss_model=loss_no_stop,
        valuation_price=Decimal(50000),
    )
    assert advice_no_stop.status == AdviceStatus.UNSIZED


def test_q12_unknown_account_position_omits_action_delta():
    """Q12 & R6: When account holdings are unobserved/unknown, show target only without action delta."""
    rulebook = AccountRulebook(
        account_id="acc_04",
        starting_equity=Decimal(100000),
        daily_starting_equity=Decimal(100000),
        max_total_drawdown_pct=Decimal("0.10"),
        max_daily_drawdown_pct=Decimal("0.05"),
        max_risk_per_trade_pct=Decimal("0.01"),
    )
    # Positions is None (unknown/unobserved account state)
    state_unknown_pos = AccountState(
        current_equity=Decimal(100000),
        existing_open_risk=Decimal(0),
        positions=None,
        as_of=datetime.now(UTC),
    )
    spec = InstrumentSpec(
        symbol="BTC",
        contract_size=Decimal("1.0"),
        min_lot=Decimal("0.01"),
        lot_step=Decimal("0.01"),
        fee_rate=Decimal("0.0"),
        slippage_rate=Decimal("0.0"),
    )
    loss_model = LossModel(stop_distance_pct=Decimal("0.02"))
    target = _mock_target("1.0")

    advice = generate_account_advice(
        target,
        rulebook,
        state_unknown_pos,
        spec,
        loss_model,
        valuation_price=Decimal(50000),
    )
    assert advice.status == AdviceStatus.SIZED
    assert advice.target_quantity is not None
    # Crucial R6 rule: action delta is None because we don't know current holdings!
    assert advice.action_delta is None


def test_q08_telegram_notification_filtering_and_formatting():
    """Q08: Telegram adapter filters micro-fluctuations and formats readable HTML cards."""
    rulebook = AccountRulebook(
        account_id="acc_05",
        starting_equity=Decimal(100000),
        daily_starting_equity=Decimal(100000),
        max_total_drawdown_pct=Decimal("0.10"),
        max_daily_drawdown_pct=Decimal("0.05"),
        max_risk_per_trade_pct=Decimal("0.01"),
    )
    state = AccountState(
        current_equity=Decimal(100000),
        existing_open_risk=Decimal(0),
        positions={"BTC": Decimal("0.0")},
        as_of=datetime.now(UTC),
    )
    spec = InstrumentSpec(
        symbol="BTC",
        contract_size=Decimal("1.0"),
        min_lot=Decimal("0.01"),
        lot_step=Decimal("0.01"),
        fee_rate=Decimal("0.0"),
        slippage_rate=Decimal("0.0"),
    )
    loss_model = LossModel(stop_distance_pct=Decimal("0.02"))

    advice_1 = generate_account_advice(
        _mock_target("1.0"), rulebook, state, spec, loss_model, Decimal(50000)
    )
    assert should_notify_advice(advice_1, prior_advice=None)  # First advice notifies

    # Micro-fluctuation: 1% change (threshold is 5%) -> suppressed
    advice_micro = generate_account_advice(
        _mock_target("0.99"), rulebook, state, spec, loss_model, Decimal(50000)
    )
    assert not should_notify_advice(
        advice_micro, prior_advice=advice_1, meaningful_change_pct=Decimal("0.05")
    )

    # Meaningful change: 20% change -> notifies
    advice_large = generate_account_advice(
        _mock_target("0.80"), rulebook, state, spec, loss_model, Decimal(50000)
    )
    assert should_notify_advice(
        advice_large, prior_advice=advice_1, meaningful_change_pct=Decimal("0.05")
    )

    # HTML rendering
    html = format_advice_html(advice_1)
    assert "<b>Account Advice: BTC</b>" in html
    assert "Target Quantity: <b>" in html
    assert "Action Delta:" in html
