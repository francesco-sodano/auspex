from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import httpx

from auspex.api.chat_grounding import ChatGrounding
from auspex.api.fundamentals import provider_fundamentals
from auspex.collectors.company_overview_collector import refresh_company_overviews
from auspex.config.loader import Universe
from auspex.models.company_overview import CompanyOverviewSnapshot
from auspex.models.conversation import RetrievalPlan
from auspex.models.security import Security
from auspex.persistence.memory import (
    InMemoryBlobSink,
    InMemoryDocumentSink,
    InMemoryFundamentalSink,
    InMemoryFxSink,
    InMemoryPriceSink,
    InMemoryRepository,
    InMemoryWatermarkStore,
)
from auspex.pipeline.context import PipelineContext, PipelineProviders, PipelineRepos
from auspex.pipeline.manifest import new_manifest
from auspex.pipeline.steps import step_collect_fundamentals

NOW = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
ASML = Security(
    id="asml", ticker="ASML", cik="0000937966", name="ASML HOLDING NV",
    cohort="semi-cap-equipment", filer_profile="FPI",
)


def snapshot():
    return CompanyOverviewSnapshot(
        id=ASML.id, security_id=ASML.id, ticker="ASML", retrieved_at=NOW,
        latest_quarter=date(2026, 6, 30), quote_currency="USD", financial_currency="EUR",
        metrics={
            "RevenueTTM": "35327500000", "GrossProfitTTM": "18629200000",
            "QuarterlyRevenueGrowthYOY": "0.213", "OperatingMarginTTM": "0.371",
            "ProfitMargin": "0.301", "ReturnOnEquityTTM": "0.539", "PERatio": "57.81",
            "EVToRevenue": "15.26", "EVToEBITDA": "45.87",
        },
    )


def test_asml_overview_uses_supplied_ratios_and_verified_reporting_currency():
    metrics, context = provider_fundamentals(snapshot(), now=NOW)
    by_label = {metric.label: metric for metric in metrics}
    assert by_label["Revenue (TTM)"].value == "EUR 35.33B"
    assert by_label["Gross profit (TTM)"].value == "EUR 18.63B"
    assert by_label["Revenue growth (YoY)"].value == "21.3%"
    assert by_label["Revenue growth (YoY)"].detail == "Provider's latest reporting period versus a year earlier"
    assert by_label["Operating margin (TTM)"].value == "37.1%"
    assert by_label["Profit margin (TTM)"].value == "30.1%"
    assert by_label["Return on equity (TTM)"].value == "53.9%"
    assert by_label["P / E (TTM)"].value == "57.81x"
    assert by_label["EV / Revenue"].value == "15.26x"
    assert by_label["EV / EBITDA"].value == "45.87x"
    assert all(metric.period_end == date(2026, 6, 30) for metric in metrics)
    assert context.retrieved_at == NOW
    assert context.source == "Alpha Vantage"


def test_quote_currency_does_not_silently_label_financial_amounts():
    item = snapshot().model_copy(update={"financial_currency": None})
    metrics, _ = provider_fundamentals(item, now=NOW)
    revenue = next(metric for metric in metrics if metric.label == "Revenue (TTM)")
    assert revenue.value is None
    assert "currency" in revenue.detail
    assert next(metric for metric in metrics if metric.label == "P / E (TTM)").value == "57.81x"


def test_provider_snapshot_cannot_be_backdated_to_its_latest_financial_quarter():
    metrics, context = provider_fundamentals(snapshot(), now=NOW, requested_date=date(2026, 6, 30))
    assert context.status == "unavailable"
    assert context.retrieved_at is None
    assert "not backdated" in context.note
    assert all(metric.value is None for metric in metrics)


def test_stale_snapshot_remains_visible_with_explicit_warning():
    metrics, context = provider_fundamentals(snapshot(), now=NOW + timedelta(days=3))
    assert context.status == "stale"
    assert "refresh is overdue" in context.note
    assert metrics[0].value == "EUR 35.33B"


