from datetime import date
from decimal import Decimal

from auspex.performance.engine import compute_disposition_outcome_metric
from auspex.performance.hit_rate import DispositionOutcome


def test_disposition_metric_splits_followed_and_rejected_outcomes() -> None:
    outcomes = [
        DispositionOutcome(
            security_return_usd=Decimal("0.20"),
            cohort_median_return_usd=Decimal("0.10"),
            accepted=True,
        ),
        DispositionOutcome(
            security_return_usd=Decimal("0.05"),
            cohort_median_return_usd=Decimal("0.10"),
            accepted=True,
        ),
        DispositionOutcome(
            security_return_usd=Decimal("0.15"),
            cohort_median_return_usd=Decimal("0.10"),
            accepted=False,
        ),
    ]

    accepted = compute_disposition_outcome_metric(
        date(2026, 8, 12),
        outcomes,
        accepted=True,
    )
    rejected = compute_disposition_outcome_metric(
        date(2026, 8, 12),
        outcomes,
        accepted=False,
    )

    assert accepted is not None
    assert accepted.value == "0.5"
    assert accepted.sample_size == 2
    assert rejected is not None
    assert rejected.value == "1"
    assert rejected.sample_size == 1


def test_disposition_metric_is_absent_without_mature_outcomes() -> None:
    assert (
        compute_disposition_outcome_metric(
            date(2026, 8, 12),
            [],
            accepted=True,
        )
        is None
    )
