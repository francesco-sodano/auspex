"""Unit tests for the API route surface: unauthenticated `/healthz` liveness
probe vs. Entra-token-gated `/api/*` routes (arc42 §7, F-16)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from auspex.api import create_app
from auspex.settings import get_settings

from .conftest import lifecycle_overrides, make_app_user


def make_client() -> TestClient:
    app = create_app()
    return TestClient(app)


class TestHealthz:
    def test_healthz_is_unauthenticated_and_returns_ok(self):
        client = make_client()
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_healthz_is_not_under_api_prefix(self):
        app = create_app()
        paths = app.openapi()["paths"]
        assert "/healthz" in paths
        assert "/api/healthz" not in paths


class TestPublicAuthConfiguration:
    def test_auth_config_is_unauthenticated_and_runtime_driven(self, monkeypatch):
        monkeypatch.setenv("AUSPEX_ENTRA_AUDIENCE", "client-id")
        monkeypatch.setenv(
            "AUSPEX_ENTRA_AUTHORITY",
            "https://login.microsoftonline.com/tenant-id",
        )
        get_settings.cache_clear()
        try:
            response = make_client().get("/auth-config.json")
            assert response.status_code == 200
            body = response.json()
            assert body["client_id"] == "client-id"
            assert body["authority"] == "https://login.microsoftonline.com/tenant-id"
            # A workforce authority is implicitly trusted by MSAL.
            assert body["known_authorities"] == []
        finally:
            get_settings.cache_clear()

    def test_auth_config_advertises_an_external_tenant_authority(self, monkeypatch):
        """An Entra External ID host must be declared or MSAL refuses it.

        This is what lets friends sign in with a personal Gmail address, so
        the SPA has to receive the ciamlogin.com host at runtime.
        """

        monkeypatch.setenv("AUSPEX_ENTRA_AUDIENCE", "client-id")
        monkeypatch.setenv(
            "AUSPEX_ENTRA_AUTHORITY", "https://auspexfriends.ciamlogin.com/tenant-id"
        )
        get_settings.cache_clear()
        try:
            body = make_client().get("/auth-config.json").json()
            assert body["known_authorities"] == ["auspexfriends.ciamlogin.com"]
        finally:
            get_settings.cache_clear()

    def test_auth_config_fails_closed_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("AUSPEX_ENTRA_AUDIENCE", raising=False)
        monkeypatch.delenv("AUSPEX_ENTRA_AUTHORITY", raising=False)
        get_settings.cache_clear()
        try:
            response = make_client().get("/auth-config.json")
            assert response.status_code == 503
        finally:
            get_settings.cache_clear()


class TestApiRoutesRequireAuth:
    def test_api_health_requires_auth(self):
        client = make_client()
        response = client.get("/api/health")
        assert response.status_code == 401

    def test_api_health_succeeds_with_valid_token(self):
        app = create_app()
        app.dependency_overrides.update(lifecycle_overrides(make_app_user("owner")))
        client = TestClient(app)
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_every_api_route_rejects_unauthenticated_requests(self):
        client = make_client()
        app = client.app
        for path, methods in app.openapi()["paths"].items():
            if not path.startswith("/api"):
                continue
            concrete_path = path.replace("{ticker}", "NVDA").replace("{as_of_date}", "2026-08-08")
            if "get" in methods:
                response = client.get(concrete_path)
                assert response.status_code == 401, f"GET {path} should require auth"
            if "post" in methods:
                response = client.post(concrete_path, json={"question": "x"})
                assert response.status_code == 401, f"POST {path} should require auth"

    def test_scores_route_is_under_api_prefix(self):
        app = create_app()
        assert "/api/scores/{ticker}/{as_of_date}" in app.openapi()["paths"]

    def test_lifecycle_surface_uses_only_canonical_routes(self):
        paths = create_app().openapi()["paths"]
        for path in (
            "/api/session/register",
            "/api/session/status",
            "/api/onboarding/initial-portfolio",
            "/api/account/deletion",
        ):
            assert path in paths
        for removed in (
            "/api/registration",
            "/api/registration/status",
            "/api/onboarding/portfolio",
            "/api/account/deletion-request",
            "/api/account/deletion-status",
        ):
            assert removed not in paths


class TestCors:
    def test_local_development_origin_receives_preflight_headers(self):
        response = make_client().options(
            "/api/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )

        assert response.status_code == 200
        assert (
            response.headers["access-control-allow-origin"]
            == "http://localhost:5173"
        )
