"""Unit tests for `_performance_command`'s wiring (arc42 §5.8): repository/context
construction and the reused `BootstrapRunner.compute_performance_metrics` call
(arc42 §6.3 step 10), which this weekly job invokes with no explicit
`scored_dates` so it recomputes over every date already present in `scores`.

`BootstrapRunner`/`compute_performance_metrics`'s own IC-computation logic is
exercised elsewhere (`test_bootstrap_pure_helpers.py`, the bootstrap-run
integration tests); here `BootstrapRunner` is faked out so these tests stay
fast/deterministic and focus purely on `_performance_command`'s own
orchestration: which repos/context it builds and how it reacts to the
metrics `compute_performance_metrics` returns.

The command reads the portfolio ledger only to collect recommendation IDs from
transactions explicitly marked as following Auspex; performance metrics still
write to the non-user-partitioned `performance` container.
"""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from auspex.cli.main import _performance_command
from auspex.config.loader import Universe
from auspex.models.enums import FilerProfile
from auspex.models.performance import PerformanceMetric
from auspex.models.security import Security


def make_universe() -> Universe:
    return Universe(
        securities=[
            Security(
                id="sec-nvda",
                ticker="NVDA",
                cik="0001045810",
                name="NVIDIA",
                cohort="semi-compute",
                filer_profile=FilerProfile.DOMESTIC,
                investable=True,
            )
        ]
    )


def make_metric(as_of: date) -> PerformanceMetric:
    return PerformanceMetric(
        id=f"composite_ic:{as_of.isoformat()}:universe",
        metric_type="composite_ic",
        as_of_date=as_of,
        horizon_days=21,
        scope="universe",
        value=str(Decimal("0.05")),
        sample_size=85,
    )


class FakeBootstrapRunnerForPerformance:
    """Replaces `auspex.cli.bootstrap.BootstrapRunner` so tests exercise only
    `_performance_command`'s wiring, not the real cross-sectional IC
    computation `compute_performance_metrics` performs."""

    last_instance: FakeBootstrapRunnerForPerformance | None = None

    def __init__(self, *, universe, context_factory) -> None:
        self.universe = universe
        self.context_factory = context_factory
        self.compute_performance_metrics_kwargs: dict | None = None
        self.metrics_to_return: list[PerformanceMetric] = []
        FakeBootstrapRunnerForPerformance.last_instance = self

    async def compute_performance_metrics(
        self,
        ctx,
        scored_dates=None,
        performance_repo=None,
        accepted_recommendation_ids=None,
        attribution_user_id=None,
        include_recommendation_metrics=True,
    ):
        self.compute_performance_metrics_kwargs = {
            "ctx": ctx,
            "scored_dates": scored_dates,
            "performance_repo": performance_repo,
            "accepted_recommendation_ids": accepted_recommendation_ids,
            "attribution_user_id": attribution_user_id,
            "include_recommendation_metrics": include_recommendation_metrics,
        }
        return self.metrics_to_return


def patch_common(
    monkeypatch, *, runner_cls=FakeBootstrapRunnerForPerformance
) -> list[str]:
    closed_resources: list[str] = []

    class FakeContext:
        def __init__(self, name: str) -> None:
            self.name = name

        async def aclose(self) -> None:
            closed_resources.append(self.name)

    class FakePortfolioAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def read_transactions(self):
            return []

        async def resolve_owner_user_sk(self):
            return "owner-sk"

    monkeypatch.setattr("auspex.config.load_universe", make_universe)
    monkeypatch.setattr(
        "auspex.persistence.cosmos_client.get_cosmos_context",
        lambda: FakeContext("cosmos"),
    )
    monkeypatch.setattr(
        "auspex.persistence.cosmos_client.get_source_ledger_context",
        lambda: FakeContext("source_ledger"),
    )
    monkeypatch.setattr(
        "auspex.persistence.blob_client.get_blob_context",
        lambda: FakeContext("blob"),
    )
    monkeypatch.setattr("auspex.portfolio.adapter.PortfolioAdapter", FakePortfolioAdapter)
    monkeypatch.setattr("auspex.portfolio.mapping.load_portfolio_mapping", lambda: object())
    monkeypatch.setattr("auspex.cli.bootstrap.BootstrapRunner", runner_cls)

    async def no_active_users(_cosmos):
        return []

    main_module = importlib.import_module("auspex.cli.main")
    monkeypatch.setattr(main_module, "_resolve_active_users", no_active_users)
    return closed_resources


