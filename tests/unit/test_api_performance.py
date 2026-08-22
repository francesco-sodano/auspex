"""Unit tests for `GET /api/performance` (arc42 §5.8, §11).

`web/src/lib/types.ts`'s `PerformanceReport` shape is the contract under
test: composite IC at 21/63/126, per-leg IC, the leg correlation matrix,
suggestion hit rate, accepted/rejected dispositions, cohort dispersion, and
sample counts — assembled from flat `PerformanceMetric` rows with **no
required query parameter**.
"""

from __future__ import annotations

from datetime import date

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import (
    get_performance_repo,
    get_portfolio_ledger_service,
    get_recommendation_repo,
    get_user_performance_repo,
)
from auspex.api.routes import performance
from auspex.models.performance import PerformanceMetric
from tests.unit.conftest import FakeCosmosRepository, make_router_app


def _metric(
    metric_type: str,
    as_of: date,
    value: str,
    *,
    horizon_days: int | None = None,
    scope: str = "universe",
    sample_size: int = 10,
    detail: dict[str, str] | None = None,
    user_id: str | None = None,
) -> PerformanceMetric:
    return PerformanceMetric(
        id=f"{metric_type}:{as_of.isoformat()}:{scope}",
        metric_type=metric_type,
        as_of_date=as_of,
        horizon_days=horizon_days,
        scope=scope,
        value=value,
        sample_size=sample_size,
        detail=detail or {},
        user_id=user_id,
    )


def test_partition_key_matches_shared_and_private_metric_containers():
    as_of = date(2026, 8, 22)
    shared = _metric("composite_ic", as_of, "0.1")
    private = _metric(
        "suggestion_hit_rate",
        as_of,
        "0.5",
        user_id="user-1",
    )

    assert shared.partition_key == "composite_ic"
    assert private.partition_key == "user-1"


def _make_client(repo=None, authed: bool = True, user_repo=None):
    class EmptyLedger:
        async def list_transactions(self, _user_id):
            return []

    overrides = {
        get_performance_repo: lambda: repo or FakeCosmosRepository(),
        get_user_performance_repo: lambda: user_repo or FakeCosmosRepository(),
        get_recommendation_repo: lambda: FakeCosmosRepository(),
        get_portfolio_ledger_service: EmptyLedger,
    }
    if authed:
        overrides[get_current_user] = lambda: AuthenticatedUser(user_id="owner-1", claims={})
    return make_router_app(performance.router, overrides)


def test_requires_auth():
    client = _make_client(authed=False)
    assert client.get("/api/performance").status_code == 401


def test_requires_no_query_parameter_and_returns_200_on_an_empty_container():
    client = _make_client(FakeCosmosRepository([]))

    response = client.get("/api/performance")

    assert response.status_code == 200
    body = response.json()
    assert body["composite_ic"] == {"21": None, "63": None, "126": None}
    assert body["leg_correlation"] == {"labels": [leg for leg in body["leg_ic"]], "values": [[None] * 6] * 6}
    assert body["suggestion_hit_rate"] is None
    assert body["dispositions"] == {
        "accepted": None,
        "rejected": None,
        "accepted_sample_size": 0,
        "rejected_sample_size": 0,
    }
    assert body["sample_size"] == 0
    assert body["backfilled_sample_size"] == 0
    assert body["diagnostics"]["63"]["confidence_low"] is None


def test_surfaces_uncertainty_spread_and_benchmark_diagnostics():
    rows = [
        _metric(
            "ic_distribution",
            date(2026, 8, 20),
            "0.04",
            horizon_days=63,
            detail={
                "icir": "0.5",
                "effective_sample_size": "5.2",
            },
        ),
        _metric(
            "ic_interval",
            date(2026, 8, 20),
            "0.04",
            horizon_days=63,
            scope="moving_block_bootstrap",
            detail={
                "low": "0.01",
                "high": "0.07",
                "method": "moving_block_bootstrap",
                "confidence": "0.95",
                "excludes_zero": "true",
            },
        ),
        _metric(
            "spread",
            date(2026, 8, 20),
            "0.03",
            horizon_days=63,
            scope="top_minus_bottom",
            detail={
                "robust_spread": "0.025",
                "cost_adjusted_spread": "0.02",
                "mean_turnover": "0.1",
                "max_drawdown": "0.08",
                "outlier_count": "3",
            },
        ),
        _metric(
            "benchmark",
            date(2026, 8, 20),
            "0.015",
            horizon_days=63,
            scope="equal_weight",
        ),
        _metric(
            "benchmark",
            date(2026, 8, 20),
            "0.02",
            horizon_days=63,
            scope="momentum",
        ),
        _metric(
            "benchmark",
            date(2026, 8, 20),
            "0",
            horizon_days=63,
            scope="random_ranking",
            detail={"p95_absolute": "0.03"},
        ),
    ]
    client = _make_client(FakeCosmosRepository(rows))

    response = client.get("/api/performance")

    assert response.status_code == 200
    diagnostic = response.json()["diagnostics"]["63"]
    assert diagnostic == {
        "mean_ic": "0.04",
        "icir": "0.5",
        "effective_sample_size": "5.2",
        "confidence_low": "0.01",
        "confidence_high": "0.07",
        "confidence_method": "moving_block_bootstrap",
        "confidence_level": "0.95",
        "excludes_zero": True,
        "robust_spread": "0.025",
        "cost_adjusted_spread": "0.02",
        "mean_turnover": "0.1",
        "max_drawdown": "0.08",
        "outlier_count": 3,
        "equal_weight_return": "0.015",
        "momentum_ic": "0.02",
        "random_p95_absolute": "0.03",
    }


