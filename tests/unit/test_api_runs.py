"""Unit tests for `GET /api/runs?limit=` (arc42 §11, §6.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import get_run_repo
from auspex.api.routes import runs
from auspex.models.run import RunManifest
from tests.unit.conftest import FakeCosmosRepository, make_router_app


def _run(run_date: str, run_type: str, started_at: datetime) -> RunManifest:
    return RunManifest(id=f"{run_date}:{run_type}", run_date=run_date, run_type=run_type, started_at=started_at)


def _make_client(repo=None, authed: bool = True):
    overrides = {get_run_repo: lambda: repo or FakeCosmosRepository()}
    if authed:
        overrides[get_current_user] = lambda: AuthenticatedUser(user_id="owner-1", claims={})
    return make_router_app(runs.router, overrides)


def test_requires_auth():
    client = _make_client(authed=False)
    response = client.get("/api/runs")
    assert response.status_code == 401


def test_returns_most_recent_runs_first():
    base = datetime(2026, 8, 1, tzinfo=UTC)
    manifests = [
        _run("2026-08-01", "nightly", base),
        _run("2026-08-03", "nightly", base + timedelta(days=2)),
        _run("2026-08-02", "nightly", base + timedelta(days=1)),
    ]
    client = _make_client(FakeCosmosRepository(manifests))

    response = client.get("/api/runs")

    assert response.status_code == 200
    body = response.json()
    assert [row["run_date"] for row in body] == ["2026-08-03", "2026-08-02", "2026-08-01"]


def test_honours_the_limit_query_param():
    base = datetime(2026, 8, 1, tzinfo=UTC)
    manifests = [_run(f"2026-08-0{i}", "nightly", base + timedelta(days=i)) for i in range(1, 6)]
    client = _make_client(FakeCosmosRepository(manifests))

    response = client.get("/api/runs", params={"limit": 2})

    assert response.status_code == 200
    assert len(response.json()) == 2


def test_rejects_a_limit_outside_the_allowed_range():
    client = _make_client(FakeCosmosRepository([]))
    response = client.get("/api/runs", params={"limit": 0})
    assert response.status_code == 422
