"""The per-user block projects the portfolio *before* the policy cascade.

arc42 §6.1 documents the per-user order as project, then policy, then assert.
In code the named steps ran ``RUN_POLICY -> ASSERT -> PROJECT_PORTFOLIO`` and
the projection was computed as a side effect *inside* the policy step, with the
later ``PROJECT_PORTFOLIO`` merely persisting the cached result. The sequence of
effects was right; the sequence of step names told a reader the book was valued
after the trades had been decided.

Reordering the names must not turn one ledger read into two — these tests pin
both halves: the order, and the single read.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from auspex.models.enums import FilerProfile
from auspex.models.market import FxRate, PriceBar
from auspex.models.run import PIPELINE_STEPS
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
from auspex.pipeline.steps import _get_portfolio_projection, step_project_portfolio
from auspex.portfolio.port import Holding, PortfolioSnapshot

AS_OF = date(2026, 8, 20)


class _Security:
    id = "sec-1"
    ticker = "NVDA"
    cohort = "c"
    filer_profile = FilerProfile.DOMESTIC


class _Universe:
    securities = [_Security()]


class CountingReader:
    """Counts how often the read-only ledger binding is actually consulted."""

    def __init__(self) -> None:
        self.reads = 0

    async def read_snapshot(self, as_of, fx_rate_to_chf):
        self.reads += 1
        return PortfolioSnapshot(
            holdings=[Holding(ticker="NVDA", quantity=Decimal("10"))],
            cash_chf=Decimal("1000"),
            as_of=as_of,
            lot_level=False,
        )


@pytest.fixture
def context() -> tuple[PipelineContext, CountingReader]:
    price_sink = InMemoryPriceSink()
    price_sink._bars["sec-1:2026-08-20"] = PriceBar(
        id="sec-1:2026-08-20",
        security_id="sec-1",
        session_date=AS_OF,
        open_raw="100",
        high_raw="100",
        low_raw="100",
        close_raw="100",
        volume=1_000,
        close_adjusted="100",
    )
    fx_sink = InMemoryFxSink()
    fx_sink._rates["USDCHF:2026-08-20"] = FxRate(
        id="USDCHF:2026-08-20", pair="USDCHF", session_date=AS_OF, close_rate="0.88"
    )

    reader = CountingReader()
    repos = PipelineRepos(
        document_sink=InMemoryDocumentSink(),
        price_sink=price_sink,
        fx_sink=fx_sink,
        fundamental_sink=InMemoryFundamentalSink(),
        blob_sink=InMemoryBlobSink(),
        watermarks=InMemoryWatermarkStore(),
        portfolio_projection_repo=InMemoryRepository(),
    )
    ctx = PipelineContext(
        universe=_Universe(),
        config={"policy": {}},
        as_of_date=AS_OF,
        user_id="owner",
        repos=repos,
        providers=PipelineProviders(portfolio_reader=reader),
    )
    return ctx, reader


def test_the_named_steps_run_in_the_documented_order():
    assert PIPELINE_STEPS.index("WRITE_SNAPSHOT") < PIPELINE_STEPS.index("PROJECT_PORTFOLIO")
    assert PIPELINE_STEPS.index("PROJECT_PORTFOLIO") < PIPELINE_STEPS.index("RUN_POLICY")
    assert PIPELINE_STEPS.index("RUN_POLICY") < PIPELINE_STEPS.index("ASSERT")
    assert PIPELINE_STEPS.index("ASSERT") < PIPELINE_STEPS.index("NARRATE")


@pytest.mark.asyncio
async def test_projecting_first_populates_the_cache_the_policy_step_reuses(context):
    ctx, reader = context

    await step_project_portfolio(ctx, new_manifest(AS_OF))

    assert reader.reads == 1
    assert "_portfolio_projection" in ctx.__dict__

    # What RUN_POLICY does next: the same accessor, which must be a cache hit.
    projection, snapshot = await _get_portfolio_projection(ctx)

    assert reader.reads == 1
    assert projection is ctx.__dict__["_portfolio_projection"]
    assert snapshot is ctx.__dict__["_portfolio_snapshot"]


@pytest.mark.asyncio
async def test_the_projection_row_is_still_written(context):
    ctx, _ = context

    manifest = new_manifest(AS_OF)
    await step_project_portfolio(ctx, manifest)

    rows = ctx.repos.portfolio_projection_repo.all()
    assert [row.id for row in rows] == [f"owner:{AS_OF.isoformat()}"]
    assert manifest.step_by_name("PROJECT_PORTFOLIO").status == "SUCCESS"


@pytest.mark.asyncio
async def test_a_skipped_projection_still_leaves_the_policy_step_able_to_project(context):
    """No projection repository configured must not blank the book."""

    ctx, reader = context
    ctx.repos.portfolio_projection_repo = None

    manifest = new_manifest(AS_OF)
    await step_project_portfolio(ctx, manifest)
    assert manifest.step_by_name("PROJECT_PORTFOLIO").status == "SKIPPED"
    assert reader.reads == 0

    projection, _ = await _get_portfolio_projection(ctx)
    assert reader.reads == 1
    assert projection.positions
