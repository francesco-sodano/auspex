from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import get_user_settings_repo
from auspex.api.routes import account
from tests.unit.conftest import FakeCosmosRepository, make_router_app


def make_client(repo=None, authed=True):
    overrides = {get_user_settings_repo: lambda: repo or FakeCosmosRepository()}
    if authed:
        overrides[get_current_user] = lambda: AuthenticatedUser(
            user_id="owner-1",
            claims={},
        )
    return make_router_app(account.router, overrides)


def valid_settings():
    return {
        "risk_profile": "CONSERVATIVE",
        "cash_reserve_chf": "5000",
        "investment_horizon": "LONG_TERM",
        "investment_objective": "CAPITAL_GROWTH",
        "directional_only_acknowledged": True,
        "no_guarantee_acknowledged": True,
        "not_financial_advice_acknowledged": True,
        "market_loss_acknowledged": True,
        "independent_decision_acknowledged": True,
    }


def test_settings_require_authentication() -> None:
    assert make_client(authed=False).get("/api/account/settings").status_code == 401


def test_get_returns_safe_unsaved_moderate_defaults() -> None:
    response = make_client().get("/api/account/settings")

    assert response.status_code == 200
    assert response.json()["risk_profile"] == "MODERATE"
    assert response.json()["cash_reserve_chf"] == "3000"
    assert response.json()["investment_horizon"] == "LONG_TERM"
    assert response.json()["investment_objective"] == "CAPITAL_GROWTH"
    assert response.json()["directional_only_acknowledged"] is False


def test_put_requires_every_acknowledgement() -> None:
    payload = valid_settings()
    payload["no_guarantee_acknowledged"] = False

    response = make_client().put("/api/account/settings", json=payload)

    assert response.status_code == 422


def test_put_persists_owner_scoped_settings() -> None:
    repo = FakeCosmosRepository()
    response = make_client(repo).put("/api/account/settings", json=valid_settings())

    assert response.status_code == 200
    assert response.json()["risk_profile"] == "CONSERVATIVE"
    assert repo.upserted[-1].user_id == "owner-1"
    assert repo.upserted[-1].acknowledged_at is not None


def test_configuration_exposes_read_only_themes_and_cohorts() -> None:
    response = make_client().get("/api/account/settings/configuration")

    assert response.status_code == 200
    body = response.json()
    assert len(body["themes"]) >= 15
    platform = next(
        cohort
        for cohort in body["cohorts"]
        if cohort["id"] == "large-cap-digital-platforms"
    )
    assert platform["parent"] == "digital-platforms"
    assert len(platform["tickers"]) == 12
