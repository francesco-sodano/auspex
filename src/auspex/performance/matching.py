"""Matched-population IC and correlation (arc42 §5.8).

Two measurement defects motivated this module:

1. **Per-leg IC required every candidate.** Computing a leg's IC only on dates
   where *every* scored name had that leg silently discarded whole dates. FPI
   filers structurally lack ``SMART_MONEY`` (arc42 §5.5 coverage), so the
   discarded dates were not missing at random and the surviving sample was
   biased toward domestic filers.
2. **Leg correlation pooled the all-legs-complete rows.** Keeping only names
   with all six legs populated, then flattening every such name-date into one
   vector per leg, mixed cross-sectional with time-series variation *and*
   selected on completeness.

Both are fixed the same way: each statistic uses its own matched
name/date population — the intersection of the two series it actually needs —
and reports how large that population was.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from auspex.models.enums import LegName
from auspex.performance.ic import pearson, spearman_ic


def matched_pairs(
    left: dict[str, Decimal],
    right: dict[str, Decimal],
) -> tuple[list[Decimal], list[Decimal], list[str]]:
    """Values for the securities present in *both* maps, ordered by security id.

    Sorting by id (rather than dict insertion order) keeps the pairing — and
    therefore any rank tie-breaking downstream — independent of upstream
    iteration order.
    """

    shared = sorted(set(left) & set(right))
    return [left[sid] for sid in shared], [right[sid] for sid in shared], shared


@dataclass(frozen=True)
class MatchedIC:
    as_of_date: date
    value: Decimal | None
    population: int
    candidates: int

    @property
    def coverage_fraction(self) -> Decimal | None:
        if self.candidates <= 0:
            return None
        return Decimal(self.population) / Decimal(self.candidates)


def matched_leg_ic(
    as_of_date: date,
    leg_z_by_security: dict[str, Decimal],
    forward_returns_by_security: dict[str, Decimal],
) -> MatchedIC:
    """Spearman IC for one leg on one date over that leg's own available names."""

    zs, returns, shared = matched_pairs(leg_z_by_security, forward_returns_by_security)
    return MatchedIC(
        as_of_date=as_of_date,
        value=spearman_ic(zs, returns),
        population=len(shared),
        candidates=len(forward_returns_by_security),
    )


def matched_composite_ic(
    as_of_date: date,
    percentile_by_security: dict[str, Decimal],
    forward_returns_by_security: dict[str, Decimal],
) -> MatchedIC:
    zs, returns, shared = matched_pairs(percentile_by_security, forward_returns_by_security)
    return MatchedIC(
        as_of_date=as_of_date,
        value=spearman_ic(zs, returns),
        population=len(shared),
        candidates=len(forward_returns_by_security),
    )


@dataclass(frozen=True)
class PairCorrelation:
    leg_a: LegName
    leg_b: LegName
    value: Decimal | None
    population: int


def matched_pair_correlation(
    leg_a: LegName,
    leg_b: LegName,
    values_a: dict[str, Decimal],
    values_b: dict[str, Decimal],
) -> PairCorrelation:
    """Pearson correlation over the two legs' own overlapping names for one date."""

    left, right, shared = matched_pairs(values_a, values_b)
    return PairCorrelation(leg_a=leg_a, leg_b=leg_b, value=pearson(left, right), population=len(shared))


def per_date_pair_correlations(
    leg_z_by_security: dict[LegName, dict[str, Decimal]],
) -> dict[tuple[LegName, LegName], PairCorrelation]:
    """All distinct leg pairs for a single date, each on its own matched population."""

    legs = sorted(leg_z_by_security, key=lambda leg: leg.value)
    result: dict[tuple[LegName, LegName], PairCorrelation] = {}
    for index, leg_a in enumerate(legs):
        for leg_b in legs[index + 1 :]:
            result[(leg_a, leg_b)] = matched_pair_correlation(
                leg_a,
                leg_b,
                leg_z_by_security[leg_a],
                leg_z_by_security[leg_b],
            )
    return result


@dataclass(frozen=True)
class AggregatedPairCorrelation:
    leg_a: LegName
    leg_b: LegName
    mean_correlation: Decimal | None
    per_date_values: list[Decimal]
    populations: list[int]

    @property
    def dates_used(self) -> int:
        return len(self.per_date_values)

    @property
    def total_observations(self) -> int:
        return sum(self.populations)

    @property
    def min_population(self) -> int:
        return min(self.populations) if self.populations else 0

    @property
    def max_population(self) -> int:
        return max(self.populations) if self.populations else 0


def aggregate_pair_correlations(
    leg_z_by_security_by_date: dict[date, dict[LegName, dict[str, Decimal]]],
) -> dict[tuple[LegName, LegName], AggregatedPairCorrelation]:
    """Per-date pairwise correlations averaged across dates.

    Replaces the pooled all-legs-complete estimate: the correlation is measured
    *within* each cross-section (where it is defined) and then averaged, so
    time-series drift in the legs cannot masquerade as cross-sectional
    redundancy.
    """

    values: dict[tuple[LegName, LegName], list[Decimal]] = {}
    populations: dict[tuple[LegName, LegName], list[int]] = {}
    for as_of_date in sorted(leg_z_by_security_by_date):
        for pair, correlation in per_date_pair_correlations(leg_z_by_security_by_date[as_of_date]).items():
            if correlation.value is None:
                continue
            values.setdefault(pair, []).append(correlation.value)
            populations.setdefault(pair, []).append(correlation.population)

    aggregated: dict[tuple[LegName, LegName], AggregatedPairCorrelation] = {}
    for pair, series in values.items():
        leg_a, leg_b = pair
        aggregated[pair] = AggregatedPairCorrelation(
            leg_a=leg_a,
            leg_b=leg_b,
            mean_correlation=sum(series, Decimal(0)) / Decimal(len(series)),
            per_date_values=series,
            populations=populations[pair],
        )
    return aggregated
