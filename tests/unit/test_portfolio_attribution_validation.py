from datetime import date

import pytest
from fastapi import HTTPException

from auspex.api.auth import AuthenticatedUser
from auspex.api.routes.portfolio import _validate_recommendation_attribution
from auspex.api.schemas import PortfolioTransactionRequest
from auspex.config.loader import Universe
from auspex.models.enums import Action, FilerProfile
from auspex.models.policy import Recommendation
from auspex.models.security import Security
from tests.unit.conftest import FakeCosmosRepository

SECURITY = Security(
    id="sec-amd",
    ticker="AMD",
    cik="0000002488",
    name="Advanced Micro Devices",
    cohort="semi-compute",
    filer_profile=FilerProfile.DOMESTIC,
)
UNIVERSE = Universe(securities=[SECURITY])
USER = AuthenticatedUser(user_id="owner-1", claims={})


def request(recommendation_id: str) -> PortfolioTransactionRequest:
    return PortfolioTransactionRequest(
        client_request_id="request-1",
        transaction_type="SELL",
        event_date=date(2026, 8, 12),
        currency="USD",
        security_code="AMD",
        quantity="10",
        price="200",
        followed_auspex=True,
        recommendation_id=recommendation_id,
    )


def recommendation(action: Action = Action.TRIM) -> Recommendation:
    return Recommendation(
        id="owner-1:sec-amd:2026-08-12",
        user_id="owner-1",
        security_id="sec-amd",
        as_of_date=date(2026, 8, 12),
        action=action,
        config_version_id="cfg",
    )


@pytest.mark.asyncio
async def test_accepts_matching_owner_ticker_and_action() -> None:
    row = recommendation()
    await _validate_recommendation_attribution(
        request(row.id),
        USER,
        UNIVERSE,
        FakeCosmosRepository([row]),
    )


@pytest.mark.asyncio
async def test_rejects_incompatible_recommendation_action() -> None:
    row = recommendation(Action.BUY)
    with pytest.raises(HTTPException, match="does not match"):
        await _validate_recommendation_attribution(
            request(row.id),
            USER,
            UNIVERSE,
            FakeCosmosRepository([row]),
        )
