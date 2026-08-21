"""Deterministic ``Decimal`` statistics primitives for self-measurement (arc42 §5.8).

Pure ``Decimal`` (no numpy/scipy) so every published statistic is exactly
reproducible from stored state, consistent with the rest of the deterministic
engine. Resampling uses :class:`DeterministicRandom`, a self-contained
splitmix64 generator, rather than :mod:`random` — the sequence is fixed by the
seed and by this file alone, so a rerun months later reproduces the identical
confidence interval.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

_MASK64 = (1 << 64) - 1
_GOLDEN_GAMMA = 0x9E3779B97F4A7C15
_MIX_A = 0xBF58476D1CE4E5B9
_MIX_B = 0x94D049BB133111EB

ZERO = Decimal(0)
ONE = Decimal(1)
TWO = Decimal(2)

# Two-sided standard-normal quantiles for the confidence levels we publish.
_TWO_SIDED_Z = {
    Decimal("0.80"): Decimal("1.2815515655446004"),
    Decimal("0.90"): Decimal("1.6448536269514722"),
    Decimal("0.95"): Decimal("1.9599639845400545"),
    Decimal("0.99"): Decimal("2.5758293035489004"),
}

# Abramowitz & Stegun 7.1.26 erf approximation (|error| < 1.5e-7).
_ERF_P = Decimal("0.3275911")
_ERF_COEFFS = (
    Decimal("0.254829592"),
    Decimal("-0.284496736"),
    Decimal("1.421413741"),
    Decimal("-1.453152027"),
    Decimal("1.061405429"),
)


def seed_from_text(text: str) -> int:
    """Stable 64-bit seed derived from a label, so seeds are reproducible and self-describing."""

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


class DeterministicRandom:
    """Seeded splitmix64 generator — identical output on every platform and Python build."""

    def __init__(self, seed: int) -> None:
        self._state = seed & _MASK64

    def next_u64(self) -> int:
        self._state = (self._state + _GOLDEN_GAMMA) & _MASK64
        z = self._state
        z = ((z ^ (z >> 30)) * _MIX_A) & _MASK64
        z = ((z ^ (z >> 27)) * _MIX_B) & _MASK64
        return (z ^ (z >> 31)) & _MASK64

    def below(self, bound: int) -> int:
        """Uniform integer in ``[0, bound)`` with rejection sampling (no modulo bias)."""

        if bound <= 0:
            raise ValueError("bound must be positive")
        limit = (_MASK64 + 1) // bound * bound
        while True:
            drawn = self.next_u64()
            if drawn < limit:
                return drawn % bound

    def permutation(self, size: int) -> list[int]:
        """Fisher-Yates shuffle of ``range(size)`` using this generator."""

        items = list(range(size))
        for i in range(size - 1, 0, -1):
            j = self.below(i + 1)
            items[i], items[j] = items[j], items[i]
        return items


def mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, ZERO) / Decimal(len(values))


def sample_std(values: list[Decimal]) -> Decimal | None:
    """Sample standard deviation (n-1). ``None`` when fewer than two observations."""

    n = len(values)
    if n < 2:
        return None
    avg = sum(values, ZERO) / Decimal(n)
    variance = sum(((v - avg) ** 2 for v in values), ZERO) / Decimal(n - 1)
    return variance.sqrt() if variance > 0 else ZERO


def median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / TWO


def quantile(values: list[Decimal], q: Decimal) -> Decimal | None:
    """Linear-interpolation quantile (the ``numpy`` "linear" convention), ``q`` in [0, 1]."""

    if not values:
        return None
    if q < 0 or q > 1:
        raise ValueError("q must be within [0, 1]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * Decimal(len(ordered) - 1)
    lower_index = int(position.to_integral_value(rounding="ROUND_FLOOR"))
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = position - Decimal(lower_index)
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * weight


def fisher_z(correlation: Decimal) -> Decimal | None:
    """Fisher transform ``atanh(r)``. ``None`` at |r| >= 1 where the transform diverges."""

    if correlation <= -ONE or correlation >= ONE:
        return None
    return ((ONE + correlation) / (ONE - correlation)).ln() / TWO


def inverse_fisher_z(value: Decimal) -> Decimal:
    exp_2z = (TWO * value).exp()
    return (exp_2z - ONE) / (exp_2z + ONE)


def erf(x: Decimal) -> Decimal:
    sign = -ONE if x < 0 else ONE
    ax = abs(x)
    t = ONE / (ONE + _ERF_P * ax)
    poly = ZERO
    power = ONE
    for coefficient in _ERF_COEFFS:
        power *= t
        poly += coefficient * power
    return sign * (ONE - poly * (-(ax**2)).exp())


def normal_cdf(x: Decimal) -> Decimal:
    """Standard-normal CDF via the A&S erf approximation, clamped to [0, 1]."""

    value = (ONE + erf(x / TWO.sqrt())) / TWO
    if value < 0:
        return ZERO
    if value > ONE:
        return ONE
    return value


def two_sided_p_value(t_statistic: Decimal) -> Decimal:
    return TWO * (ONE - normal_cdf(abs(t_statistic)))


def z_for_confidence(confidence: Decimal) -> Decimal:
    """Two-sided normal critical value. Only pre-registered confidence levels are allowed."""

    try:
        return _TWO_SIDED_Z[confidence]
    except KeyError as exc:
        supported = ", ".join(str(level) for level in sorted(_TWO_SIDED_Z))
        raise ValueError(f"unsupported confidence level {confidence}; supported: {supported}") from exc


def overlap_variance_inflation(horizon_days: int, observations: int) -> Decimal:
    """Variance inflation from overlapping windows under a triangular (Bartlett) kernel.

    Consecutive daily observations of an ``h``-session forward return share
    ``h - k`` of their ``h`` sessions at lag ``k``, so the implied autocorrelation
    is ``1 - k/h`` and the inflation factor is ``1 + 2*sum_{k=1}^{h-1}(1 - k/h) = h``
    — capped at the number of observations, which is the most any finite sample
    can be inflated by.
    """

    if horizon_days <= 1 or observations <= 0:
        return ONE
    return Decimal(min(horizon_days, observations))


def effective_sample_size(observations: int, horizon_days: int) -> Decimal:
    """Effective *non-overlapping* observation count for an overlapping-horizon series.

    Daily observations of a 21/63/126-session forward return are not independent;
    reporting ``n`` overstates the evidence by roughly the horizon length. This is
    the denominator any t-statistic on such a series must use.
    """

    if observations <= 0:
        return ZERO
    return Decimal(observations) / overlap_variance_inflation(horizon_days, observations)


def effective_sample_size_from_autocorrelation(observations: int, autocorrelations: list[Decimal]) -> Decimal:
    """Empirical variant: ``n / (1 + 2*sum_k w_k*rho_k)`` with Bartlett weights."""

    if observations <= 0:
        return ZERO
    lag_count = len(autocorrelations)
    inflation = ONE
    for index, rho in enumerate(autocorrelations, start=1):
        weight = ONE - Decimal(index) / Decimal(lag_count + 1)
        inflation += TWO * weight * rho
    if inflation < ONE:
        inflation = ONE
    return Decimal(observations) / inflation


def autocorrelations(series: list[Decimal], max_lag: int) -> list[Decimal]:
    """Sample autocorrelations at lags ``1..max_lag`` (zero when variance vanishes)."""

    n = len(series)
    if n < 2 or max_lag < 1:
        return []
    avg = sum(series, ZERO) / Decimal(n)
    denominator = sum(((v - avg) ** 2 for v in series), ZERO)
    if denominator == 0:
        return [ZERO] * min(max_lag, n - 1)
    result: list[Decimal] = []
    for lag in range(1, min(max_lag, n - 1) + 1):
        numerator = sum(
            ((series[i] - avg) * (series[i + lag] - avg) for i in range(n - lag)),
            ZERO,
        )
        result.append(numerator / denominator)
    return result


@dataclass(frozen=True)
class Moments:
    count: int
    mean: Decimal
    std: Decimal | None


def moments(values: list[Decimal]) -> Moments | None:
    avg = mean(values)
    if avg is None:
        return None
    return Moments(count=len(values), mean=avg, std=sample_std(values))
