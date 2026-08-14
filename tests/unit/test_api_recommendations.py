"""Unit tests for `GET /api/recommendations` and
`POST /api/recommendations/{id}/disposition` (arc42 §11, §5.7, §8.3)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import get_recommendation_repo
from auspex.api.routes import recommendations
from auspex.config.loader import Universe
from auspex.models.enums import Action, FilerProfile
from auspex.models.policy import Recommendation
from auspex.models.security import Security
from tests.unit.conftest import FakeCosmosRepository, make_router_app


def _recommendation(
    user_id: str = "owner-1", security_id: str = "sec-a", as_of: date = date(2026, 8, 8)
) -> Recommendation:
    return Recommendation(
        id=f"{user_id}:{security_id}:{as_of.isoformat()}",
        user_id=user_id,
        security_id=security_id,
        as_of_date=as_of,
        action="HOLD_NO_ACTION",
        config_version_id="cfg-1",
    )


def _make_client(repo=None, authed: bool = True):
    overrides = {get_recommendation_repo: lambda: repo or FakeCosmosRepository()}
    if authed:
        overrides[get_current_user] = lambda: AuthenticatedUser(user_id="owner-1", claims={})
    return make_router_app(recommendations.router, overrides)


class TestListRecommendations:
    def test_requires_auth(self):
        client = _make_client(authed=False)
        response = client.get("/api/recommendations", params={"as_of_date": "2026-08-08"})
        assert response.status_code == 401

    def test_scopes_query_to_the_authenticated_user(self):
        repo = FakeCosmosRepository([_recommendation()])
        client = _make_client(repo)

        response = client.get("/api/recommendations", params={"as_of_date": "2026-08-08"})

        assert response.status_code == 200
        assert len(response.json()) == 1
        recorded = repo.queries[0]
        assert recorded.partition_key == "owner-1"
        assert {"name": "@user_id", "value": "owner-1"} in recorded.parameters


class TestSetDisposition:
    def test_requires_auth(self):
        client = _make_client(authed=False)
        response = client.post("/api/recommendations/some-id/disposition", json={"disposition": "ACCEPTED"})
        assert response.status_code == 401

    def test_records_disposition_and_persists_it(self):
        recommendation = _recommendation()
        repo = FakeCosmosRepository([recommendation])
        client = _make_client(repo)

        response = client.post(
            f"/api/recommendations/{recommendation.id}/disposition",
            json={"disposition": "ACCEPTED"},
        )

        assert response.status_code == 200
        assert response.json()["disposition"] == "ACCEPTED"
        assert repo.upserted[-1].disposition == "ACCEPTED"

    def test_404_for_unknown_recommendation(self):
        client = _make_client(FakeCosmosRepository([]))
        response = client.post("/api/recommendations/unknown-id/disposition", json={"disposition": "REJECTED"})
        assert response.status_code == 404

    def test_404_when_recommendation_belongs_to_a_different_user(self):
        """The body carries no `user_id` — ownership is enforced purely by
        the token-derived partition key never matching another owner's row
        (arc42 §11 "`user_id` derives from the token, never the request body")."""

        other_owners_recommendation = _recommendation(user_id="someone-else")
        repo = FakeCosmosRepository([other_owners_recommendation])
        client = _make_client(repo)

        response = client.post(
            f"/api/recommendations/{other_owners_recommendation.id}/disposition",
            json={"disposition": "ACCEPTED"},
        )

        assert response.status_code == 404

    def test_rejects_an_invalid_disposition_value(self):
        recommendation = _recommendation()
        repo = FakeCosmosRepository([recommendation])
        client = _make_client(repo)

        response = client.post(
            f"/api/recommendations/{recommendation.id}/disposition",
            json={"disposition": "MAYBE_LATER"},
        )

        assert response.status_code == 422


@pytest.mark.asyncio
async def test_history_only_returns_actionable_last_three_days() -> None:
    today = date.today()
    rows = [
        _recommendation(as_of=today).model_copy(update={"action": Action.HOLD_NO_ACTION}),
        _recommendation(as_of=today - timedelta(days=1)).model_copy(update={"action": Action.TRIM}),
        _recommendation(as_of=today - timedelta(days=5)).model_copy(update={"action": Action.BUY}),
    ]
    universe = Universe(
        securities=[
            Security(
                id="sec-a",
                ticker="AAA",
                cik="0000000001",
                name="Alpha",
                cohort="tech",
                filer_profile=FilerProfile.DOMESTIC,
            )
        ]
    )

    class EmptyLedger:
        async def list_transactions(self, _user_id):
            return []

    history = await recommendations.recommendation_history(
        "sec-a",
        AuthenticatedUser(user_id="owner-1", claims={}),
        universe,
        FakeCosmosRepository(rows),
        FakeCosmosRepository(),
        EmptyLedger(),
    )

    assert len(history) == 1
    assert history[0].action == Action.TRIM
    assert history[0].as_of_date == today - timedelta(days=1)
