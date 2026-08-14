"""Live portfolio analytics and owner-scoped transaction management."""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import (
    get_fx_sink,
    get_portfolio_adapter,
    get_portfolio_ledger_service,
    get_portfolio_projection_repo,
    get_price_sink,
    get_recommendation_repo,
    get_score_repo,
    get_universe,
)
from auspex.api.schemas import (
    PortfolioPositionOut,
    PortfolioTransactionOut,
    PortfolioTransactionRequest,
    PortfolioView,
    PricePointOut,
)
from auspex.api.viewmodels import build_recommendation_out
from auspex.config.loader import Universe
from auspex.models.common import utc_now
from auspex.models.enums import Action
from auspex.models.policy import Recommendation
from auspex.models.portfolio import PortfolioProjection, PositionProjectionRow
from auspex.models.scoring import ScoreSnapshot
from auspex.persistence.repositories import CosmosFxSink, CosmosPriceSink, CosmosRepository
from auspex.portfolio.adapter import PortfolioAdapter
from auspex.portfolio.ledger_service import PortfolioLedgerService, PortfolioLedgerValidationError
from auspex.portfolio.projection import project_portfolio

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


async def _validate_recommendation_attribution(
    request: PortfolioTransactionRequest,
    user: AuthenticatedUser,
    universe: Universe,
    recommendation_repo: CosmosRepository[Recommendation],
) -> None:
    if not request.followed_auspex:
        return
    if request.recommendation_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="recommendation_id is required when followed_auspex is true",
        )
    recommendation = await recommendation_repo.get(
        request.recommendation_id,
        partition_key=user.user_id,
    )
    security = universe.by_ticker().get((request.security_code or "").upper())
    compatible_actions = {
        "BUY": {"BUY", "ADD"},
        "SELL": {"SELL", "TRIM"},
    }
    allowed = compatible_actions.get(request.transaction_type, set())
    if (
        recommendation is None
        or recommendation.user_id != user.user_id
        or security is None
        or recommendation.security_id != security.id
        or recommendation.action.value not in allowed
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="recommendation does not match this transaction owner, ticker, or action",
        )


def _transaction_out(row: dict) -> PortfolioTransactionOut:
    return PortfolioTransactionOut(
        transaction_id=str(row["transaction_id"]),
        transaction_type=str(row["transaction_type"]),
        event_date=row["event_date"],
        currency=str(row["currency"]),
        security_code=row.get("security_code"),
        quantity=str(row["quantity"]) if row.get("quantity") is not None else None,
        price=str(row["price"]) if row.get("price") is not None else None,
        gross_amount=str(row.get("gross_amount", "0")),
        cash_amount=str(row.get("cash_amount", "0")),
        cash_currency=str(row.get("cash_currency") or row.get("currency") or "CHF"),
        fees=str(row.get("fees", "0")),
        cost_components=[
            {
                "category": str(component.get("category", "OTHER_FEE")),
                "amount": str(component.get("amount", "0")),
                "currency": str(component.get("currency") or row.get("currency", "CHF")),
                "source_amount": (
                    str(component["source_amount"])
                    if component.get("source_amount") is not None
                    else None
                ),
                "source_currency": component.get("source_currency"),
                "fx_rate_to_settlement": (
                    str(component["fx_rate_to_settlement"])
                    if component.get("fx_rate_to_settlement") is not None
                    else None
                ),
            }
            for component in row.get("cost_components", [])
        ],
        fx_rate_to_base=(
            str(row["fx_rate_to_base"]) if row.get("fx_rate_to_base") is not None else None
        ),
        followed_auspex=bool(row.get("followed_auspex", False)),
        recommendation_id=row.get("recommendation_id"),
        notes=row.get("notes"),
        created_at=str(row.get("created_at", "")),
        corrects_transaction_id=row.get("corrects_transaction_id"),
        status=row.get("status", "EFFECTIVE"),
    )


