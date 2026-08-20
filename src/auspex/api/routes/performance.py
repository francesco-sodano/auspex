"""Performance metrics endpoint (arc42 §5.8, §11 `GET /api/performance`).

Aggregates the flat `PerformanceMetric` rows (`performance` container,
partitioned by `/metric_type`, arc42 §5.8) into the single `PerformanceReport`
shape `web/src/lib/types.ts` and `Performance.tsx` consume — composite IC at
21/63/126 sessions, per-leg IC, the leg correlation matrix, suggestion hit
rate, accepted/rejected disposition outcomes, and cohort dispersion — always
as of the most recent date each metric type was last computed, so **no query
parameter is required**.

Metric-row conventions this endpoint relies on (arc42 §5.8,
`auspex.performance.engine`):
- ``composite_ic``: `scope="universe"`, one row per `horizon_days` (21/63/126).
- ``leg_ic``: `scope=f"leg:{leg}"`, one row per leg per horizon; the report
  surfaces the 126-session horizon (matching `suggestion_hit_rate`'s own
  126-day evaluation window).
- ``leg_correlation``: `scope=f"{leg_a}x{leg_b}"` (lowercase `x` separator;
  no `LegName` value contains the letter "x", so the split is unambiguous).
- ``suggestion_hit_rate``: `scope="universe"`, `horizon_days=126`.
- ``disposition_outcome``: `scope="accepted"` / `scope="rejected"` — mirrors
  the unprefixed `"universe"` scope convention other metric types use.
- ``cohort_quality``: `scope=f"cohort:{name}"`, surfaced as `cohort_dispersion`.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import (
    get_performance_repo,
    get_portfolio_ledger_service,
    get_recommendation_repo,
    get_user_performance_repo,
)
from auspex.api.schemas import (
    AttributionStatus,
    DispositionOutcomes,
    LegCorrelationMatrix,
    PerformanceReport,
)
from auspex.models.enums import Action, LegName
from auspex.models.performance import PerformanceMetric
from auspex.models.policy import Recommendation
from auspex.performance.hit_rate import OUTCOME_MATURITY_CALENDAR_DAYS
from auspex.persistence.repositories import CosmosRepository
from auspex.portfolio.ledger_service import PortfolioLedgerService

router = APIRouter(prefix="/performance", tags=["performance"])

LEG_IC_HORIZON_DAYS = 126


async def _metrics_of_type(repo: CosmosRepository, metric_type: str) -> list[PerformanceMetric]:
    return await repo.query(
        query="SELECT * FROM c WHERE c.metric_type = @metric_type",
        parameters=[{"name": "@metric_type", "value": metric_type}],
        partition_key=metric_type,
    )


def _latest_as_of_date(metrics: list[PerformanceMetric]) -> date | None:
    dates = [m.as_of_date for m in metrics]
    return max(dates) if dates else None


def _latest_by_scope(metrics: list[PerformanceMetric]) -> dict[str, PerformanceMetric]:
    """Keep only the most-recent-`as_of_date` row per `scope` (a container can
    hold one row per weekly recompute, arc42 §6.1)."""

    latest: dict[str, PerformanceMetric] = {}
    for metric in metrics:
        current = latest.get(metric.scope)
        if current is None or metric.as_of_date > current.as_of_date:
            latest[metric.scope] = metric
    return latest


def _latest_by_horizon(metrics: list[PerformanceMetric]) -> dict[int | None, PerformanceMetric]:
    latest: dict[int | None, PerformanceMetric] = {}
    for metric in metrics:
        current = latest.get(metric.horizon_days)
        if current is None or metric.as_of_date > current.as_of_date:
            latest[metric.horizon_days] = metric
    return latest


@router.get("", response_model=PerformanceReport)
async def get_performance(
    user: AuthenticatedUser = Depends(get_current_user),
    repo: CosmosRepository = Depends(get_performance_repo),
    user_performance_repo: CosmosRepository = Depends(get_user_performance_repo),
    recommendation_repo: CosmosRepository[Recommendation] = Depends(
        get_recommendation_repo
    ),
    ledger: PortfolioLedgerService = Depends(get_portfolio_ledger_service),
) -> PerformanceReport:
    composite_ic_rows = await _metrics_of_type(repo, "composite_ic")
    leg_ic_rows = await _metrics_of_type(repo, "leg_ic")
    leg_correlation_rows = await _metrics_of_type(repo, "leg_correlation")
    hit_rate_rows = await user_performance_repo.query(
        query=(
            "SELECT * FROM c WHERE c.user_id=@user_id "
            "AND c.metric_type=@metric_type"
        ),
        parameters=[
            {"name": "@user_id", "value": user.user_id},
            {"name": "@metric_type", "value": "suggestion_hit_rate"},
        ],
        partition_key=user.user_id,
    )
    disposition_rows = await user_performance_repo.query(
        query=(
            "SELECT * FROM c WHERE c.user_id=@user_id "
            "AND c.metric_type=@metric_type"
        ),
        parameters=[
            {"name": "@user_id", "value": user.user_id},
            {"name": "@metric_type", "value": "disposition_outcome"},
        ],
        partition_key=user.user_id,
    )
    cohort_rows = await _metrics_of_type(repo, "cohort_quality")

    all_rows = [
        *composite_ic_rows,
        *leg_ic_rows,
        *leg_correlation_rows,
        *hit_rate_rows,
        *disposition_rows,
        *cohort_rows,
    ]
    as_of_date = _latest_as_of_date(all_rows) or date.today()

    latest_composite_by_horizon = _latest_by_horizon(composite_ic_rows)
    composite_ic = {
        str(horizon): latest_composite_by_horizon[horizon].value if horizon in latest_composite_by_horizon else None
        for horizon in (21, 63, 126)
    }

    leg_ic_at_horizon = [m for m in leg_ic_rows if m.horizon_days == LEG_IC_HORIZON_DAYS]
    latest_leg_ic_by_scope = _latest_by_scope(leg_ic_at_horizon)
    leg_ic = {
        leg.value: (
            latest_leg_ic_by_scope[f"leg:{leg.value}"].value if f"leg:{leg.value}" in latest_leg_ic_by_scope else None
        )
        for leg in LegName
    }

    latest_correlation_by_pair = _latest_by_scope(leg_correlation_rows)
    labels = [leg.value for leg in LegName]
    value_by_pair: dict[tuple[str, str], str] = {}
    for metric in latest_correlation_by_pair.values():
        leg_a, _, leg_b = metric.scope.partition("x")
        value_by_pair[(leg_a, leg_b)] = metric.value
        value_by_pair[(leg_b, leg_a)] = metric.value
    correlation_values = [[value_by_pair.get((a, b)) for b in labels] for a in labels]

    latest_hit_rate = max(hit_rate_rows, key=lambda m: m.as_of_date, default=None)
    suggestion_hit_rate = latest_hit_rate.value if latest_hit_rate is not None else None
    sample_size = latest_hit_rate.sample_size if latest_hit_rate is not None else 0
    backfilled_sample_size = (
        int(latest_hit_rate.detail.get("backfilled_sample_size", "0")) if latest_hit_rate is not None else 0
    )

    latest_disposition_by_scope = _latest_by_scope(disposition_rows)
    dispositions = DispositionOutcomes(
        accepted=latest_disposition_by_scope["accepted"].value if "accepted" in latest_disposition_by_scope else None,
        rejected=latest_disposition_by_scope["rejected"].value if "rejected" in latest_disposition_by_scope else None,
        accepted_sample_size=(
            latest_disposition_by_scope["accepted"].sample_size
            if "accepted" in latest_disposition_by_scope
            else 0
        ),
        rejected_sample_size=(
            latest_disposition_by_scope["rejected"].sample_size
            if "rejected" in latest_disposition_by_scope
            else 0
        ),
    )

    latest_cohort_by_scope = _latest_by_scope(cohort_rows)
    cohort_dispersion = {
        scope.removeprefix("cohort:"): metric.value for scope, metric in latest_cohort_by_scope.items()
    }
    recommendations = await recommendation_repo.query(
        query="SELECT * FROM c WHERE c.user_id=@user_id",
        parameters=[{"name": "@user_id", "value": user.user_id}],
        partition_key=user.user_id,
    )
    transactions = await ledger.list_transactions(user.user_id)
    followed_ids = {
        row.get("recommendation_id")
        for row in transactions
        if row.get("followed_auspex") and row.get("recommendation_id")
    }
    attribution = AttributionStatus()
    maturity_cutoff = date.today() - timedelta(
        days=OUTCOME_MATURITY_CALENDAR_DAYS
    )
    for recommendation in recommendations:
        if recommendation.action not in {
            Action.BUY,
            Action.ADD,
            Action.TRIM,
            Action.SELL,
        }:
            continue
        followed = recommendation.id in followed_ids
        mature = recommendation.as_of_date <= maturity_cutoff
        field = (
            "followed_mature"
            if followed and mature
            else "followed_pending"
            if followed
            else "not_followed_mature"
            if mature
            else "not_followed_pending"
        )
        setattr(attribution, field, getattr(attribution, field) + 1)

    return PerformanceReport(
        as_of_date=as_of_date,
        composite_ic=composite_ic,
        leg_ic=leg_ic,
        leg_correlation=LegCorrelationMatrix(labels=labels, values=correlation_values),
        suggestion_hit_rate=suggestion_hit_rate,
        dispositions=dispositions,
        attribution=attribution,
        cohort_dispersion=cohort_dispersion,
        sample_size=sample_size,
        backfilled_sample_size=backfilled_sample_size,
    )
