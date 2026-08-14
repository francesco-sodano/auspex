"""Post-run assertions (arc42 §5.6 "Post-run assertions").

Violation raises an alert and marks the run DEGRADED. It does not roll back
— a degraded day is still published, visibly flagged.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auspex.models.enums import Action


@dataclass(frozen=True)
class AssertionViolation:
    name: str
    detail: str


def run_post_run_assertions(
    *,
    actions: list[Action],
    scored_security_count: int,
    eligible_but_no_cash_count: int,
    policy_config: dict,
) -> list[AssertionViolation]:
    violations: list[AssertionViolation] = []
    cfg = policy_config["assertions"]

    actionable_count = sum(
        1
        for action in actions
        if action in {Action.BUY, Action.ADD, Action.TRIM, Action.SELL}
    )
    if not (actionable_count > 0 or eligible_but_no_cash_count > 0):
        violations.append(
            AssertionViolation(
                "at_least_one_actionable_or_eligible_no_cash",
                (
                    f"actionable_count={actionable_count}, "
                    f"eligible_but_no_cash_count={eligible_but_no_cash_count}"
                ),
            )
        )

    hold_insufficient = sum(1 for a in actions if a == Action.HOLD_INSUFFICIENT_DATA)
    fraction = Decimal(hold_insufficient) / Decimal(len(actions)) if actions else Decimal(0)
    max_fraction = Decimal(cfg["max_hold_insufficient_data_fraction"])
    if fraction >= max_fraction:
        violations.append(
            AssertionViolation(
                "hold_insufficient_data_fraction_below_max",
                f"fraction={fraction}, max={max_fraction}",
            )
        )

    min_scored = cfg["min_scored_securities"]
    if scored_security_count < min_scored:
        violations.append(
            AssertionViolation(
                "min_scored_securities",
                f"scored={scored_security_count}, min={min_scored}",
            )
        )

    return violations