async def _current_projection(
    *,
    user: AuthenticatedUser,
    effective_date: date,
    universe: Universe,
    adapter: PortfolioAdapter,
    price_sink: CosmosPriceSink,
    fx_sink: CosmosFxSink,
    score_repo: CosmosRepository[ScoreSnapshot],
    recommendation_repo: CosmosRepository[Recommendation],
    projection_repo: CosmosRepository[PortfolioProjection],
) -> PortfolioView:
    security_ids = [security.id for security in universe.securities]
    ticker_by_id = {security.id: security.ticker for security in universe.securities}
    id_by_ticker = {security.ticker: security.id for security in universe.securities}
    security_by_ticker = universe.by_ticker()
    latest_prices = await price_sink.latest_as_of(effective_date, security_ids)
    prices_by_ticker = {
        ticker_by_id[bar.security_id]: Decimal(bar.close_adjusted)
        for bar in latest_prices
        if bar.security_id in ticker_by_id
    }
    fx_rows = [row for row in await fx_sink.all() if row.session_date <= effective_date]
    fx_rate = Decimal(max(fx_rows, key=lambda row: row.session_date).close_rate) if fx_rows else Decimal(1)
    snapshot = await adapter.read_snapshot(
        effective_date,
        fx_rate_to_chf=lambda currency: (
            Decimal(1)
            if currency.upper() == "CHF"
            else fx_rate
            if currency.upper() == "USD"
            else None
        ),
    )
    projected = project_portfolio(snapshot, prices_by_ticker, fx_rate, effective_date)
    read_at = utc_now()
    persisted_rows = [
        PositionProjectionRow(
            ticker=position.ticker,
            quantity=str(position.quantity),
            weight=str(position.weight) if position.weight is not None else None,
            market_value_usd=(
                str(position.market_value_usd) if position.market_value_usd is not None else None
            ),
            market_value_chf=(
                str(position.market_value_chf) if position.market_value_chf is not None else None
            ),
            cost_basis_usd=(
                str(position.cost_basis_usd) if position.cost_basis_usd is not None else None
            ),
            cost_basis_chf=(
                str(position.cost_basis_chf) if position.cost_basis_chf is not None else None
            ),
            unrealised_usd=(
                str(position.unrealised_usd) if position.unrealised_usd is not None else None
            ),
            unrealised_chf=(
                str(position.unrealised_chf) if position.unrealised_chf is not None else None
            ),
            fx_effect_chf=(
                str(position.fx_effect_chf) if position.fx_effect_chf is not None else None
            ),
            holding_period_days=position.holding_period_days,
            source_ledger_read_at=read_at,
            degraded_fields=position.degraded_fields,
        )
        for position in projected.positions
    ]
    projection = PortfolioProjection(
        id=f"{user.user_id}:{effective_date.isoformat()}",
        user_id=user.user_id,
        as_of_date=effective_date,
        lot_level=projected.lot_level,
        total_value_chf=str(projected.total_value_chf),
        invested_chf=str(projected.invested_chf),
        total_gain_chf=str(projected.total_gain_chf),
        cash_chf=str(projected.cash_chf),
        dividends_chf=str(projected.dividends_chf),
        expenses_chf=str(projected.expenses_chf),
        withdrawals_chf=str(projected.withdrawals_chf),
        positions=persisted_rows,
        degraded_fields=projected.degraded_fields,
    )
    prior_rows = await projection_repo.query(
        query=(
            "SELECT TOP 1 * FROM c WHERE c.user_id=@user_id AND c.as_of_date<@as_of "
            "ORDER BY c.as_of_date DESC"
        ),
        parameters=[
            {"name": "@user_id", "value": user.user_id},
            {"name": "@as_of", "value": effective_date.isoformat()},
        ],
        partition_key=user.user_id,
    )
    if effective_date == date.today():
        await projection_repo.upsert(projection)
    day_change = Decimal(0)
    if prior_rows:
        day_change = projected.total_value_chf - Decimal(prior_rows[0].total_value_chf)

    scores = await score_repo.query(
        query="SELECT * FROM c WHERE c.as_of_date=@as_of",
        parameters=[{"name": "@as_of", "value": effective_date.isoformat()}],
    )
    score_by_id = {score.security_id: score for score in scores}
    recommendations = await recommendation_repo.query(
        query=(
            "SELECT * FROM c WHERE c.user_id=@user_id AND c.as_of_date=@as_of"
        ),
        parameters=[
            {"name": "@user_id", "value": user.user_id},
            {"name": "@as_of", "value": effective_date.isoformat()},
        ],
        partition_key=user.user_id,
    )
    recommendation_by_id = {
        recommendation.security_id: recommendation
        for recommendation in recommendations
    }

    async def enrich(row: PositionProjectionRow) -> PortfolioPositionOut:
        security_id = id_by_ticker.get(row.ticker)
        recommendation = recommendation_by_id.get(security_id) if security_id else None
        security = security_by_ticker.get(row.ticker)
        recommendation_out = (
            build_recommendation_out(
                recommendation,
                row.ticker,
                security.name if security else row.ticker,
                score_by_id.get(security_id) if security_id else None,
            )
            if recommendation is not None
            else None
        )
        readiness_reason = None
        if recommendation is not None and recommendation.action == Action.HOLD_INSUFFICIENT_DATA:
            readiness_reason = (
                recommendation_out.blocking_reasons[0]
                if recommendation_out and recommendation_out.blocking_reasons
                else "Coverage is below 80% or the comparison cohort has low confidence."
            )
        history = (
            await price_sink.history_as_of(security_id, effective_date, 7)
            if security_id is not None
            else []
        )
        return PortfolioPositionOut(
            **row.model_dump(),
            company_name=(
                security_by_ticker[row.ticker].name
                if row.ticker in security_by_ticker
                else row.ticker
            ),
            auspex_score=(
                score_by_id[security_id].percentile
                if security_id in score_by_id
                else None
            ),
            action=(
                recommendation_by_id[security_id].action
                if security_id in recommendation_by_id
                else None
            ),
            buy_ready=(
                recommendation_by_id[security_id].action in {Action.BUY, Action.ADD}
                if security_id in recommendation_by_id
                else None
            ),
            readiness_reason=readiness_reason,
            price_history=[
                PricePointOut(
                    date=bar.session_date,
                    open=bar.open_raw,
                    high=bar.high_raw,
                    low=bar.low_raw,
                    close=bar.close_raw,
                )
                for bar in history
            ],
        )

    positions = await asyncio.gather(*(enrich(row) for row in persisted_rows))
    return PortfolioView(
        as_of_date=effective_date,
        lot_level=projection.lot_level,
        total_value_chf=projection.total_value_chf,
        invested_chf=projection.invested_chf,
        cash_chf=projection.cash_chf,
        total_gain_chf=projection.total_gain_chf,
        day_change_chf=str(day_change),
        expenses_chf=projection.expenses_chf,
        dividends_chf=projection.dividends_chf,
        source_ledger_read_at=read_at.isoformat(),
        degraded_fields=projection.degraded_fields,
        positions=positions,
    )


