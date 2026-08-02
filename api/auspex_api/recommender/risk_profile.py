from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RiskPolicy:
    max_position_weight: Decimal
    cash_buffer_pct: Decimal
    min_trade_base: Decimal


_POLICIES = {
    "Conservative": RiskPolicy(Decimal("0.06"), Decimal("0.20"), Decimal("750")),
    "Balanced": RiskPolicy(Decimal("0.10"), Decimal("0.12"), Decimal("500")),
    "Growth": RiskPolicy(Decimal("0.13"), Decimal("0.08"), Decimal("400")),
    "Aggressive": RiskPolicy(Decimal("0.16"), Decimal("0.05"), Decimal("300")),
}


def policy_for_profile(risk_profile: str) -> RiskPolicy:
    try:
        return _POLICIES[risk_profile]
    except KeyError as exc:
        raise ValueError("invalid risk profile") from exc