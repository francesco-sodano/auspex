"""Pipeline execution context — all injectable dependencies for one run.

Every dependency is optional except ``universe``/``config``/``as_of_date``.
A ``None`` dependency causes its step to be marked SKIPPED with a reason
rather than FAILED (arc42 §6.1: provider failures degrade coverage, they do
not abort the run).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from auspex.collectors.base import BlobSink, DocumentSink, FundamentalSink, FxSink, PriceSink, WatermarkStore
from auspex.config.loader import Universe
from auspex.extraction.channel_a import ChannelAExtractionSink
from auspex.extraction.channel_b import ChannelBDigestSink
from auspex.narrative.generator import NarrativeSink
from auspex.portfolio.port import PortfolioPort
from auspex.providers.base import FxProvider, NewsProvider, PriceProvider
from auspex.providers.edgar import EdgarClient
from auspex.providers.openai_provider import AzureOpenAIClient


@dataclass
class PipelineRepos:
    document_sink: DocumentSink
    price_sink: PriceSink
    fx_sink: FxSink
    fundamental_sink: FundamentalSink
    blob_sink: BlobSink
    watermarks: WatermarkStore
    channel_a_sink: ChannelAExtractionSink | None = None
    channel_b_sink: ChannelBDigestSink | None = None
    narrative_sink: NarrativeSink | None = None
    score_repo: object | None = None
    leg_change_repo: object | None = None
    recommendation_repo: object | None = None
    run_repo: object | None = None
    config_version_repo: object | None = None
    portfolio_projection_repo: object | None = None
    user_settings_repo: object | None = None


@dataclass
class PipelineProviders:
    price_provider: PriceProvider | None = None
    fx_provider: FxProvider | None = None
    news_provider: NewsProvider | None = None
    edgar_client: EdgarClient | None = None
    openai_client: AzureOpenAIClient | None = None
    portfolio_reader: PortfolioPort | None = None
    """Read-only binding to the portfolio ledger (arc42 §5.7).
    Auspex only ever calls `read_snapshot` through this — never a write."""


@dataclass
class PipelineContext:
    universe: Universe
    config: dict
    as_of_date: date
    user_id: str
    repos: PipelineRepos
    providers: PipelineProviders = field(default_factory=PipelineProviders)
    hard_timeout_minutes: int = 45
    cash_chf: Decimal = field(default_factory=lambda: Decimal("4179"))

    # per-run scratch state, populated by earlier steps and consumed by later ones
    new_document_ids_by_security: dict[str, list[str]] = field(default_factory=dict)
    new_accessions_by_security: dict[str, set[str]] = field(default_factory=dict)
    degraded_securities: set[str] = field(default_factory=set)