@router.get("", response_model=PortfolioView)
async def get_portfolio(
    as_of_date: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    universe: Universe = Depends(get_universe),
    adapter: PortfolioAdapter = Depends(get_portfolio_adapter),
    price_sink: CosmosPriceSink = Depends(get_price_sink),
    fx_sink: CosmosFxSink = Depends(get_fx_sink),
    score_repo: CosmosRepository = Depends(get_score_repo),
    recommendation_repo: CosmosRepository = Depends(get_recommendation_repo),
    projection_repo: CosmosRepository = Depends(get_portfolio_projection_repo),
) -> PortfolioView:
    effective_date = date.fromisoformat(as_of_date) if as_of_date else date.today()
    return await _current_projection(
        user=user,
        effective_date=effective_date,
        universe=universe,
        adapter=adapter,
        price_sink=price_sink,
        fx_sink=fx_sink,
        score_repo=score_repo,
        recommendation_repo=recommendation_repo,
        projection_repo=projection_repo,
    )


@router.get("/history", response_model=list[PortfolioProjection])
async def get_portfolio_history(
    date_from: str = Query(alias="from"),
    date_to: str = Query(alias="to"),
    user: AuthenticatedUser = Depends(get_current_user),
    repo: CosmosRepository = Depends(get_portfolio_projection_repo),
) -> list[PortfolioProjection]:
    return await repo.query(
        query=(
            "SELECT * FROM c WHERE c.user_id=@user_id AND c.as_of_date>=@from "
            "AND c.as_of_date<=@to"
        ),
        parameters=[
            {"name": "@user_id", "value": user.user_id},
            {"name": "@from", "value": date_from},
            {"name": "@to", "value": date_to},
        ],
        partition_key=user.user_id,
    )


