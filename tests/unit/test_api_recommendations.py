"""Unit tests for `GET /api/recommendations` and
`POST /api/recommendations/{id}/disposition` (arc42 §11, §5.7, §8.3)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import get_recommendation_disposition_repo, get_recommendation_repo
from auspex.api.routes import recommendations
from auspex.config.loader import Universe
from auspex.models.enums import Action, DispositionStatus, FilerProfile
from auspex.models.policy import Recommendation
from auspex.models.security import Security
from auspex.settings import get_settings
from tests.unit.conftest import FakeCosmosRepository, make_router_app


def _recommendation(
    user_id: str = "owner-1",
    security_id: str = "sec-a",
    as_of: date = date(2026, 8, 8),
    *,
    decision_signature: str | None = "v1:abc123",
    suppressed: bool = False,
) -> Recommendation:
    return Recommendation(
        id=f"{user_id}:{security_id}:{as_of.isoformat()}",
        user_id=user_id,
        security_id=security_id,
        as_of_date=as_of,
        action="HOLD_NO_ACTION",
        config_version_id="cfg-1",
        decision_signature=decision_signature,
        suppressed=suppressed,
    )


def _make_client(repo=None, authed: bool = True, disposition_repo=None):
    overrides = {
        get_recommendation_repo: lambda: repo or FakeCosmosRepository(),
        get_recommendation_disposition_repo: lambda: disposition_repo or FakeCosmosRepository(),
    }
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

    def test_suppressed_decisions_are_withheld_by_default(self):
        """A decision the user already rejected must not reappear in the feed."""

        repo = FakeCosmosRepository(
            [
                _recommendation(security_id="sec-a"),
                _recommendation(security_id="sec-b", suppressed=True),
            ]
        )
        client = _make_client(repo)

        body = client.get("/api/recommendations", params={"as_of_date": "2026-08-08"}).json()

        assert [row["security_id"] for row in body] == ["sec-a"]

    def test_suppressed_decisions_remain_available_for_audit(self):
        repo = FakeCosmosRepository(
            [
                _recommendation(security_id="sec-a"),
                _recommendation(security_id="sec-b", suppressed=True),
            ]
        )
        client = _make_client(repo)

        body = client.get(
            "/api/recommendations",
            params={"as_of_date": "2026-08-08", "include_suppressed": "true"},
        ).json()

        assert {row["security_id"] for row in body} == {"sec-a", "sec-b"}


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


class TestDispositionSuppression:
    def test_rejection_records_an_indefinite_suppression_for_that_signature(self):
        recommendation = _recommendation(decision_signature="v1:sig-buy-10")
        dispositions = FakeCosmosRepository()
        client = _make_client(FakeCosmosRepository([recommendation]), disposition_repo=dispositions)

        body = client.post(
            f"/api/recommendations/{recommendation.id}/disposition",
            json={"disposition": "REJECTED"},
        ).json()

        stored = dispositions.upserted[-1]
        assert stored.id == "owner-1:sec-a"
        assert stored.user_id == "owner-1"
        assert stored.decision_signature == "v1:sig-buy-10"
        assert stored.disposition is DispositionStatus.REJECTED
        assert stored.expires_at is None
        assert body["suppressed"] is True
        assert body["suppression_reason"] == "REJECTED"

    def test_deferral_expires_after_the_configured_window(self):
        recommendation = _recommendation(decision_signature="v1:sig-buy-10")
        dispositions = FakeCosmosRepository()
        client = _make_client(FakeCosmosRepository([recommendation]), disposition_repo=dispositions)

        body = client.post(
            f"/api/recommendations/{recommendation.id}/disposition",
            json={"disposition": "DEFERRED"},
        ).json()

        stored = dispositions.upserted[-1]
        assert stored.disposition is DispositionStatus.DEFERRED
        assert stored.expires_at is not None
        window = stored.expires_at - stored.recorded_at
        assert window == timedelta(days=get_settings().deferred_disposition_days)
        assert body["suppressed"] is True
        assert body["suppression_reason"] == "DEFERRED"

    def test_acceptance_suppresses_nothing_going_forward(self):
        recommendation = _recommendation(decision_signature="v1:sig-buy-10", suppressed=True)
        dispositions = FakeCosmosRepository()
        client = _make_client(FakeCosmosRepository([recommendation]), disposition_repo=dispositions)

        body = client.post(
            f"/api/recommendations/{recommendation.id}/disposition",
            json={"disposition": "ACCEPTED"},
        ).json()

        assert body["suppressed"] is False
        assert dispositions.upserted[-1].disposition is DispositionStatus.ACCEPTED

    def test_no_suppression_is_written_without_a_signature(self):
        """A pre-signature row cannot suppress anything — there is nothing to match."""

        recommendation = _recommendation(decision_signature=None)
        dispositions = FakeCosmosRepository()
        client = _make_client(FakeCosmosRepository([recommendation]), disposition_repo=dispositions)

        client.post(
            f"/api/recommendations/{recommendation.id}/disposition",
            json={"disposition": "REJECTED"},
        )

        assert dispositions.upserted == []

    def test_dispositions_are_listed_only_for_the_caller(self):
        dispositions = FakeCosmosRepository()
        client = _make_client(disposition_repo=dispositions)

        response = client.get("/api/recommendations/dispositions")

        assert response.status_code == 200
        assert dispositions.queries[-1].partition_key == "owner-1"


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
