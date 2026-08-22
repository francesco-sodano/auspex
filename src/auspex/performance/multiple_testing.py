"""Multiple-testing control for the metric family (arc42 §5.8).

Six legs x three horizons is eighteen simultaneous tests before the first
challenger is considered; at alpha = 0.05 roughly one spurious "significant"
leg is expected by construction. Benjamini-Hochberg is the single published
method and controls the false discovery rate across the family.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

DEFAULT_ALPHA = Decimal("0.05")


@dataclass(frozen=True)
class TestResult:
    label: str
    p_value: Decimal
    q_value: Decimal
    rejected: bool
    rank: int


def benjamini_hochberg(p_values: dict[str, Decimal], alpha: Decimal = DEFAULT_ALPHA) -> list[TestResult]:
    """Benjamini-Hochberg step-up FDR control.

    Returned in ascending p-value order, ties broken by label so the output is
    deterministic regardless of input ordering.
    """

    if not p_values:
        return []
    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    total = Decimal(len(ordered))

    raw_q: list[Decimal] = []
    for index, (_label, p_value) in enumerate(ordered, start=1):
        raw_q.append(p_value * total / Decimal(index))

    # Step-up monotonicity: q_(i) = min over j >= i of raw_q_(j), capped at 1.
    adjusted: list[Decimal] = [Decimal(0)] * len(raw_q)
    running = Decimal(1)
    for index in range(len(raw_q) - 1, -1, -1):
        running = min(running, raw_q[index])
        adjusted[index] = running

    largest_rejected = 0
    for index, (_label, p_value) in enumerate(ordered, start=1):
        if p_value <= alpha * Decimal(index) / total:
            largest_rejected = index

    return [
        TestResult(
            label=label,
            p_value=p_value,
            q_value=adjusted[index - 1],
            rejected=index <= largest_rejected,
            rank=index,
        )
        for index, (label, p_value) in enumerate(ordered, start=1)
    ]
