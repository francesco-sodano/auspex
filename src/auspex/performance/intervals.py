"""Confidence intervals for overlapping-horizon series (arc42 §5.8).

Per-date IC (and per-date spread return) observations at 21/63/126 sessions
overlap: consecutive dates share most of their forward window, so the naive
``std/sqrt(n)`` standard error is far too small and would declare noise
significant. Two overlap-aware estimators are published side by side:

- **Newey-West** — heteroskedasticity- and autocorrelation-consistent standard
  error with Bartlett weights and a lag equal to the horizon minus one, the
  standard choice for exactly this overlap structure.
- **Moving-block bootstrap** — resamples contiguous blocks (block length =
  horizon) so the resampled series inherits the dependence, then takes a
  percentile interval. Seeded and therefore exactly reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auspex.performance.stats import (
    ONE,
    TWO,
    ZERO,
    DeterministicRandom,
    effective_sample_size,
    quantile,
    sample_std,
    z_for_confidence,
)

DEFAULT_CONFIDENCE = Decimal("0.95")
DEFAULT_BOOTSTRAP_REPLICATES = 1000


@dataclass(frozen=True)
class ConfidenceInterval:
    point: Decimal
    low: Decimal
    high: Decimal
    confidence: Decimal
    method: str
    sample_size: int
    effective_sample_size: Decimal
    standard_error: Decimal | None = None
    seed: int | None = None
    replicates: int | None = None

    @property
    def excludes_zero(self) -> bool:
        return self.low < self.high and (
            self.low > 0 or self.high < 0
        )


def newey_west_standard_error(series: list[Decimal], lag: int) -> Decimal | None:
    """Bartlett-weighted HAC standard error of the *mean* of ``series``."""

    n = len(series)
    if n < 2:
        return None
    avg = sum(series, ZERO) / Decimal(n)
    deviations = [value - avg for value in series]

    gamma0 = sum((d * d for d in deviations), ZERO) / Decimal(n)
    variance = gamma0
    effective_lag = min(max(lag, 0), n - 1)
    for k in range(1, effective_lag + 1):
        gamma_k = sum((deviations[i] * deviations[i + k] for i in range(n - k)), ZERO) / Decimal(n)
        weight = ONE - Decimal(k) / Decimal(effective_lag + 1)
        variance += TWO * weight * gamma_k
    if variance <= 0:
        return None
    return (variance / Decimal(n)).sqrt()


def newey_west_interval(
    series: list[Decimal],
    *,
    horizon_days: int,
    confidence: Decimal = DEFAULT_CONFIDENCE,
) -> ConfidenceInterval | None:
    if len(series) < 2:
        return None
    avg = sum(series, ZERO) / Decimal(len(series))
    standard_error = newey_west_standard_error(series, lag=max(horizon_days - 1, 0))
    if standard_error is None:
        return None
    critical = z_for_confidence(confidence)
    return ConfidenceInterval(
        point=avg,
        low=avg - critical * standard_error,
        high=avg + critical * standard_error,
        confidence=confidence,
        method="newey_west",
        sample_size=len(series),
        effective_sample_size=effective_sample_size(len(series), horizon_days),
        standard_error=standard_error,
    )


def moving_block_bootstrap_means(
    series: list[Decimal],
    *,
    block_size: int,
    replicates: int,
    seed: int,
) -> list[Decimal]:
    """Bootstrap replicate means from contiguous blocks, deterministic for a given seed."""

    n = len(series)
    if n == 0 or block_size <= 0 or replicates <= 0:
        return []
    effective_block = min(block_size, n)
    start_count = n - effective_block + 1
    blocks_needed = -(-n // effective_block)  # ceil
    rng = DeterministicRandom(seed)

    means: list[Decimal] = []
    for _ in range(replicates):
        resampled: list[Decimal] = []
        for _ in range(blocks_needed):
            start = rng.below(start_count)
            resampled.extend(series[start : start + effective_block])
        resampled = resampled[:n]
        means.append(sum(resampled, ZERO) / Decimal(len(resampled)))
    return means


def block_bootstrap_interval(
    series: list[Decimal],
    *,
    horizon_days: int,
    seed: int,
    confidence: Decimal = DEFAULT_CONFIDENCE,
    replicates: int = DEFAULT_BOOTSTRAP_REPLICATES,
) -> ConfidenceInterval | None:
    """Percentile interval from a seeded moving-block bootstrap (block = horizon)."""

    if len(series) < 2:
        return None
    means = moving_block_bootstrap_means(
        series,
        block_size=max(horizon_days, 1),
        replicates=replicates,
        seed=seed,
    )
    if not means:
        return None
    tail = (ONE - confidence) / TWO
    low = quantile(means, tail)
    high = quantile(means, ONE - tail)
    if low is None or high is None:
        return None
    return ConfidenceInterval(
        point=sum(series, ZERO) / Decimal(len(series)),
        low=low,
        high=high,
        confidence=confidence,
        method="moving_block_bootstrap",
        sample_size=len(series),
        effective_sample_size=effective_sample_size(len(series), horizon_days),
        standard_error=sample_std(means),
        seed=seed,
        replicates=replicates,
    )
