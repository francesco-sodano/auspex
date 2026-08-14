"""Tests for compiled SPA static serving (arc42 §7 deployment view).

Covers `auspex.api.static.mount_spa` and its wiring into `create_app`:
- `/healthz` and `/api/*` are unaffected whether or not a SPA is mounted.
- hashed `/assets/*` bundles and other real files under `web/dist` are served
  from disk.
- unknown paths outside `/api` and `/healthz` fall back to `index.html`
  (client-side routing / deep links / refresh).
- unknown paths *under* `/api` or `/healthz` still 404 instead of silently
  returning `index.html`.
- path traversal outside the compiled `web/dist` directory is rejected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auspex.api import create_app
from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.static import mount_spa


def _build_fake_dist(tmp_path: Path) -> Path:
    """Write a minimal fake `web/dist` (as Vite would produce it)."""

    dist_dir = tmp_path / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)

    (dist_dir / "index.html").write_text("<html><body>Auspex SPA</body></html>", encoding="utf-8")
    (dist_dir / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    (assets_dir / "index-abc123.js").write_text("console.log('auspex');", encoding="utf-8")
    (assets_dir / "index-abc123.css").write_text("body{color:#000}", encoding="utf-8")

    return dist_dir


class TestMountSpaNoOp:
    def test_missing_dist_dir_is_a_noop(self, tmp_path: Path):
        app = FastAPI()
        mounted = mount_spa(app, dist_dir=tmp_path / "does-not-exist")
        assert mounted is False

    def test_missing_index_html_is_a_noop(self, tmp_path: Path):
        (tmp_path / "assets").mkdir()
        mounted = mount_spa(FastAPI(), dist_dir=tmp_path)
        assert mounted is False

    def test_create_app_without_built_dist_leaves_api_and_healthz_working(self, monkeypatch, tmp_path: Path):
        # Point at a directory that exists but was never built (no index.html) —
        # mirrors what happens in local/dev checkouts and the rest of this suite.
        monkeypatch.setenv("AUSPEX_WEB_DIST_DIR", str(tmp_path))
        client = TestClient(create_app())

        assert client.get("/healthz").status_code == 200
        assert client.get("/api/health").status_code == 401
        # No SPA mounted: unmatched paths 404 rather than serving anything.
        assert client.get("/some/deep/link").status_code == 404


class TestMountSpaServesBuiltAssets:
    @pytest.fixture
    def client(self, monkeypatch, tmp_path: Path) -> TestClient:
        dist_dir = _build_fake_dist(tmp_path)
        monkeypatch.setenv("AUSPEX_WEB_DIST_DIR", str(dist_dir))
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(user_id="owner", claims={})
        return TestClient(app)

    def test_healthz_still_unauthenticated_and_ok(self, client: TestClient):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_api_routes_still_served_not_intercepted_by_spa(self, client: TestClient):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_unknown_api_path_404s_instead_of_serving_index_html(self, client: TestClient):
        response = client.get("/api/this-route-does-not-exist")
        assert response.status_code == 404
        assert "Auspex SPA" not in response.text

    def test_unknown_healthz_subpath_404s_instead_of_serving_index_html(self, client: TestClient):
        response = client.get("/healthz/extra")
        assert response.status_code == 404
        assert "Auspex SPA" not in response.text

    def test_auth_config_path_is_never_served_by_spa(self, client: TestClient):
        response = client.get("/auth-config.json/extra")
        assert response.status_code == 404
        assert "Auspex SPA" not in response.text

    def test_root_serves_index_html(self, client: TestClient):
        response = client.get("/")
        assert response.status_code == 200
        assert "Auspex SPA" in response.text
        assert response.headers["content-type"].startswith("text/html")

    def test_deep_client_side_route_falls_back_to_index_html(self, client: TestClient):
        response = client.get("/portfolio/nvda/2026-08-08")
        assert response.status_code == 200
        assert "Auspex SPA" in response.text

    def test_hashed_asset_served_with_correct_content_type(self, client: TestClient):
        response = client.get("/assets/index-abc123.js")
        assert response.status_code == 200
        assert response.text == "console.log('auspex');"
        assert "javascript" in response.headers["content-type"]

    def test_public_file_served_directly(self, client: TestClient):
        response = client.get("/favicon.svg")
        assert response.status_code == 200
        assert response.text == "<svg></svg>"

    def test_missing_asset_under_assets_mount_404s(self, client: TestClient):
        # `/assets` is served by a dedicated StaticFiles mount (hashed bundle
        # dir): a genuinely missing file there is a real 404, not silently
        # masked by the SPA fallback (that fallback is only for client-side
        # routes like `/portfolio/...`, which never live under `/assets`).
        response = client.get("/assets/does-not-exist.js")
        assert response.status_code == 404
        assert "Auspex SPA" not in response.text


class TestMountSpaPathTraversal:
    def test_escaping_dist_dir_does_not_read_arbitrary_files(self, tmp_path: Path):
        dist_dir = _build_fake_dist(tmp_path / "public_root")
        secret = tmp_path / "secret.txt"
        secret.write_text("do-not-serve-me", encoding="utf-8")

        app = FastAPI()
        mount_spa(app, dist_dir=dist_dir)
        client = TestClient(app)

        # Percent-encoded so the HTTP client does not collapse the ".." itself;
        # Starlette decodes it server-side into the literal path our guard sees.
        response = client.get("/%2e%2e/secret.txt")
        assert "do-not-serve-me" not in response.text