class TestPerformanceCommandWiring:
    @pytest.mark.asyncio
    async def test_returns_zero_and_calls_compute_performance_metrics_with_no_explicit_dates(self, monkeypatch):
        FakeBootstrapRunnerForPerformance.last_instance = None
        closed_resources = patch_common(monkeypatch)

        as_of = date(2026, 8, 8)
        result = await _performance_command(as_of)

        assert result == 0
        runner = FakeBootstrapRunnerForPerformance.last_instance
        assert runner is not None
        assert runner.compute_performance_metrics_kwargs is not None
        # scored_dates must be omitted/None: this job recomputes over the
        # full history already in `scores`, it has no replay window of its
        # own to pass (arc42 §5.8).
        assert runner.compute_performance_metrics_kwargs["scored_dates"] is None
        assert runner.compute_performance_metrics_kwargs["performance_repo"] is not None
        assert runner.compute_performance_metrics_kwargs["accepted_recommendation_ids"] is None
        assert runner.compute_performance_metrics_kwargs["ctx"].as_of_date == as_of
        assert runner.compute_performance_metrics_kwargs["attribution_user_id"] is None
        assert runner.compute_performance_metrics_kwargs["include_recommendation_metrics"] is False
        assert set(closed_resources) == {
            "cosmos",
            "source_ledger",
            "blob",
        }

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_metrics_computed_yet(self, monkeypatch):
        """An empty `scores` history (e.g. before bootstrap/nightly have ever
        run) is a legitimate non-error state for this job, not a failure."""

        FakeBootstrapRunnerForPerformance.last_instance = None
        patch_common(monkeypatch)

        result = await _performance_command(date(2026, 8, 8))

        assert result == 0
        runner = FakeBootstrapRunnerForPerformance.last_instance
        assert runner is not None
        assert runner.compute_performance_metrics_kwargs is not None

    @pytest.mark.asyncio
    async def test_returns_zero_when_metrics_are_computed(self, monkeypatch):
        class FakeRunnerWithMetrics(FakeBootstrapRunnerForPerformance):
            def __init__(self, *, universe, context_factory) -> None:
                super().__init__(universe=universe, context_factory=context_factory)
                self.metrics_to_return = [make_metric(date(2026, 8, 8))]

        FakeBootstrapRunnerForPerformance.last_instance = None
        patch_common(monkeypatch, runner_cls=FakeRunnerWithMetrics)

        result = await _performance_command(date(2026, 8, 8))

        assert result == 0
        runner = FakeBootstrapRunnerForPerformance.last_instance
        assert runner is not None
        assert len(runner.metrics_to_return) == 1

    @pytest.mark.asyncio
    async def test_reads_followed_transaction_attribution(self, monkeypatch):
        """The performance job passes linked recommendation IDs into outcome measurement."""

        FakeBootstrapRunnerForPerformance.last_instance = None
        patch_common(monkeypatch)

        class AttributedPortfolioAdapter:
            def __init__(self, *_args, **_kwargs):
                pass

            async def read_transactions(self):
                return [
                    SimpleNamespace(
                        transaction_id="transaction-1",
                        corrects_transaction_id=None,
                        linked_transaction_id=None,
                        transaction_type="BUY",
                        created_at="2026-08-08T00:00:00Z",
                        followed_auspex=True,
                        recommendation_id="recommendation-1",
                    )
                ]

        monkeypatch.setattr(
            "auspex.portfolio.adapter.PortfolioAdapter",
            AttributedPortfolioAdapter,
        )
        monkeypatch.setattr(
            "auspex.portfolio.event_ledger.effective_transactions",
            lambda rows: rows,
        )

        async def one_active_user(_cosmos):
            return [("owner-sk", "owner-sk")]

        monkeypatch.setattr(
            importlib.import_module("auspex.cli.main"),
            "_resolve_active_users",
            one_active_user,
        )
        fenced_users = []

        class FakeUserService:
            def __init__(self, **kwargs):
                pass

            @asynccontextmanager
            async def user_operation(self, user_id, *, require_active):
                assert require_active is True
                fenced_users.append(user_id)
                yield

        monkeypatch.setattr(
            "auspex.users.service.AppUserService",
            FakeUserService,
        )

        result = await _performance_command(date(2026, 8, 8))

        assert result == 0
        runner = FakeBootstrapRunnerForPerformance.last_instance
        assert runner is not None
        assert runner.compute_performance_metrics_kwargs is not None
        assert runner.compute_performance_metrics_kwargs[
            "accepted_recommendation_ids"
        ] == {"recommendation-1"}
        assert fenced_users == ["owner-sk"]
