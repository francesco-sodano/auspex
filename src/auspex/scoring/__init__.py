"""Deterministic scoring engine — six legs, cohort normalisation, composite (arc42 §5.5).

Pure Python. No I/O, no LLM. Fully unit-testable.
"""

from __future__ import annotations

from auspex.scoring.composite import (
    CompositeResult,
    LegCompositeResult,
    classify_direction,
    compute_percentile,
    compute_security_composite,
    cross_sectional_zscores,
)
from auspex.scoring.coverage import applicable_legs, coverage, is_stale
from auspex.scoring.engine import (
    SecurityScoreResult,
    SecurityScoringInput,
    build_cohort_scopes,
    score_universe,
)
from auspex.scoring.legs import (
    AttentionEvent,
    FundamentalHealthInputs,
    InsiderTxnEvent,
    NarrativeClaimEvent,
    ThemeClaimEvent,
    ValuationMetrics,
    attention_acceleration,
    enterprise_value,
    fcf_margin,
    fundamental_health,
    gross_margin_trend_slope,
    narrative_premium,
    net_cash_ratio,
    revenue_growth_yoy,
    roic,
    smart_money,
    thesis_linkage,
    valuation_brake,
)
from auspex.scoring.normalize import (
    CohortScope,
    assign_cohort_scope,
    clip,
    exponential_decay,
    mean_std,
    percentile_rank,
    winsorise,
    zscore,
)

__all__ = [
    "CompositeResult",
    "LegCompositeResult",
    "classify_direction",
    "compute_percentile",
    "compute_security_composite",
    "cross_sectional_zscores",
    "applicable_legs",
    "coverage",
    "is_stale",
    "SecurityScoreResult",
    "SecurityScoringInput",
    "build_cohort_scopes",
    "score_universe",
    "AttentionEvent",
    "FundamentalHealthInputs",
    "InsiderTxnEvent",
    "NarrativeClaimEvent",
    "ThemeClaimEvent",
    "ValuationMetrics",
    "attention_acceleration",
    "enterprise_value",
    "fcf_margin",
    "fundamental_health",
    "gross_margin_trend_slope",
    "narrative_premium",
    "net_cash_ratio",
    "revenue_growth_yoy",
    "roic",
    "smart_money",
    "thesis_linkage",
    "valuation_brake",
    "CohortScope",
    "assign_cohort_scope",
    "clip",
    "exponential_decay",
    "mean_std",
    "percentile_rank",
    "winsorise",
    "zscore",
]
