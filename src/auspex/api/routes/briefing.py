"""Daily briefing endpoint (arc42 §11 `GET /api/briefing`, §12 Home page).

Aggregates, in one authenticated and user-scoped call, everything the Home
page's "what changed today" needs: today's own portfolio summary, leg
movements ranked by absolute contribution delta (`leg_changes`),
HIGH-severity `risk_factors_added` escalated regardless of whether any leg
moved (arc42 §5.4), today's recommended actions with their full gate
traces, the day's run status, and any degraded-run assertion failures.

Response shape is reconciled 1:1 with `web/src/lib/types.ts`'s `Briefing`
(query param is `date`, matching `web/src/lib/api.tsx`'s `getBriefing`) —
see `auspex.api.schemas` for the field-by-field mapping rationale.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import (
    get_portfolio_projection_repo,
    get_recommendation_repo,
    get_run_repo,
    get_score_repo,
    get_universe,
)
from auspex.api.repos import get_digest_repo, get_document_repo, get_extraction_repo, get_leg_change_repo
from auspex.api.schemas import (
    BriefingChangeItem,
    BriefingResponse,
    BriefingScoreMover,
    EscalatedRiskItem,
    PortfolioSummary,
)
from auspex.api.viewmodels import build_recommendation_out, contribution_delta, map_run_status, sum_decimal_strings
from auspex.config.loader import Universe
from auspex.models.common import as_decimal
from auspex.models.enums import RiskSeverity
from auspex.models.portfolio import PortfolioProjection
from auspex.models.scoring import ScoreSnapshot
from auspex.persistence.repositories import CosmosRepository

router = APIRouter(prefix="/briefing", tags=["briefing"])

MAX_LEG_MOVEMENTS = 10


def _abs_delta_z(leg_change) -> float:  # noqa: ANN001 - LegChange, kept loose to avoid a circular import
    if leg_change.delta_z is None:
        return 0.0
    try:
        return abs(float(leg_change.delta_z))
    except ValueError:
        return 0.0


async def _latest_score(repo: CosmosRepository, security_id: str) -> ScoreSnapshot | None:
    rows = await repo.query(
        query="SELECT TOP 1 * FROM c WHERE c.security_id = @security_id ORDER BY c.as_of_date DESC",
        parameters=[{"name": "@security_id", "value": security_id}],
        partition_key=security_id,
    )
    return rows[0] if rows else None


async def _portfolio_summary(
    repo: CosmosRepository, user_id: str, effective_date: str
) -> PortfolioSummary | None:
    today: PortfolioProjection | None = await repo.get(f"{user_id}:{effective_date}", partition_key=user_id)
    if today is None:
        return None

    unrealised = sum_decimal_strings([p.unrealised_chf for p in today.positions])

    previous_rows = await repo.query(
        query=(
            "SELECT TOP 1 * FROM c WHERE c.user_id = @user_id AND c.as_of_date < @as_of_date "
            "ORDER BY c.as_of_date DESC"
        ),
        parameters=[{"name": "@user_id", "value": user_id}, {"name": "@as_of_date", "value": effective_date}],
        partition_key=user_id,
    )
    day_change = "0"
    if previous_rows:
        day_change = str(as_decimal(today.total_value_chf) - as_decimal(previous_rows[0].total_value_chf))

    return PortfolioSummary(
        value_chf=today.total_value_chf,
        invested_chf=today.invested_chf,
        total_gain_chf=today.total_gain_chf,
        day_change_chf=day_change,
        unrealised_chf=str(unrealised),
        cash_chf=today.cash_chf,
        expenses_chf=today.expenses_chf,
        dividends_chf=today.dividends_chf,
    )


async def _evidence_excerpt(
    document_repo: CosmosRepository, extraction_repo: CosmosRepository, security_id: str
) -> str:
    """Best-effort real evidence for a leg movement: the excerpt backing the
    security's most recently retrieved filing/news claim. `ChannelAExtraction`
    carries no timestamp of its own, so "most recent" is resolved via the
    document it was extracted from."""

    documents = await document_repo.query(
        query="SELECT TOP 1 * FROM c WHERE c.security_id = @security_id ORDER BY c.knowledge_date DESC",
        parameters=[{"name": "@security_id", "value": security_id}],
        partition_key=security_id,
    )
    if not documents:
        return ""
    extractions = await extraction_repo.query(
        query="SELECT * FROM c WHERE c.document_id = @document_id",
        parameters=[{"name": "@document_id", "value": documents[0].id}],
        partition_key=security_id,
    )
    if not extractions:
        return ""
    extraction = extractions[0]
    for claims in (extraction.theme_claims, extraction.risk_claims, extraction.narrative_claims):
        if claims:
            return claims[0].evidence_excerpt
    return ""


@router.get("", response_model=BriefingResponse)
async def get_briefing(
    date: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    universe: Universe = Depends(get_universe),
    recommendation_repo: CosmosRepository = Depends(get_recommendation_repo),
    leg_change_repo: CosmosRepository = Depends(get_leg_change_repo),
    document_repo: CosmosRepository = Depends(get_document_repo),
    digest_repo: CosmosRepository = Depends(get_digest_repo),
    extraction_repo: CosmosRepository = Depends(get_extraction_repo),
    score_repo: CosmosRepository = Depends(get_score_repo),
    run_repo: CosmosRepository = Depends(get_run_repo),
    portfolio_repo: CosmosRepository = Depends(get_portfolio_projection_repo),
) -> BriefingResponse:
    effective_date = date or date_cls.today().isoformat()
    by_id = universe.by_id()

    # --- run status + assertion failures (arc42 §6.1 `runs`) ---------------
    runs = await run_repo.query(
        query="SELECT * FROM c WHERE c.run_date = @run_date",
        parameters=[{"name": "@run_date", "value": effective_date}],
        partition_key=effective_date,
    )
    run = next((r for r in runs if r.run_type == "nightly"), runs[0] if runs else None)
    run_status = map_run_status(run.status) if run is not None else "RUNNING"
    assertion_failures = (
        [
            reason
            for reason in run.degraded_reasons
            if reason.startswith(("ASSERT:", "VALIDATE:", "NARRATE:"))
        ]
        if run is not None
        else []
    )
    if run_status == "DEGRADED" and not assertion_failures:
        run_status = "SUCCESS"

    # --- freshest evidence used to compute today's snapshot -----------------
    scores_today = await score_repo.query(
        query="SELECT * FROM c WHERE c.as_of_date = @as_of_date",
        parameters=[{"name": "@as_of_date", "value": effective_date}],
        partition_key=None,
    )
    max_knowledge_date = max((s.max_knowledge_date for s in scores_today), default=None) or effective_date
    score_today_by_id = {score.security_id: score for score in scores_today}

    prior_scores: list[ScoreSnapshot] = []
    effective_day = date_cls.fromisoformat(effective_date)
    for offset in range(1, 8):
        prior_date = (effective_day - timedelta(days=offset)).isoformat()
        prior_scores = await score_repo.query(
            query="SELECT * FROM c WHERE c.as_of_date = @as_of_date",
            parameters=[{"name": "@as_of_date", "value": prior_date}],
            partition_key=None,
        )
        if prior_scores:
            break
    prior_by_id = {score.security_id: score for score in prior_scores}
    recommendation_rows = await recommendation_repo.query(
        query="SELECT * FROM c WHERE c.user_id = @user_id AND c.as_of_date = @as_of_date",
        parameters=[{"name": "@user_id", "value": user.user_id}, {"name": "@as_of_date", "value": effective_date}],
        partition_key=user.user_id,
    )
    recommendation_by_security = {
        recommendation.security_id: recommendation for recommendation in recommendation_rows
    }
    movers: list[BriefingScoreMover] = []
    for security_id, current in score_today_by_id.items():
        prior = prior_by_id.get(security_id)
        if current.percentile is None or prior is None or prior.percentile is None:
            continue
        security = by_id.get(security_id)
        recommendation = recommendation_by_security.get(security_id)
        recommendation_out = (
            build_recommendation_out(
                recommendation,
                security.ticker if security else security_id,
                security.name if security else security_id,
                current,
            )
            if recommendation is not None
            else None
        )
        movers.append(
            BriefingScoreMover(
                security_id=security_id,
                ticker=security.ticker if security else security_id,
                company_name=security.name if security else security_id,
                score=current.percentile,
                prior_score=prior.percentile,
                score_change=current.percentile - prior.percentile,
                narrative=current.narrative or "",
                buy_ready=recommendation_out.buy_ready if recommendation_out else False,
                buy_blockers=(
                    recommendation_out.blocking_reasons
                    if recommendation_out
                    and recommendation is not None
                    and recommendation.action.value == "HOLD_NO_ACTION"
                    and Decimal(recommendation.current_weight_pct or "0") == 0
                    and current.percentile >= 75
                    else []
                ),
            )
        )
    movers_up = sorted(
        (mover for mover in movers if mover.score_change > 0),
        key=lambda mover: mover.score_change,
        reverse=True,
    )[:3]
    movers_down = sorted(
        (mover for mover in movers if mover.score_change < 0),
        key=lambda mover: mover.score_change,
    )[:3]

    # --- portfolio summary (Auspex's own projection, never the ledger) -----
    portfolio = await _portfolio_summary(portfolio_repo, user.user_id, effective_date)

    # --- leg movements ranked by |contribution delta| -----------------------
    leg_changes = await leg_change_repo.query(
        query="SELECT * FROM c WHERE c.as_of_date = @as_of_date",
        parameters=[{"name": "@as_of_date", "value": effective_date}],
        partition_key=None,
    )
    leg_changes.sort(key=_abs_delta_z, reverse=True)

    score_cache: dict[str, ScoreSnapshot | None] = dict(score_today_by_id)

    async def _score_for(security_id: str) -> ScoreSnapshot | None:
        if security_id not in score_cache:
            score_cache[security_id] = await _latest_score(score_repo, security_id)
        return score_cache[security_id]

    changes: list[BriefingChangeItem] = []
    for leg_change in leg_changes[:MAX_LEG_MOVEMENTS]:
        security = by_id.get(leg_change.security_id)
        ticker = security.ticker if security is not None else leg_change.security_id
        company_name = security.name if security is not None else leg_change.security_id

        score = await _score_for(leg_change.security_id)
        leg_result = score.legs.get(leg_change.leg) if score is not None else None
        weight = leg_result.weight if leg_result is not None else None
        narrative = score.narrative if score is not None and score.narrative else ""
        evidence_excerpt = await _evidence_excerpt(document_repo, extraction_repo, leg_change.security_id)

        changes.append(
            BriefingChangeItem(
                security_id=leg_change.security_id,
                ticker=ticker,
                company_name=company_name,
                leg=leg_change.leg,
                contribution_delta=contribution_delta(leg_change.delta_z, weight),
                narrative=narrative,
                evidence_excerpt=evidence_excerpt,
            )
        )

    # --- escalated HIGH-severity risk-factor additions ----------------------
    documents_today = await document_repo.query(
        query="SELECT * FROM c WHERE c.knowledge_date = @as_of_date",
        parameters=[{"name": "@as_of_date", "value": effective_date}],
        partition_key=None,
    )
    escalated: list[EscalatedRiskItem] = []
    for document in documents_today:
        digests = await digest_repo.query(
            query="SELECT * FROM c WHERE c.document_id = @document_id",
            parameters=[{"name": "@document_id", "value": document.id}],
            partition_key=document.security_id,
        )
        for digest in digests:
            if digest.comparative is None:
                continue
            for risk in digest.comparative.risk_factors_added:
                if risk.severity != RiskSeverity.HIGH:
                    continue
                security = by_id.get(document.security_id)
                ticker = security.ticker if security is not None else document.security_id
                escalated.append(
                    EscalatedRiskItem(
                        security_id=document.security_id,
                        ticker=ticker,
                        category=risk.category,
                        summary=risk.summary,
                        severity=risk.severity,
                    )
                )

    # --- today's actions, with full gate traces -----------------------------
    recommendations = []
    actionable = [
        recommendation
        for recommendation in recommendation_rows
        if not recommendation.action.value.startswith("HOLD")
        and not recommendation.suppressed
    ]

    def benefit(recommendation) -> tuple[float, int]:  # noqa: ANN001
        target = float(recommendation.target_weight_pct or 0)
        current = float(recommendation.current_weight_pct or 0)
        action_priority = {"SELL": 4, "TRIM": 3, "ADD": 2, "BUY": 1}.get(
            recommendation.action.value, 0
        )
        return abs(target - current), action_priority

    for recommendation in sorted(actionable, key=benefit, reverse=True)[:5]:
        security = by_id.get(recommendation.security_id)
        ticker = security.ticker if security is not None else recommendation.security_id
        company_name = security.name if security is not None else recommendation.security_id
        score = await _score_for(recommendation.security_id)
        recommendations.append(build_recommendation_out(recommendation, ticker, company_name, score))

    return BriefingResponse(
        date=effective_date,
        run_status=run_status,
        max_knowledge_date=max_knowledge_date,
        portfolio=portfolio,
        changes=changes,
        movers_up=movers_up,
        movers_down=movers_down,
        escalated_risks=escalated,
        recommendations=recommendations,
        assertion_failures=assertion_failures,
    )