@router.get("/binding")
async def get_portfolio_binding(
    user: AuthenticatedUser = Depends(get_current_user),
    repo: CosmosRepository = Depends(get_portfolio_projection_repo),
) -> dict:
    projections = await repo.query(
        query="SELECT TOP 1 * FROM c WHERE c.user_id=@user_id ORDER BY c.as_of_date DESC",
        parameters=[{"name": "@user_id", "value": user.user_id}],
        partition_key=user.user_id,
    )
    projection = projections[0] if projections else None
    degraded_fields = projection.degraded_fields if projection is not None else []
    last_read_at = None
    if projection is not None and projection.positions:
        last_read_at = max(
            position.source_ledger_read_at for position in projection.positions
        ).isoformat()
    critical_fields = {"market_value"}
    critical_missing = critical_fields.intersection(degraded_fields)
    return {
        "status": "READY" if projection is not None and not critical_missing else "DEGRADED",
        "lot_level": projection.lot_level if projection is not None else False,
        "degraded_fields": degraded_fields,
        "last_successful_read_at": last_read_at,
    }


@router.get("/transactions", response_model=list[PortfolioTransactionOut])
async def list_transactions(
    user: AuthenticatedUser = Depends(get_current_user),
    service: PortfolioLedgerService = Depends(get_portfolio_ledger_service),
) -> list[PortfolioTransactionOut]:
    try:
        return [_transaction_out(row) for row in await service.list_transactions(user.user_id)]
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


@router.post("/transactions", response_model=PortfolioTransactionOut, status_code=status.HTTP_201_CREATED)
async def create_transaction(
    request: PortfolioTransactionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    service: PortfolioLedgerService = Depends(get_portfolio_ledger_service),
    universe: Universe = Depends(get_universe),
    recommendation_repo: CosmosRepository = Depends(get_recommendation_repo),
) -> PortfolioTransactionOut:
    try:
        await _validate_recommendation_attribution(
            request,
            user,
            universe,
            recommendation_repo,
        )
        row = await service.create_transaction(user.user_id, request.model_dump(mode="json"))
        return _transaction_out({**row, "status": "EFFECTIVE"})
    except PortfolioLedgerValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


@router.put("/transactions/{transaction_id}", response_model=PortfolioTransactionOut)
async def correct_transaction(
    transaction_id: str,
    request: PortfolioTransactionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    service: PortfolioLedgerService = Depends(get_portfolio_ledger_service),
    universe: Universe = Depends(get_universe),
    recommendation_repo: CosmosRepository = Depends(get_recommendation_repo),
) -> PortfolioTransactionOut:
    try:
        await _validate_recommendation_attribution(
            request,
            user,
            universe,
            recommendation_repo,
        )
        row = await service.correct_transaction(
            user.user_id,
            transaction_id,
            request.model_dump(mode="json"),
        )
        return _transaction_out({**row, "status": "EFFECTIVE"})
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found") from exc
    except PortfolioLedgerValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


@router.delete("/transactions/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
async def void_transaction(
    transaction_id: str,
    client_request_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    service: PortfolioLedgerService = Depends(get_portfolio_ledger_service),
) -> None:
    try:
        await service.void_transaction(user.user_id, transaction_id, client_request_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found") from exc
    except PortfolioLedgerValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
