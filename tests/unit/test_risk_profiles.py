from decimal import Decimal

from auspex.config.loader import load_policy
from auspex.policy.engine import load_policy_thresholds
from auspex.policy.target_weight import target_weight_pct


def test_risk_profiles_change_action_thresholds_meaningfully() -> None:
    policy = load_policy()
    conservative = load_policy_thresholds(policy, risk_profile="CONSERVATIVE")
    moderate = load_policy_thresholds(policy, risk_profile="MODERATE")
    aggressive = load_policy_thresholds(policy, risk_profile="AGGRESSIVE")

    assert conservative.buy_min_percentile > moderate.buy_min_percentile
    assert moderate.buy_min_percentile > aggressive.buy_min_percentile
    assert conservative.buy_max_resulting_weight_pct < moderate.buy_max_resulting_weight_pct
    assert moderate.buy_max_resulting_weight_pct < aggressive.buy_max_resulting_weight_pct
    assert conservative.buy_min_cash_after_trade_chf > moderate.buy_min_cash_after_trade_chf
    assert moderate.buy_min_cash_after_trade_chf > aggressive.buy_min_cash_after_trade_chf


def test_custom_cash_reserve_overrides_profile_default() -> None:
    thresholds = load_policy_thresholds(
        load_policy(),
        risk_profile="AGGRESSIVE",
        cash_reserve_chf="4200",
    )

    assert thresholds.buy_min_cash_after_trade_chf == Decimal("4200")


def test_target_weight_uses_profile_bounds() -> None:
    assert target_weight_pct(
        80,
        max_pct=Decimal("10"),
        floor_pct=Decimal("3"),
    ) == Decimal("8")
    assert target_weight_pct(
        10,
        max_pct=Decimal("20"),
        floor_pct=Decimal("5"),
    ) == Decimal("5")
