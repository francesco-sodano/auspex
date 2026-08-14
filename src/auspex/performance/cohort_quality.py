"""Cohort quality — within-cohort return dispersion (arc42 §5.8).

A cohort whose members move identically cannot rank anything; low dispersion
is itself a diagnostic, not merely descriptive.
"""

from __future__ import annotations

from decimal import Decimal

from auspex.scoring.normalize import mean_std


def cohort_return_dispersion(returns_usd: list[Decimal]) -> Decimal | None:
    """Population standard deviation of trailing returns within one cohort."""

    _, std = mean_std(returns_usd)
    return std
