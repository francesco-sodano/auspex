"""Aggregate response/request schemas for API routes (arc42 §11).

These are read-model shapes that join several containers together (e.g. a
security's latest score, or a document's digest) — kept separate from the
persisted models in `auspex.models` so a projection never gets confused with
an actual Cosmos document shape. Nothing here carries `user_id` in a request
body: every owner-scoped value is still derived from the validated token
(arc42 §11 "`user_id` derives from the token, never from the request body").

Field names and nesting here are deliberately reconciled 1:1 with
`web/src/lib/types.ts` (`Briefing`, `SecuritySummary`, `SecurityPackage`,
`Recommendation`, `GateTrace`) so the SPA needs no adapter layer between the
API response and its own types.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field

from auspex.models.common import AuspexModel
from auspex.models.enums import (
    Action,
    Direction,
    DispositionStatus,
    FilerProfile,
    LegName,
    RiskCategory,
    RiskSeverity,
)
from auspex.models.portfolio import PositionProjectionRow

BriefingRunStatus = Literal["SUCCESS", "DEGRADED", "FAILED", "RUNNING"]


class GateTraceOut(AuspexModel):
    """One row of `Recommendation.gate_trace` — matches `web/src/lib/types.ts` `GateTrace`.

    Renamed from the persisted `auspex.models.policy.GateResult`
    (`actual_value`/`threshold_value`/`detail`) to the frontend's
    `actual`/`threshold`/`reason` — same values, API-facing names.
    """

    gate: str
    passed: bool
    actual: str | None = None
    threshold: str | None = None
    reason: str | None = None


class RecommendationOut(AuspexModel):
    """A recommendation as the SPA consumes it — matches `web/src/lib/types.ts` `Recommendation`.

    Enriches the persisted `auspex.models.policy.Recommendation` (which only
    carries `security_id`) with `ticker`/`company_name` (joined from the
    universe) and a human-readable `rationale` (the security's latest
    narrative, or a gate-cascade summary when no narrative exists yet).
    """

    id: str
    security_id: str
    ticker: str
    company_name: str
    action: Action
    rationale: str
    target_weight: str | None = None
    current_weight: str | None = None
    suggested_trade_chf: str | None = None
    suggested_quantity: str | None = None
    allocation_mode: str = "LEGACY_INDEPENDENT"
    allocation_trace: list[GateTraceOut] = Field(default_factory=list)
    estimated_cost_chf: str | None = None
    auspex_score: int | None = None
    buy_ready: bool = False
    blocking_reasons: list[str] = Field(default_factory=list)
    gate_trace: list[GateTraceOut] = Field(default_factory=list)
    as_of_date: date | None = None
    disposition: DispositionStatus | None = None
    followed: bool = False
    outcome_matures_on: date | None = None
    outcome_mature: bool = False


class SecuritySummary(AuspexModel):
    """One row of `GET /api/securities` — matches `web/src/lib/types.ts` `SecuritySummary`."""

    security_id: str
    ticker: str
    name: str
    market: str = "US"
    cohort: str
    score: str | None = None
    percentile: int | None = None
    direction: Direction | None = None
    coverage: str | None = None
    action: Action | None = None


class SecuritySummaryWithProfile(SecuritySummary):
    """`SecurityPackage.security` — `SecuritySummary` plus `filer_profile` (arc42 §12 Discussion)."""

    filer_profile: FilerProfile


class LegDetail(AuspexModel):
    """One entry of `SecurityPackage.legs` — matches `web/src/lib/types.ts` `SecurityPackage.legs`."""

    raw: str | None = None
    z: str | None = None
    weight: str
    contribution: str | None = None
    computable: bool
    score: int | None = None
    neutral: bool = False
    status_explanation: str | None = None


class SecurityHistoryPoint(AuspexModel):
    """One entry of `SecurityPackage.history` — only fields with a computed composite/percentile."""

    as_of_date: date
    composite: str
    percentile: int


class SecurityDocumentOut(AuspexModel):
    """One entry of `SecurityPackage.documents` — matches `web/src/lib/types.ts` inline document shape.

    Also the response shape of the standalone `GET
    /api/securities/{id}/documents` (arc42 §11), so both call sites share one
    view model instead of two.
    """

    document_id: str
    form: str
    filed_at: str
    headline: str
    digest: str
    source_url: str
    publisher: str
    retrieved_at: datetime
    relevance_reason: str
    stale: bool = False


class FundamentalMetricOut(AuspexModel):
    label: str
    value: str | None = None
    period_end: date | None = None


class SecurityPricePoint(AuspexModel):
    date: date
    close: str


class SecurityPackage(AuspexModel):
    """`GET /api/securities/{id}` — matches `web/src/lib/types.ts` `SecurityPackage`.

    Embeds a compact `history` and the full `documents` list directly (arc42
    §12 Discussion loads a security's whole package in one call); the
    dedicated `GET /securities/{id}/history` and `.../documents` routes
    remain available separately for callers that only need one slice.
    """

    security: SecuritySummaryWithProfile
    as_of_date: date
    narrative: str
    legs: dict[str, LegDetail] = Field(default_factory=dict)
    recommendation: RecommendationOut | None = None
    market: str
    business_summary: str
    current_price_usd: str | None = None
    price_change_pct: str | None = None
    price_history: list[SecurityPricePoint] = Field(default_factory=list)
    fundamentals: list[FundamentalMetricOut] = Field(default_factory=list)
    score_change: int | None = None
    score_reasoning: str
    news: list[SecurityDocumentOut] = Field(default_factory=list)
    history: list[SecurityHistoryPoint] = Field(default_factory=list)
    documents: list[SecurityDocumentOut] = Field(default_factory=list)


class PortfolioSummary(AuspexModel):
    """`Briefing.portfolio` — matches `web/src/lib/types.ts` inline shape (arc42 §12 Home tiles)."""

    value_chf: str
    invested_chf: str
    cash_chf: str
    total_gain_chf: str
    day_change_chf: str
    unrealised_chf: str = "0"
    expenses_chf: str = "0"
    dividends_chf: str = "0"


class BriefingChangeItem(AuspexModel):
    """One entry of `Briefing.changes` — matches `web/src/lib/types.ts` inline shape.

    `contribution_delta` is `weight * delta_z` using the security's current
    leg weight (delta_z alone when no current weight is known yet); ranking
    in `GET /api/briefing` sorts by this value's absolute magnitude.
    """

    security_id: str
    ticker: str
    company_name: str
    leg: LegName
    contribution_delta: str
    narrative: str
    evidence_excerpt: str


class BriefingScoreMover(AuspexModel):
    security_id: str
    ticker: str
    company_name: str
    score: int
    prior_score: int
    score_change: int
    summary: str
    narrative: str = ""
    buy_ready: bool = False
    buy_blockers: list[str] = Field(default_factory=list)


class EscalatedRiskItem(AuspexModel):
    """One entry of `Briefing.escalated_risks` — a HIGH-severity `risk_factors_added`
    entry, surfaced regardless of leg movement (arc42 §5.4)."""

    security_id: str
    ticker: str
    category: RiskCategory
    summary: str
    severity: RiskSeverity


class BriefingResponse(AuspexModel):
    """`GET /api/briefing` — matches `web/src/lib/types.ts` `Briefing` (arc42 §11, §12 Home)."""

    date: date
    run_status: BriefingRunStatus
    max_knowledge_date: date
    portfolio: PortfolioSummary | None = None
    changes: list[BriefingChangeItem] = Field(default_factory=list)
    movers_up: list[BriefingScoreMover] = Field(default_factory=list)
    movers_down: list[BriefingScoreMover] = Field(default_factory=list)
    escalated_risks: list[EscalatedRiskItem] = Field(default_factory=list)
    recommendations: list[RecommendationOut] = Field(default_factory=list)
    assertion_failures: list[str] = Field(default_factory=list)


class DispositionRequest(AuspexModel):
    """Body of `POST /api/recommendations/{id}/disposition`.

    Only the disposition itself — never a `user_id` (that comes solely from
    the validated Entra token, arc42 §11).
    """

    disposition: DispositionStatus


class PricePointOut(AuspexModel):
    date: date
    open: str
    high: str
    low: str
    close: str


class PortfolioPositionOut(PositionProjectionRow):
    company_name: str
    auspex_score: int | None = None
    action: Action | None = None
    buy_ready: bool | None = None
    readiness_reason: str | None = None
    price_history: list[PricePointOut] = Field(default_factory=list)


class PortfolioView(AuspexModel):
    as_of_date: date
    lot_level: bool
    total_value_chf: str
    invested_chf: str
    cash_chf: str
    total_gain_chf: str
    day_change_chf: str
    expenses_chf: str
    dividends_chf: str
    source_ledger_read_at: str
    degraded_fields: list[str] = Field(default_factory=list)
    positions: list[PortfolioPositionOut] = Field(default_factory=list)


TransactionType = Literal[
    "OPENING_POSITION",
    "OPENING_CASH",
    "BUY",
    "SELL",
    "DEPOSIT",
    "WITHDRAWAL",
    "DIVIDEND",
    "INTEREST",
    "FEE",
    "TAX",
]


class PortfolioCostComponentRequest(AuspexModel):
    category: Literal[
        "BROKER_COMMISSION",
        "TRANSACTION_TAX",
        "WITHHOLDING_TAX",
        "VAT",
        "CUSTODY_FEE",
        "ACCOUNT_FEE",
        "OTHER_FEE",
    ]
    amount: str
    currency: Literal["CHF", "USD"]


class PortfolioTransactionRequest(AuspexModel):
    client_request_id: str
    transaction_type: TransactionType
    event_date: date
    currency: str = "CHF"
    security_code: str | None = None
    quantity: str | None = None
    price: str | None = None
    amount: str | None = None
    fees: str = "0"
    broker_commission: str = "0"
    stamp_duty: str = "0"
    taxes: str = "0"
    cost_components: list[PortfolioCostComponentRequest] | None = None
    fx_rate_to_base: str | None = None
    followed_auspex: bool = False
    recommendation_id: str | None = None
    notes: str | None = None


class PortfolioTransactionOut(AuspexModel):
    transaction_id: str
    transaction_type: str
    event_date: date
    currency: str
    security_code: str | None = None
    quantity: str | None = None
    price: str | None = None
    gross_amount: str
    cash_amount: str
    cash_currency: str = "CHF"
    fees: str
    cost_components: list[dict[str, str | None]] = Field(default_factory=list)
    fx_rate_to_base: str | None = None
    followed_auspex: bool = False
    recommendation_id: str | None = None
    notes: str | None = None
    created_at: str
    corrects_transaction_id: str | None = None
    status: Literal["EFFECTIVE", "CORRECTED", "VOIDED"]


class LegCorrelationMatrix(AuspexModel):
    """Pairwise Pearson correlation across the six legs (arc42 §5.8, §12).

    ``labels[i]``/``labels[j]`` index into ``values[i][j]``; a missing pair
    (insufficient sample, dropped by `auspex.performance.engine`) renders as
    `None` rather than a fabricated zero.
    """

    labels: list[str]
    values: list[list[str | None]]


class DispositionOutcomes(AuspexModel):
    """Accepted vs. rejected suggestion hit rate (arc42 §5.8, §12)."""

    accepted: str | None = None
    rejected: str | None = None
    accepted_sample_size: int = 0
    rejected_sample_size: int = 0


class AttributionStatus(AuspexModel):
    followed_pending: int = 0
    followed_mature: int = 0
    not_followed_pending: int = 0
    not_followed_mature: int = 0


class HorizonDiagnostics(AuspexModel):
    mean_ic: str | None = None
    icir: str | None = None
    effective_sample_size: str | None = None
    confidence_low: str | None = None
    confidence_high: str | None = None
    confidence_method: str | None = None
    confidence_level: str | None = None
    excludes_zero: bool | None = None
    robust_spread: str | None = None
    cost_adjusted_spread: str | None = None
    mean_turnover: str | None = None
    max_drawdown: str | None = None
    outlier_count: int = 0
    equal_weight_return: str | None = None
    momentum_ic: str | None = None
    random_p95_absolute: str | None = None


class PerformanceReport(AuspexModel):
    """`GET /api/performance` — arc42 §11, §12 Performance page.

    One aggregate read model assembled from the flat `PerformanceMetric` rows
    (`performance` container, arc42 §5.8), always as of the most recent date
    each metric type was last computed — no query parameter is required.
    """

    as_of_date: date
    composite_ic: dict[str, str | None] = Field(
        default_factory=dict, description="keys '21' | '63' | '126' (session horizons)"
    )
    leg_ic: dict[str, str | None] = Field(default_factory=dict, description="keyed by LegName value")
    leg_correlation: LegCorrelationMatrix
    suggestion_hit_rate: str | None = None
    dispositions: DispositionOutcomes
    attribution: AttributionStatus = Field(default_factory=AttributionStatus)
    cohort_dispersion: dict[str, str | None] = Field(default_factory=dict, description="keyed by cohort name")
    diagnostics: dict[str, HorizonDiagnostics] = Field(
        default_factory=dict,
        description="uncertainty, robust spread and benchmark context by horizon",
    )
    sample_size: int
    backfilled_sample_size: int
