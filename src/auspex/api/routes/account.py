"""Owner account preferences and decision-support acknowledgements."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import get_user_settings_repo
from auspex.config.loader import load_cohorts, load_taxonomy, load_universe
from auspex.models.common import utc_now
from auspex.models.user_settings import (
    InvestmentHorizon,
    InvestmentObjective,
    RiskProfile,
    UserSettings,
    migrate_investment_horizon,
)
from auspex.persistence.repositories import CosmosRepository

router = APIRouter(prefix="/account/settings", tags=["account"])


class UserSettingsRequest(BaseModel):
    risk_profile: RiskProfile
    cash_reserve_chf: str
    investment_horizon: InvestmentHorizon
    investment_objective: InvestmentObjective
    directional_only_acknowledged: bool
    no_guarantee_acknowledged: bool
    not_financial_advice_acknowledged: bool
    market_loss_acknowledged: bool
    independent_decision_acknowledged: bool

    @field_validator("investment_horizon", mode="before")
    @classmethod
    def _accept_legacy_horizon(cls, value: object) -> object:
        """Keep older clients working across the five-band horizon split."""

        return migrate_investment_horizon(value)


def default_user_settings(user_id: str, now: datetime | None = None) -> UserSettings:
    return UserSettings(id=user_id, user_id=user_id, updated_at=now or utc_now())


@router.get("", response_model=UserSettings)
async def get_user_settings(
    user: AuthenticatedUser = Depends(get_current_user),
    repo: CosmosRepository = Depends(get_user_settings_repo),
) -> UserSettings:
    return await repo.get(user.user_id, user.user_id) or default_user_settings(
        user.user_id
    )


@router.put("", response_model=UserSettings)
async def update_user_settings(
    request: UserSettingsRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    repo: CosmosRepository = Depends(get_user_settings_repo),
) -> UserSettings:
    acknowledgements = (
        request.directional_only_acknowledged,
        request.no_guarantee_acknowledged,
        request.not_financial_advice_acknowledged,
        request.market_loss_acknowledged,
        request.independent_decision_acknowledged,
    )
    if not all(acknowledgements):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="all decision-support acknowledgements are required",
        )
    now = utc_now()
    settings = UserSettings(
        id=user.user_id,
        user_id=user.user_id,
        risk_profile=request.risk_profile,
        cash_reserve_chf=request.cash_reserve_chf,
        investment_horizon=request.investment_horizon,
        investment_objective=request.investment_objective,
        directional_only_acknowledged=True,
        no_guarantee_acknowledged=True,
        not_financial_advice_acknowledged=True,
        market_loss_acknowledged=True,
        independent_decision_acknowledged=True,
        acknowledged_at=now,
        updated_at=now,
    )
    await repo.upsert(settings)
    return settings


@router.get("/configuration")
async def get_account_configuration(
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    universe = load_universe()
    cohorts = load_cohorts()
    taxonomy = load_taxonomy()
    tickers_by_cohort: dict[str, list[str]] = {}
    for security in universe.securities:
        tickers_by_cohort.setdefault(security.cohort, []).append(security.ticker)
    return {
        "themes": taxonomy["themes"],
        "cohorts": [
            {
                "id": cohort,
                "parent": config["parent"],
                "tickers": sorted(tickers_by_cohort.get(cohort, [])),
            }
            for cohort, config in cohorts["cohorts"].items()
        ],
    }
