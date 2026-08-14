"""Self-measurement (arc42 §5.8) — Composite/leg IC, leg correlation, hit rate, cohort quality.

Built as a shipped feature, not an analysis exercise. Runs weekly and writes
to the `performance` container.
"""

from __future__ import annotations

from auspex.performance.cohort_quality import cohort_return_dispersion
from auspex.performance.correlation import (
    average_ic,
    composite_ic_for_date,
    leg_correlation_matrix,
    leg_ic_for_date,
)
from auspex.performance.engine import (
    HORIZONS,
    DateCrossSection,
    compute_cohort_quality_metrics,
    compute_composite_ic_metrics,
    compute_disposition_outcome_metric,
    compute_leg_correlation_metrics,
    compute_leg_ic_metrics,
    compute_suggestion_hit_rate_metric,
)
from auspex.performance.hit_rate import DispositionOutcome, SuggestionOutcome, disposition_hit_rate, suggestion_hit_rate
from auspex.performance.ic import pearson, rank, spearman_ic

__all__ = [
    "cohort_return_dispersion",
    "average_ic",
    "composite_ic_for_date",
    "leg_correlation_matrix",
    "leg_ic_for_date",
    "HORIZONS",
    "DateCrossSection",
    "compute_composite_ic_metrics",
    "compute_disposition_outcome_metric",
    "compute_cohort_quality_metrics",
    "compute_leg_correlation_metrics",
    "compute_leg_ic_metrics",
    "compute_suggestion_hit_rate_metric",
    "DispositionOutcome",
    "SuggestionOutcome",
    "disposition_hit_rate",
    "suggestion_hit_rate",
    "pearson",
    "rank",
    "spearman_ic",
]
