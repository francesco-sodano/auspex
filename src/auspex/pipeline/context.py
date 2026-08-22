"""Pipeline execution context — all injectable dependencies for one run.

Every dependency is optional except ``universe``/``config``/``as_of_date``.
A ``None`` dependency causes its step to be marked SKIPPED with a reason
rather than FAILED (arc42 §6.1: provider failures degrade coverage, they do
not abort the run).
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import date
from decimal import Decimal
from typing import ClassVar

from auspex.collectors.base import BlobSink, DocumentSink, FundamentalSink, FxSink, PriceSink, WatermarkStore
from auspex.config.loader import Universe
from auspex.extraction.channel_a import ChannelAExtractionSink
from auspex.extraction.channel_b import ChannelBDigestSink
from auspex.narrative.generator import NarrativeSink
from auspex.portfolio.port import PortfolioPort
from auspex.providers.base import FxProvider, NewsProvider, PriceProvider
from auspex.providers.edgar import EdgarClient
from auspex.providers.openai_provider import AzureOpenAIClient
from auspex.settings import Settings, get_settings

#: Only used by hand-built contexts; production resolves the real budgets from
#: configuration. Kept in step with ``Settings.pipeline_hard_timeout_minutes``
#: and ``config/policy.yaml``'s ``pipeline.hard_timeout_minutes``.
DEFAULT_HARD_TIMEOUT_MINUTES = 45


def _resolve_pipeline_minutes(
    *,
    settings_field: str,
    config_key: str,
    config: dict | None,
    settings: Settings | None,
) -> int:
    """Resolve one pipeline timing budget from configuration rather than a literal.

    Precedence, most specific first:

    1. the ``AUSPEX_``-prefixed environment variable for ``settings_field`` — an
       operator overriding a budget for a specific deployment must win over
       anything in the repo, otherwise the environment variable is decorative;
    2. ``config/policy.yaml``'s ``pipeline.<config_key>`` — the versioned value,
       fingerprinted into ``config_versions`` alongside the weights, so a run's
       deadline is reproducible from its config version;
    3. the ``Settings`` default.

    "Operator set the variable" is read from the settings model's populated
    field set rather than by comparing against the default, so deliberately
    setting the variable *to* the default value still takes precedence and does
    not silently hand control back to the config file.
    """

    settings = settings or get_settings()
    if settings_field in settings.model_fields_set:
        return int(getattr(settings, settings_field))

    configured = ((config or {}).get("policy") or {}).get("pipeline", {}).get(config_key)
    if configured is not None:
        try:
            return int(configured)
        except (TypeError, ValueError):
            pass
    return int(getattr(settings, settings_field))


def resolve_hard_timeout_minutes(config: dict | None = None, settings: Settings | None = None) -> int:
    """The whole-run budget in minutes (see :func:`_resolve_pipeline_minutes`)."""

    return _resolve_pipeline_minutes(
        settings_field="pipeline_hard_timeout_minutes",
        config_key="hard_timeout_minutes",
        config=config,
        settings=settings,
    )


def resolve_step_timeout_minutes(config: dict | None = None, settings: Settings | None = None) -> int:
    """The per-step ceiling in minutes (see :func:`_resolve_pipeline_minutes`).

    Deliberately a budget of its own rather than "whatever is left of the run":
    defaulting the ceiling to the whole-run deadline lets a single hung step
    burn the entire night before anything else is attempted, which is only
    marginally better than the container job's own replica timeout.
    """

    return _resolve_pipeline_minutes(
        settings_field="pipeline_step_timeout_minutes",
        config_key="step_timeout_minutes",
        config=config,
        settings=settings,
    )


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
    recommendation_disposition_repo: object | None = None
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
    #: Whole-run budget. Production and bootstrap factories resolve this from
    #: configuration via :func:`resolve_hard_timeout_minutes`; the literal
    #: default only applies to hand-built contexts (tests, ad-hoc scripts).
    hard_timeout_minutes: int = DEFAULT_HARD_TIMEOUT_MINUTES
    #: Ceiling on any *single* step. ``None`` means "whatever is left of the run
    #: budget", which is what keeps one hung step from running to the container
    #: job's own replica timeout instead of the configured pipeline deadline.
    step_timeout_minutes: int | None = None
    cash_chf: Decimal = field(default_factory=lambda: Decimal("4179"))

    # per-run scratch state, populated by earlier steps and consumed by later ones
    new_document_ids_by_security: dict[str, list[str]] = field(default_factory=dict)
    new_accessions_by_security: dict[str, set[str]] = field(default_factory=dict)
    #: Securities whose *scoring evidence* degraded this run — a price that
    #: could not be refreshed, or a Channel A extraction that raised. Channel A
    #: feeds the six legs, so its failure genuinely degrades a score.
    degraded_securities: set[str] = field(default_factory=set)
    #: Securities whose *explanation* evidence degraded this run (Channel B).
    #: Deliberately separate: Channel B produces narrative digests and plain
    #: summaries and feeds no leg, so a failure there must cost the user their
    #: explanation, never their score.
    explanation_degraded_securities: set[str] = field(default_factory=set)

    #: Scratch keys whose value is specific to one user. Everything else in
    #: ``__dict__`` is universe-wide research and is deliberately shared
    #: verbatim across the per-user fan-out (see :meth:`derive_for_user`).
    PER_USER_SCRATCH_KEYS: ClassVar[tuple[str, ...]] = (
        "_portfolio_projection",
        "_portfolio_snapshot",
        "_actions",
        "_eligible_but_no_cash_count",
        "_assertion_violations",
    )

    def step_budget_seconds(self, elapsed_seconds: float) -> float:
        """How long a single step may run, given how long the run already has.

        Bounded by both the per-step ceiling and whatever is left of the whole
        run budget. Evaluating the deadline only *between* steps left a hung
        step running until the container job's own replica timeout — an order of
        magnitude past the deadline the configuration promises — so the runner
        applies this to each step individually as well.
        """

        run_remaining = self.hard_timeout_minutes * 60 - elapsed_seconds
        ceiling = (self.step_timeout_minutes or self.hard_timeout_minutes) * 60
        return max(0.0, min(float(ceiling), run_remaining))

    def derive_for_user(
        self,
        user_id: str,
        *,
        portfolio_reader: PortfolioPort | None = None,
    ) -> PipelineContext:
        """A sibling context for ``user_id`` that reuses the shared research.

        The nightly run computes ingestion, extraction and scoring exactly
        once; only the portfolio projection, the policy cascade and the
        resulting recommendations are per-user. This returns a context that
        keeps every shared scratch value (score results, snapshots, packages,
        prices, FX) by reference — recomputing them per user would be both
        wasteful and, for LLM-backed steps, expensive — while dropping every
        value that belongs to a specific user so one user's portfolio can
        never leak into another's evaluation.

        ``portfolio_reader`` replaces the read-only ledger binding wholesale,
        so the returned context can only ever see ``user_id``'s events. When
        it is omitted the shared context's existing binding is kept rather
        than blanked: erasing it would silently degrade the projection to an
        empty book and emit trade suggestions against a phantom portfolio
        instead of failing loudly.
        """

        providers = replace(
            self.providers,
            portfolio_reader=(
                portfolio_reader if portfolio_reader is not None else self.providers.portfolio_reader
            ),
        )
        derived = PipelineContext(
            universe=self.universe,
            config=self.config,
            as_of_date=self.as_of_date,
            user_id=user_id,
            repos=self.repos,
            providers=providers,
            hard_timeout_minutes=self.hard_timeout_minutes,
            step_timeout_minutes=self.step_timeout_minutes,
            cash_chf=self.cash_chf,
            new_document_ids_by_security=self.new_document_ids_by_security,
            new_accessions_by_security=self.new_accessions_by_security,
            degraded_securities=self.degraded_securities,
            explanation_degraded_securities=self.explanation_degraded_securities,
        )
        declared = {f.name for f in fields(PipelineContext)}
        for key, value in self.__dict__.items():
            if key in declared or key in self.PER_USER_SCRATCH_KEYS:
                continue
            derived.__dict__[key] = value
        return derived
