"""Benchmark comparisons for the composite signal (arc42 §5.8).

An IC of 0.03 means nothing without a reference point. Three references are
computed from data the engine already stores, so no new ingestion is required:

- **Equal weight** — the return of holding every scored name equally. The
  honest hurdle for any long/short spread claim.
- **Random ranking** — seeded random permutations of the cross-section, giving
  the empirical null band an IC of this sample size would produce by chance.
- **Simple momentum** — trailing realised return used directly as the score.
  The cheapest competing signal; a composite that cannot beat it is not
  earning its complexity.

Where an input is unavailable (no trailing returns stored, for example) the
corresponding comparison is omitted rather than approximated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from auspex.performance.ic import spearman_ic
from auspex.performance.matching import matched_pairs
from auspex.performance.stats import ZERO, DeterministicRandom, mean, quantile, sample_std

DEFAULT_RANDOM_REPLICATES = 200


def equal_weight_return(forward_returns_by_security: dict[str, Decimal]) -> Decimal | None:
    """Mean forward return of the scored cross-section — the equal-weight benchmark."""

    return mean([forward_returns_by_security[sid] for sid in sorted(forward_returns_by_security)])


def momentum_ic(
    trailing_returns_by_security: dict[str, Decimal],
    forward_returns_by_security: dict[str, Decimal],
) -> Decimal | None:
    """IC of the naive momentum signal (trailing return ranks forward return)."""

    trailing, forward, _shared = matched_pairs(trailing_returns_by_security, forward_returns_by_security)
    return spearman_ic(trailing, forward)


def random_ranking_ics(
    forward_returns_by_security: dict[str, Decimal],
    *,
    seed: int,
    replicates: int = DEFAULT_RANDOM_REPLICATES,
) -> list[Decimal]:
    """ICs of ``replicates`` seeded random rankings of the same cross-section."""

    securities = sorted(forward_returns_by_security)
    if len(securities) < 2 or replicates <= 0:
        return []
    forward = [forward_returns_by_security[sid] for sid in securities]
    rng = DeterministicRandom(seed)

    results: list[Decimal] = []
    for _ in range(replicates):
        permutation = rng.permutation(len(securities))
        pseudo_scores = [Decimal(position) for position in permutation]
        value = spearman_ic(pseudo_scores, forward)
        if value is not None:
            results.append(value)
    return results


@dataclass(frozen=True)
class RandomNullBand:
    replicates: int
    mean: Decimal
    std: Decimal | None
    p95_absolute: Decimal


def random_null_band(
    forward_returns_by_security_by_date: dict[date, dict[str, Decimal]],
    *,
    seed: int,
    replicates: int = DEFAULT_RANDOM_REPLICATES,
) -> RandomNullBand | None:
    """Empirical null distribution of the per-date IC under random ranking.

    The 95th percentile of ``|IC|`` is the level a genuinely uninformative
    signal clears one date in twenty; a composite IC below it is indistinguishable
    from noise at this cross-section size.
    """

    pooled: list[Decimal] = []
    for index, as_of_date in enumerate(sorted(forward_returns_by_security_by_date)):
        pooled.extend(
            random_ranking_ics(
                forward_returns_by_security_by_date[as_of_date],
                seed=seed + index,
                replicates=replicates,
            )
        )
    if not pooled:
        return None
    absolute = [abs(value) for value in pooled]
    p95 = quantile(absolute, Decimal("0.95"))
    return RandomNullBand(
        replicates=len(pooled),
        mean=sum(pooled, ZERO) / Decimal(len(pooled)),
        std=sample_std(pooled),
        p95_absolute=ZERO if p95 is None else p95,
    )


@dataclass(frozen=True)
class PairedComparison:
    name: str
    count: int
    mean_difference: Decimal
    std_difference: Decimal | None
    win_fraction: Decimal
    differences: list[Decimal]


def paired_comparison(
    name: str,
    champion_by_date: dict[date, Decimal],
    benchmark_by_date: dict[date, Decimal],
) -> PairedComparison | None:
    """Date-matched difference (champion minus benchmark) over their shared dates."""

    shared = sorted(set(champion_by_date) & set(benchmark_by_date))
    if not shared:
        return None
    differences = [champion_by_date[day] - benchmark_by_date[day] for day in shared]
    wins = sum(1 for difference in differences if difference > 0)
    return PairedComparison(
        name=name,
        count=len(differences),
        mean_difference=sum(differences, ZERO) / Decimal(len(differences)),
        std_difference=sample_std(differences),
        win_fraction=Decimal(wins) / Decimal(len(differences)),
        differences=differences,
    )