def test_assembles_composite_ic_across_all_three_horizons():
    rows = [
        _metric("composite_ic", date(2026, 8, 1), "0.12", horizon_days=21),
        _metric("composite_ic", date(2026, 8, 1), "0.09", horizon_days=63),
        _metric("composite_ic", date(2026, 8, 1), "0.05", horizon_days=126),
    ]
    client = _make_client(FakeCosmosRepository(rows))

    response = client.get("/api/performance")

    assert response.status_code == 200
    assert response.json()["composite_ic"] == {"21": "0.12", "63": "0.09", "126": "0.05"}


def test_uses_only_the_latest_as_of_date_per_horizon():
    rows = [
        _metric("composite_ic", date(2026, 7, 1), "0.01", horizon_days=21),
        _metric("composite_ic", date(2026, 8, 1), "0.12", horizon_days=21),
    ]
    client = _make_client(FakeCosmosRepository(rows))

    response = client.get("/api/performance")

    assert response.json()["composite_ic"]["21"] == "0.12"


def test_leg_ic_uses_the_126_session_horizon_keyed_by_leg_name():
    rows = [
        _metric("leg_ic", date(2026, 8, 1), "0.30", horizon_days=126, scope="leg:smart_money"),
        _metric("leg_ic", date(2026, 8, 1), "0.90", horizon_days=21, scope="leg:smart_money"),  # wrong horizon
    ]
    client = _make_client(FakeCosmosRepository(rows))

    response = client.get("/api/performance")

    leg_ic = response.json()["leg_ic"]
    assert leg_ic["smart_money"] == "0.30"
    assert leg_ic["thesis_linkage"] is None


def test_leg_correlation_matrix_is_symmetric_and_labelled():
    rows = [
        _metric("leg_correlation", date(2026, 8, 1), "0.62", scope="smart_moneyxthesis_linkage"),
    ]
    client = _make_client(FakeCosmosRepository(rows))

    response = client.get("/api/performance")

    body = response.json()
    labels = body["leg_correlation"]["labels"]
    values = body["leg_correlation"]["values"]
    i, j = labels.index("smart_money"), labels.index("thesis_linkage")
    assert values[i][j] == "0.62"
    assert values[j][i] == "0.62"


def test_dispositions_reads_accepted_and_rejected_scopes():
    rows = [
        _metric("disposition_outcome", date(2026, 8, 1), "0.70", scope="accepted", user_id="owner-1"),
        _metric("disposition_outcome", date(2026, 8, 1), "0.40", scope="rejected", user_id="owner-1"),
    ]
    client = _make_client(user_repo=FakeCosmosRepository(rows))

    response = client.get("/api/performance")

    assert response.json()["dispositions"] == {
        "accepted": "0.70",
        "rejected": "0.40",
        "accepted_sample_size": 10,
        "rejected_sample_size": 10,
    }


def test_private_performance_never_reads_another_user_partition():
    rows = [
        _metric(
            "suggestion_hit_rate",
            date(2026, 8, 1),
            "0.65",
            user_id="owner-1",
        ),
        _metric(
            "suggestion_hit_rate",
            date(2026, 8, 1),
            "0.05",
            user_id="other-user",
        ),
    ]

    body = _make_client(
        user_repo=FakeCosmosRepository(rows)
    ).get("/api/performance").json()

    assert body["suggestion_hit_rate"] == "0.65"


def test_cohort_dispersion_strips_the_cohort_prefix():
    rows = [_metric("cohort_quality", date(2026, 8, 1), "0.18", scope="cohort:semiconductors")]
    client = _make_client(FakeCosmosRepository(rows))

    response = client.get("/api/performance")

    assert response.json()["cohort_dispersion"] == {"semiconductors": "0.18"}


def test_sample_size_and_backfilled_sample_size_come_from_the_hit_rate_row():
    rows = [
        _metric(
            "suggestion_hit_rate",
            date(2026, 8, 1),
            "0.65",
            horizon_days=126,
            sample_size=42,
            detail={"backfilled_sample_size": "7"},
            user_id="owner-1",
        )
    ]
    client = _make_client(user_repo=FakeCosmosRepository(rows))

    response = client.get("/api/performance")

    body = response.json()
    assert body["suggestion_hit_rate"] == "0.65"
    assert body["sample_size"] == 42
    assert body["backfilled_sample_size"] == 7