async def test_refresh_is_resumable_and_keeps_existing_success_on_provider_failure(caplog):
    repository = InMemoryRepository[CompanyOverviewSnapshot]()
    item = snapshot().model_copy(update={"retrieved_at": NOW - timedelta(days=3)})
    await repository.upsert(item)
    provider = AsyncMock()
    request = httpx.Request("GET", "https://provider.example/query?apikey=do-not-log")
    response = httpx.Response(500, request=request)
    provider.get_company_overview.side_effect = httpx.HTTPStatusError(
        "secret=do-not-log", request=request, response=response
    )
    result = await refresh_company_overviews([ASML], provider, repository, clock=lambda: NOW)
    assert result.failed_tickers == ["ASML"]
    assert repository.all() == [item]
    assert "refresh failed for ASML" in caplog.text
    assert "do-not-log" not in caplog.text
    provider.get_company_overview.side_effect = None
    provider.get_company_overview.return_value = snapshot()
    result = await refresh_company_overviews([ASML], provider, repository, clock=lambda: NOW)
    assert result.refreshed == 1
    result = await refresh_company_overviews([ASML], provider, repository, clock=lambda: NOW)
    assert result.cached == 1
    assert provider.get_company_overview.await_count == 2
    assert len(repository.all()) == 1
    result = await refresh_company_overviews([ASML], provider, repository, force=True, clock=lambda: NOW)
    assert result.refreshed == 1
    assert result.cached == 0
    assert provider.get_company_overview.await_count == 3
    assert len(repository.all()) == 1


async def test_refresh_rejects_cross_issuer_result(caplog):
    repository = InMemoryRepository[CompanyOverviewSnapshot]()
    provider = AsyncMock()
    provider.get_company_overview.return_value = snapshot().model_copy(update={"security_id": "other"})
    result = await refresh_company_overviews([ASML], provider, repository, clock=lambda: NOW)
    assert result.failed_tickers == ["ASML"]
    assert not repository.all()
    assert "ASML" in caplog.text


async def test_discussion_uses_the_same_current_provider_values_but_not_for_past_dates(monkeypatch):
    repository = InMemoryRepository[CompanyOverviewSnapshot]()
    await repository.upsert(snapshot())
    monkeypatch.setattr("auspex.api.chat_grounding.get_company_overview_repo", lambda: repository)
    monkeypatch.setattr("auspex.api.chat_grounding.utc_now", lambda: NOW)
    grounding = ChatGrounding(Universe(securities=[ASML]))
    current = await grounding.fundamentals(
        RetrievalPlan(securities=["ASML"], date_range_end=NOW.date()), "owner"
    )
    assert len(current) == 1
    assert current[0].content["provider_context"]["source"] == "Alpha Vantage"
    pe = next(metric for metric in current[0].content["metrics"] if metric["label"] == "P / E (TTM)")
    assert pe["value"] == "57.81x"
    past = await grounding.fundamentals(
        RetrievalPlan(securities=["ASML"], date_range_end=NOW.date() - timedelta(days=1)), "owner"
    )
    assert len(past) == 1
    assert past[0].content["provider_context"]["status"] == "unavailable"
    assert all(metric["value"] is None for metric in past[0].content["metrics"])
    assert "57.81" not in str(past[0].content)


async def test_nightly_refresh_is_separate_from_sec_history_and_never_runs_for_historical_replay(monkeypatch):
    monkeypatch.setattr("auspex.pipeline.steps.utc_now", lambda: NOW)
    repository = InMemoryRepository[CompanyOverviewSnapshot]()
    provider = AsyncMock()
    provider.get_company_overview.return_value = snapshot()
    repos = PipelineRepos(
        document_sink=InMemoryDocumentSink(), fundamental_sink=InMemoryFundamentalSink(),
        price_sink=InMemoryPriceSink(), fx_sink=InMemoryFxSink(), blob_sink=InMemoryBlobSink(),
        watermarks=InMemoryWatermarkStore(), company_overview_repo=repository,
    )
    context = PipelineContext(
        universe=Universe(securities=[ASML]), config={}, as_of_date=NOW.date(), user_id="owner", repos=repos,
        providers=PipelineProviders(company_overview_provider=provider),
    )
    manifest = new_manifest(NOW.date())
    await step_collect_fundamentals(context, manifest)
    assert manifest.step_by_name("COLLECT_FUNDAMENTALS").status == "SUCCESS"
    assert len(repository.all()) == 1
    assert repos.fundamental_sink.all() == []
    assert not context.degraded_securities
    context.as_of_date = date(2025, 12, 31)
    await step_collect_fundamentals(context, new_manifest(context.as_of_date))
    assert provider.get_company_overview.await_count == 1
