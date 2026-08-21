"""Coverage bias diagnostics (arc42 §5.8).

Coverage is not missing at random: FPI filers structurally lack ``SMART_MONEY``
and thinly-followed names lack attention data. If the composite scores
well-covered names systematically higher, part of the measured IC is a data
availability artefact rather than signal. Three checks quantify that:

- correlation between coverage and score (does coverage drive the ranking?);
- correlation between coverage and forward return (is coverage itself priced?);
- IC measured separately on the well-covered and thinly-covered halves, with
  the difference reported, so any concentration of the signal in one
  availability regime is visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auspex.performance.ic import spearman_ic
from auspex.performance.matching import matched_pairs
from auspex.performance.stats import median


@dataclass(frozen=True)
class CoverageBiasResult:
    population: int
    coverage_score_correlation: Decimal | None
    coverage_return_correlation: Decimal | None
    coverage_median: Decimal | None
    high_coverage_population: int
    low_coverage_population: int
    high_coverage_ic: Decimal | None
    low_coverage_ic: Decimal | None

    @property
    def ic_difference(self) -> Decimal | None:
        if self.high_coverage_ic is None or self.low_coverage_ic is None:
            return None
        return self.high_coverage_ic - self.low_coverage_ic


def coverage_bias(
    coverage_by_security: dict[str, Decimal],
    scores_by_security: dict[str, Decimal],
    forward_returns_by_security: dict[str, Decimal],
) -> CoverageBiasResult | None:
    """Coverage-bias diagnostics for one cross-section."""

    shared = sorted(set(coverage_by_security) & set(scores_by_security) & set(forward_returns_by_security))
    if len(shared) < 2:
        return None

    coverages = [coverage_by_security[sid] for sid in shared]
    coverage_median = median(coverages)

    covered_scores, covered_returns, _ = matched_pairs(
        {sid: coverage_by_security[sid] for sid in shared},
        {sid: scores_by_security[sid] for sid in shared},
    )
    coverage_score_correlation = spearman_ic(covered_scores, covered_returns)

    covered_only, return_values, _ = matched_pairs(
        {sid: coverage_by_security[sid] for sid in shared},
        {sid: forward_returns_by_security[sid] for sid in shared},
    )
    coverage_return_correlation = spearman_ic(covered_only, return_values)

    high_ids = [sid for sid in shared if coverage_median is not None and coverage_by_security[sid] >= coverage_median]
    low_ids = [sid for sid in shared if coverage_median is not None and coverage_by_security[sid] < coverage_median]

    high_ic = spearman_ic(
        [scores_by_security[sid] for sid in high_ids],
        [forward_returns_by_security[sid] for sid in high_ids],
    )
    low_ic = spearman_ic(
        [scores_by_security[sid] for sid in low_ids],
        [forward_returns_by_security[sid] for sid in low_ids],
    )

    return CoverageBiasResult(
        population=len(shared),
        coverage_score_correlation=coverage_score_correlation,
        coverage_return_correlation=coverage_return_correlation,
        coverage_median=coverage_median,
        high_coverage_population=len(high_ids),
        low_coverage_population=len(low_ids),
        high_coverage_ic=high_ic,
        low_coverage_ic=low_ic,
    )
