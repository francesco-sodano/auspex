"""Self-measurement (arc42 §5.8) — Composite/leg IC, leg correlation, hit rate, cohort quality.

Built as a shipped feature, not an analysis exercise. Runs weekly and writes
to the `performance` container.
"""

from __future__ import annotations

from auspex.performance.benchmarks import PairedComparison, equal_weight_return, momentum_ic, paired_comparison
from auspex.performance.cohort_quality import cohort_return_dispersion
from auspex.performance.correlation import (
    average_ic,
    composite_ic_for_date,
    leg_correlation_matrix,
    leg_ic_for_date,
)
from auspex.performance.coverage_bias import CoverageBiasResult, coverage_bias
from auspex.performance.detail import DETAILED_METRICS_VERSION
from auspex.performance.distribution import ICDistribution, ic_distribution
from auspex.performance.engine import (
    HORIZONS,
    DateCrossSection,
    compute_benchmark_metrics,
    compute_cohort_quality_metrics,
    compute_composite_ic_metrics,
    compute_coverage_bias_metrics,
    compute_detailed_metrics,
    compute_disposition_outcome_metric,
    compute_ic_distribution_metrics,
    compute_ic_interval_metrics,
    compute_leg_correlation_metrics,
    compute_leg_correlation_metrics_per_date,
    compute_leg_ic_metrics,
    compute_multiple_testing_metrics,
    compute_spread_metrics,
    compute_suggestion_hit_rate_metric,
)
from auspex.performance.hit_rate import DispositionOutcome, SuggestionOutcome, disposition_hit_rate, suggestion_hit_rate
from auspex.performance.ic import pearson, rank, spearman_ic
from auspex.performance.intervals import (
    ConfidenceInterval,
    block_bootstrap_interval,
    newey_west_interval,
)
from auspex.performance.matching import (
    AggregatedPairCorrelation,
    MatchedIC,
    aggregate_pair_correlations,
    matched_composite_ic,
    matched_leg_ic,
    matched_pairs,
)
from auspex.performance.multiple_testing import TestResult, benjamini_hochberg
from auspex.performance.shadow import (
    CHAMPION,
    CORRECTED_FIXED,
    PreRegistration,
    ShadowCrossSection,
    ShadowReport,
    ShadowVariant,
    default_pre_registration,
    promotion_verdict,
    run_shadow_comparison,
    shadow_metrics,
)
from auspex.performance.spread import SpreadResult, max_drawdown, top_minus_bottom, turnover
from auspex.performance.stats import DeterministicRandom, effective_sample_size, seed_from_text

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
    # Detailed metrics (arc42 §5.8) — versioned by DETAILED_METRICS_VERSION.
    "DETAILED_METRICS_VERSION",
    "compute_benchmark_metrics",
    "compute_coverage_bias_metrics",
    "compute_detailed_metrics",
    "compute_ic_distribution_metrics",
    "compute_ic_interval_metrics",
    "compute_leg_correlation_metrics_per_date",
    "compute_multiple_testing_metrics",
    "compute_spread_metrics",
    "AggregatedPairCorrelation",
    "MatchedIC",
    "aggregate_pair_correlations",
    "matched_composite_ic",
    "matched_leg_ic",
    "matched_pairs",
    "ICDistribution",
    "ic_distribution",
    "ConfidenceInterval",
    "block_bootstrap_interval",
    "newey_west_interval",
    "TestResult",
    "benjamini_hochberg",
    "PairedComparison",
    "equal_weight_return",
    "momentum_ic",
    "paired_comparison",
    "SpreadResult",
    "max_drawdown",
    "top_minus_bottom",
    "turnover",
    "CoverageBiasResult",
    "coverage_bias",
    "DeterministicRandom",
    "effective_sample_size",
    "seed_from_text",
    # Pre-registered shadow validation — measurement only, never production scoring.
    "CHAMPION",
    "CORRECTED_FIXED",
    "PreRegistration",
    "ShadowCrossSection",
    "ShadowReport",
    "ShadowVariant",
    "default_pre_registration",
    "promotion_verdict",
    "run_shadow_comparison",
    "shadow_metrics",
]
