"""Recommendation endpoints (arc42 §5.10 "recommendations" + "gate_trace")."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import (
    get_portfolio_ledger_service,
    get_recommendation_disposition_repo,
    get_recommendation_repo,
    get_score_repo,
    get_universe,
)
from auspex.api.schemas import DispositionRequest, RecommendationOut
from auspex.api.viewmodels import build_recommendation_out
from auspex.config.loader import Universe
from auspex.models.common import utc_now
from auspex.models.enums import Action, DispositionStatus
from auspex.models.policy import Recommendation, RecommendationDisposition
from auspex.performance.hit_rate import OUTCOME_MATURITY_CALENDAR_DAYS
from auspex.persistence.repositories import CosmosRepository
from auspex.portfolio.ledger_service import PortfolioLedgerService
from auspex.settings import get_settings

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


@router.get("/history/{security_id}", response_model=list[RecommendationOut])
async def recommendation_history(
    security_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    universe: Universe = Depends(get_universe),
    repo: CosmosRepository = Depends(get_recommendation_repo),
    score_repo: CosmosRepository = Depends(get_score_repo),
    ledger: PortfolioLedgerService = Depends(get_portfolio_ledger_service),
) -> list[RecommendationOut]:
    security = universe.by_id().get(security_id)
    if security is None:
        raise HTTPException(status_code=404, detail="security not found")
    date_from = date.today() - timedelta(days=2)
    rows = await repo.query(
        query=(
            "SELECT * FROM c WHERE c.user_id=@user_id "
            "AND c.security_id=@security_id AND c.as_of_date>=@date_from "
            "ORDER BY c.as_of_date DESC"
        ),
        parameters=[
            {"name": "@user_id", "value": user.user_id},
            {"name": "@security_id", "value": security_id},
            {"name": "@date_from", "value": date_from.isoformat()},
        ],
        partition_key=user.user_id,
    )
    rows = [
        row
        for row in rows
        if row.action in {Action.BUY, Action.ADD, Action.TRIM, Action.SELL}
    ]
    scores = await score_repo.query(
        query="SELECT * FROM c WHERE c.security_id=@security_id",
        parameters=[{"name": "@security_id", "value": security_id}],
        partition_key=security_id,
    )
    score_by_date = {row.as_of_date: row for row in scores}
    transactions = await ledger.list_transactions(user.user_id)
    followed_ids = {
        row.get("recommendation_id")
        for row in transactions
        if row.get("followed_auspex") and row.get("recommendation_id")
    }
    today = date.today()
    history: list[RecommendationOut] = []
    for row in rows:
        matures_on = row.as_of_date + timedelta(
            days=OUTCOME_MATURITY_CALENDAR_DAYS
        )
        history.append(
            build_recommendation_out(
                row,
                security.ticker,
                security.name,
                score_by_date.get(row.as_of_date),
            ).model_copy(
                update={
                    "as_of_date": row.as_of_date,
                    "disposition": row.disposition,
                    "followed": row.id in followed_ids,
                    "outcome_matures_on": matures_on,
                    "outcome_mature": today >= matures_on,
                }
            )
        )
    return history


@router.get("", response_model=list[Recommendation])
async def list_recommendations(
    as_of_date: str,
    include_suppressed: bool = Query(
        default=False,
        description="include decisions the user has rejected or deferred (audit view)",
    ),
    user: AuthenticatedUser = Depends(get_current_user),
    repo: CosmosRepository = Depends(get_recommendation_repo),
) -> list[Recommendation]:
    """One day's recommendations for the caller.

    Suppressed rows — decisions whose signature the user has already rejected
    or deferred — are withheld by default. They remain stored so the run is
    still fully auditable and so the suppression itself is explainable.
    """

    rows = await repo.query(
        query="SELECT * FROM c WHERE c.user_id = @user_id AND c.as_of_date = @as_of_date",
        parameters=[{"name": "@user_id", "value": user.user_id}, {"name": "@as_of_date", "value": as_of_date}],
        partition_key=user.user_id,
    )
    if include_suppressed:
        return rows
    return [row for row in rows if not getattr(row, "suppressed", False)]


@router.post("/{recommendation_id}/disposition", response_model=Recommendation)
async def set_disposition(
    recommendation_id: str,
    request: DispositionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    repo: CosmosRepository = Depends(get_recommendation_repo),
    disposition_repo: CosmosRepository = Depends(get_recommendation_disposition_repo),
) -> Recommendation:
    """Record accepted / rejected / deferred (arc42 §5.7, §8.3).

    Written only to Auspex's own containers, never to the external ledger.
    The recommendation is looked up scoped to the caller's own partition
    (`user_id` from the token) so one user can never disposition another
    user's recommendation, regardless of what id is guessed.

    Beyond stamping the row, ``REJECTED`` and ``DEFERRED`` also persist a
    durable suppression keyed on the recommendation's *decision signature*:
    tomorrow's run will not re-raise the same ask. ``REJECTED`` holds
    indefinitely; ``DEFERRED`` lapses after
    ``Settings.deferred_disposition_days`` (7 by default). Either way, a
    materially different decision — different action, size, target or gate
    outcome — carries a different signature and surfaces normally.
    """

    recommendation = await repo.get(recommendation_id, partition_key=user.user_id)
    if recommendation is None or recommendation.user_id != user.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="recommendation not found")

    now = utc_now()
    recommendation.disposition = request.disposition
    recommendation.suppressed = request.disposition in {
        DispositionStatus.REJECTED,
        DispositionStatus.DEFERRED,
    }
    recommendation.suppression_reason = (
        request.disposition.value if recommendation.suppressed else None
    )
    await repo.upsert(recommendation)

    if recommendation.decision_signature:
        expires_at = None
        if request.disposition is DispositionStatus.DEFERRED:
            expires_at = now + timedelta(days=get_settings().deferred_disposition_days)
        await disposition_repo.upsert(
            RecommendationDisposition(
                id=f"{user.user_id}:{recommendation.security_id}",
                user_id=user.user_id,
                security_id=recommendation.security_id,
                disposition=request.disposition,
                decision_signature=recommendation.decision_signature,
                recommendation_id=recommendation.id,
                as_of_date=recommendation.as_of_date,
                recorded_at=now,
                expires_at=expires_at,
            )
        )
    return recommendation


@router.get("/dispositions", response_model=list[RecommendationDisposition])
async def list_dispositions(
    user: AuthenticatedUser = Depends(get_current_user),
    disposition_repo: CosmosRepository = Depends(get_recommendation_disposition_repo),
) -> list[RecommendationDisposition]:
    """The caller's durable dispositions — strictly their own partition."""

    return await disposition_repo.query(
        query="SELECT * FROM c WHERE c.user_id = @user_id",
        parameters=[{"name": "@user_id", "value": user.user_id}],
        partition_key=user.user_id,
    )
