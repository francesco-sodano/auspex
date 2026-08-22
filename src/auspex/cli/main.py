"""`auspex` console entrypoint (arc42 §6.1, §6.3, §7).

Subcommands (matching the IaC job container commands in
``infra/modules/containerapps.bicep``, e.g. ``python -m auspex nightly``):
- ``bootstrap``    — one-time cold start (arc42 §6.3)
- ``nightly``      — nightly 20-step pipeline for a given date (arc42 §6.1)
- ``performance``   — weekly self-measurement job (arc42 §5.8)
- ``serve``         — run the FastAPI app (arc42 §7 app-auspex-api)

Supports both the ``auspex`` console script and ``python -m auspex`` module
invocation (see ``src/auspex/__main__.py``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta

from auspex.persistence.repositories import CosmosRepository

logger = logging.getLogger("auspex.cli")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auspex", description="Auspex — personal AI financial research assistant")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # No CLI args: user_id is resolved via PortfolioAdapter.resolve_owner_user_sk()
    # at run time, never operator-supplied (see _bootstrap_command docstring).
    subparsers.add_parser("bootstrap", help="one-time cold start (arc42 §6.3)")
    recover_parser = subparsers.add_parser(
        "bootstrap-recover",
        help="resume only missing extraction, score dates, metrics, and validation",
    )
    recover_parser.add_argument(
        "--replay-all",
        action="store_true",
        help="recompute the full 18-month score window instead of only incomplete dates",
    )
    subparsers.add_parser(
        "bootstrap-audit",
        help="report historical score coverage without modifying data",
    )
    subparsers.add_parser(
        "seed-edgar-watermarks",
        help="advance missing filing/Form 4 watermarks after a historical bootstrap",
    )
    subparsers.add_parser(
        "migrate-multi-user",
        help="idempotently seed the configured legacy owner as the initial active administrator",
    )

    nightly_parser = subparsers.add_parser(
        "nightly", help="run the nightly 20-step pipeline (arc42 §6.1, job-auspex-pipeline)"
    )
    nightly_parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD, defaults to today (UTC)")

    perf_parser = subparsers.add_parser(
        "performance", help="run the weekly self-measurement job (arc42 §5.8, job-auspex-performance)"
    )
    perf_parser.add_argument("--date", type=str, default=None)

    shadow_parser = subparsers.add_parser(
        "shadow",
        help="run the pre-registered champion/challenger shadow study (arc42 §5.8, measurement only)",
    )
    shadow_parser.add_argument("--date", type=str, default=None)
    shadow_parser.add_argument(
        "--publish",
        action="store_true",
        help="write shadow_comparison metrics to the performance container (default: dry run)",
    )
    baseline_parser = subparsers.add_parser(
        "engine-baseline-export",
        help="export immutable score/performance baseline before replay",
    )
    baseline_parser.add_argument(
        "--label",
        required=True,
        help="safe baseline label, e.g. pre-convergence",
    )
    cleanup_parser = subparsers.add_parser(
        "derived-cleanup",
        help=(
            "clear rebuildable pre-production engine state while preserving "
            "raw evidence, users, user decisions, settings, conversations, "
            "performance attribution, and the portfolio ledger"
        ),
    )
    cleanup_parser.add_argument(
        "--apply",
        action="store_true",
        help="perform deletion; without this flag the command is read-only",
    )

    diagnose_parser = subparsers.add_parser(
        "market-data-diagnose",
        help="report market-data integrity findings, read-only (arc42 §5.3)",
    )
    diagnose_parser.add_argument(
        "--ticker",
        action="append",
        default=[],
        help="limit to this ticker; repeatable, defaults to the whole universe",
    )
    diagnose_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    repair_parser = subparsers.add_parser(
        "market-data-repair",
        help="idempotently repair adjusted series and quarantine bad bars (arc42 §5.3)",
    )
    repair_parser.add_argument(
        "--ticker",
        action="append",
        default=[],
        help="limit to this ticker; repeatable, defaults to the whole universe",
    )
    repair_parser.add_argument(
        "--dry-run", action="store_true", help="plan only; write no bars and no manifest revision"
    )
    repair_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    serve_parser = subparsers.add_parser("serve", help="run the FastAPI app (app-auspex-api)")
    serve_parser.add_argument("--host", type=str, default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8080)

    return parser


async def _migrate_multi_user_command() -> int:
    """Create the initial administrator before lifecycle enforcement goes live."""

    from auspex.api.deps import get_app_user_service
    from auspex.models.app_user import UserRole, UserStatus
    from auspex.persistence.cosmos_client import get_cosmos_context
    from auspex.settings import get_settings

    settings = get_settings()
    provider_user_id = (settings.owner_provider_user_id or "").strip()
    ledger_partition_key = (settings.owner_ledger_partition_key or "").strip()
    initial_admin_email = (settings.initial_admin_email or "").strip()
    if not provider_user_id:
        logger.error("migrate-multi-user: AUSPEX_OWNER_PROVIDER_USER_ID is required")
        return 1
    if not initial_admin_email:
        logger.error("migrate-multi-user: AUSPEX_INITIAL_ADMIN_EMAIL is required")
        return 1
    if not ledger_partition_key:
        logger.error(
            "migrate-multi-user: AUSPEX_OWNER_LEDGER_PARTITION_KEY is required "
            "to preserve the existing portfolio"
        )
        return 1

    cosmos = get_cosmos_context()
    service = get_app_user_service()
    try:
        user = await service.register(
            provider_user_id=provider_user_id,
            email=initial_admin_email,
            email_verified=False,
        )
        if user.ledger_partition_key != ledger_partition_key:
            logger.error(
                "migrate-multi-user: owner record uses ledger partition %s, "
                "expected the explicitly configured %s",
                user.ledger_partition_key,
                ledger_partition_key,
            )
            return 1
        if user.status is UserStatus.APPROVED_NEEDS_ONBOARDING:
            user = await service.complete_onboarding(user.user_id)
        if user.status is not UserStatus.ACTIVE:
            logger.error(
                "migrate-multi-user: configured owner resolved to unexpected status %s",
                user.status.value,
            )
            return 1
        if user.role is not UserRole.ADMIN:
            user = await service.set_role(
                user.user_id,
                UserRole.ADMIN,
                actor_user_id=user.user_id,
            )
        logger.info(
            "migrate-multi-user: owner is ACTIVE ADMIN with preserved ledger partition %s",
            user.ledger_partition_key,
        )
        return 0
    finally:
        await cosmos.aclose()


def _parse_date(value: str | None) -> date:
    if value is None:
        return datetime.now().date()
    return date.fromisoformat(value)


async def _aclose_unique(*resources) -> None:
    closed: set[int] = set()
    for resource in resources:
        if resource is None or id(resource) in closed:
            continue
        closed.add(id(resource))
        close = getattr(resource, "aclose", None)
        if close is not None:
            await close()


async def _run_pipeline_command(as_of_date: date) -> int:
    from auspex.config import (
        build_config_version,
        load_cohorts,
        load_fees,
        load_label_mappings,
        load_policy,
        load_taxonomy,
        load_universe,
        load_weights,
        load_xbrl_concepts,
    )
    from auspex.models.common import utc_now
    from auspex.models.config_version import ConfigVersion
    from auspex.models.policy import Recommendation, RecommendationDisposition
    from auspex.models.portfolio import PortfolioProjection
    from auspex.models.run import RunManifest
    from auspex.models.scoring import LegChange, ScoreSnapshot
    from auspex.models.user_settings import UserSettings
    from auspex.persistence.blob_client import get_blob_context
    from auspex.persistence.cosmos_client import get_cosmos_context, get_source_ledger_context
    from auspex.persistence.repositories import (
        CosmosChannelAExtractionSink,
        CosmosChannelBDigestSink,
        CosmosDocumentSink,
        CosmosFundamentalSink,
        CosmosFxSink,
        CosmosNarrativeSink,
        CosmosPriceSink,
        CosmosWatermarkStore,
    )
    from auspex.pipeline.context import (
        PipelineContext,
        PipelineProviders,
        PipelineRepos,
        resolve_hard_timeout_minutes,
        resolve_step_timeout_minutes,
    )
    from auspex.portfolio.adapter import PortfolioAdapter
    from auspex.portfolio.mapping import load_portfolio_mapping
    from auspex.providers.factory import build_default_providers
    from auspex.providers.openai_provider import AzureOpenAIClient
    from auspex.providers.secrets import get_secret_resolver
    from auspex.settings import get_settings

    universe = load_universe()
    config = {
        "weights": load_weights(),
        "policy": load_policy(),
        "xbrl_concepts": load_xbrl_concepts(),
        "label_mappings": load_label_mappings(),
        "cohorts": load_cohorts(),
        "taxonomy": load_taxonomy(),
        "fees": load_fees(),
    }
    config_version = build_config_version(f"{as_of_date.isoformat()}-a", utc_now())

    cosmos = get_cosmos_context()
    blob = get_blob_context()
    settings = get_settings()

    # Bind to the source portfolio ledger account (arc42 §5.7 — a separate Cosmos
    # account from Auspex's own) and resolve who this run is for.
    #
    # Multi-user: the nightly run no longer belongs to a single owner. Shared
    # research (ingestion, extraction, scoring, narratives) runs once; the
    # per-user stage then runs for every ACTIVE application user, each against a
    # ledger binding that can only see their own partition. If the roster cannot
    # be read at all, the run falls back to the legacy single-owner binding so a
    # deployment that has not yet registered anybody still produces its owner's
    # recommendations exactly as before.
    source_ledger = None
    try:
        mapping = load_portfolio_mapping()
        source_ledger = get_source_ledger_context()
    except Exception as exc:  # noqa: BLE001 - fatal: no ledger binding at all
        logger.error(
            "nightly: could not bind to the source portfolio ledger: %s", exc, exc_info=True
        )
        await _aclose_unique(source_ledger, blob, cosmos)
        return 1

    active_users = await _resolve_active_users(cosmos)
    uses_multi_user_roster = active_users is not None
    fallback_reader = None
    if active_users is None:
        # No roster yet (fresh deployment, or a pre-multi-user database): keep
        # the historical behaviour of resolving the one configured owner rather
        # than silently producing nothing.
        try:
            fallback_reader = PortfolioAdapter(source_ledger, mapping)
            legacy_user_id = await fallback_reader.resolve_owner_user_sk()
            active_users = [(legacy_user_id, legacy_user_id)]
        except Exception as exc:  # noqa: BLE001 - fatal: nobody to write for
            logger.error(
                "nightly: no ACTIVE application users and no resolvable legacy owner — "
                "cannot proceed without an unambiguous user_sk for user-scoped writes: %s",
                exc,
                exc_info=True,
            )
            await _aclose_unique(source_ledger, blob, cosmos)
            return 1
    elif not active_users:
        logger.error(
            "nightly: the application-user roster exists but contains no ACTIVE users; "
            "refusing to fall back to a legacy partition"
        )
        await _aclose_unique(source_ledger, blob, cosmos)
        return 1

    user_operation_factory = None
    if uses_multi_user_roster:
        from auspex.models.app_user import AppUser, AppUserSummary
        from auspex.users.service import AppUserService

        user_service = AppUserService(
            user_repo=CosmosRepository(cosmos, "app_users", AppUser),
            index_repo=CosmosRepository(
                cosmos,
                "app_user_index",
                AppUserSummary,
            ),
        )
        def user_operation_factory(user_id):
            return user_service.user_operation(
                user_id,
                require_active=True,
            )

    primary_user_id = active_users[0][0]
    portfolio_reader = fallback_reader or PortfolioAdapter(
        source_ledger, mapping, owner_user_sk=active_users[0][1]
    )

    # Persist the config version bundle actually used by this run (arc42
    # §5.11) — every `scores` row cites `config_version_id`, so without this
    # write no historical score could be reproduced under its original
    # weights/policy/taxonomy. No pipeline step performs this write; it must
    # happen here, once, before the run starts.
    config_version_repo = CosmosRepository(cosmos, "config_versions", ConfigVersion)
    await config_version_repo.upsert(config_version)

    # Default provider set (arc42 §3.1): Alpha Vantage serves both price and FX
    # from the one `AUSPEX_PRICE_API_KEY_SECRET`-named Key Vault secret; Finnhub
    # serves news from `AUSPEX_NEWS_API_KEY_SECRET`. A provider whose secret
    # cannot be resolved comes back None and its collector step is skipped
    # rather than aborting the run (arc42 §6.1).
    secret_resolver = get_secret_resolver(settings.key_vault_url)
    default_providers = await build_default_providers(settings, secret_resolver)

    # Channel A/B extraction + narrative generation (arc42 §5.4, §6.3 "Runtime
    # budget"), paced against the confirmed `gpt-4.1-mini` deployment quota
    # (450,000 TPM in Sweden Central) via a token-bucket in AzureOpenAIClient
    # itself. Absent if the endpoint can't be reached, in which case Channel
    # A/B/narrative steps are skipped rather than aborting the run.
    openai_client = None
    try:
        openai_client = AzureOpenAIClient(
            endpoint=settings.aoai_endpoint,
            api_version=settings.aoai_api_version,
            tokens_per_minute=settings.aoai_tokens_per_minute,
            tokens_per_minute_by_deployment={
                settings.aoai_deployment_narrative: settings.aoai_narrative_tokens_per_minute,
                settings.aoai_deployment_answer: settings.aoai_narrative_tokens_per_minute,
            },
        )
    except Exception:  # noqa: BLE001 - degrade to no LLM extraction, do not abort the run
        logger.warning(
            "could not construct Azure OpenAI client; extraction/narrative steps will be skipped", exc_info=True
        )

    providers = PipelineProviders(
        price_provider=default_providers.price_and_fx,
        fx_provider=default_providers.price_and_fx,
        news_provider=default_providers.news,
        edgar_client=default_providers.edgar,
        openai_client=openai_client,
        portfolio_reader=portfolio_reader,
    )

    # Reuse the ready-made Cosmos sink adapters from auspex.persistence.repositories
    # (arc42 §5.3, §5.4, §5.11) instead of re-declaring local duplicates — this
    # also wires channel_a_sink/channel_b_sink/narrative_sink so Channel A/B
    # extraction and narrative generation actually execute (rather than
    # skipping as success-shaped no-ops) whenever `openai_client` above is
    # available, matching `_bootstrap_command`'s wiring.
    repos = PipelineRepos(
        document_sink=CosmosDocumentSink(cosmos),
        price_sink=CosmosPriceSink(cosmos),
        fx_sink=CosmosFxSink(cosmos),
        fundamental_sink=CosmosFundamentalSink(cosmos),
        blob_sink=blob,
        watermarks=CosmosWatermarkStore(cosmos),
        channel_a_sink=CosmosChannelAExtractionSink(cosmos),
        channel_b_sink=CosmosChannelBDigestSink(cosmos),
        narrative_sink=CosmosNarrativeSink(cosmos),
        score_repo=CosmosRepository(cosmos, "scores", ScoreSnapshot),
        leg_change_repo=CosmosRepository(cosmos, "leg_changes", LegChange),
        recommendation_repo=CosmosRepository(cosmos, "recommendations", Recommendation),
        recommendation_disposition_repo=CosmosRepository(
            cosmos, "recommendation_dispositions", RecommendationDisposition
        ),
        run_repo=CosmosRepository(cosmos, "runs", RunManifest),
        portfolio_projection_repo=CosmosRepository(cosmos, "portfolio_projection", PortfolioProjection),
        user_settings_repo=CosmosRepository(
            cosmos,
            "user_settings",
            UserSettings,
        ),
        config_version_repo=config_version_repo,
    )

    ctx = PipelineContext(
        universe=universe,
        config=config,
        as_of_date=as_of_date,
        user_id=primary_user_id,
        repos=repos,
        providers=providers,
        # The run budget is configuration, not a literal: without this the
        # deadline was a hard-coded 45 minutes and both
        # AUSPEX_PIPELINE_HARD_TIMEOUT_MINUTES and policy.yaml's
        # pipeline.hard_timeout_minutes were read by nothing. The per-step
        # ceiling is resolved the same way so one hung provider call cannot
        # consume the entire night on its own.
        hard_timeout_minutes=resolve_hard_timeout_minutes(config, settings),
        step_timeout_minutes=resolve_step_timeout_minutes(config, settings),
    )
    ctx.__dict__["_config_version_id"] = config_version.id

    def reader_for(user_id: str):
        partition = next(
            (partition for candidate, partition in active_users if candidate == user_id), user_id
        )
        if fallback_reader is not None and user_id == primary_user_id:
            return fallback_reader
        return PortfolioAdapter(source_ledger, mapping, owner_user_sk=partition)

    try:
        result = await run_pipeline_wrapper(
            ctx,
            user_ids=[user_id for user_id, _ in active_users],
            portfolio_reader_factory=reader_for,
            user_operation_factory=user_operation_factory,
            concurrency=settings.nightly_user_concurrency,
        )
        manifest = result.manifest
        if result.failed_user_ids:
            logger.warning(
                "nightly: per-user stage failed for %d of %d users: %s",
                len(result.failed_user_ids),
                len(active_users),
                ", ".join(result.failed_user_ids),
            )
    finally:
        # Release all per-run SDK clients and credentials.
        await _aclose_unique(
            default_providers.price_and_fx,
            default_providers.news,
            default_providers.edgar,
            openai_client,
            secret_resolver,
            blob,
            source_ledger,
            cosmos,
        )

    logger.info(
        "pipeline run finished: status=%s users=%d",
        manifest.status.value,
        len(active_users),
    )
    return 0 if manifest.status.value in ("SUCCESS", "DEGRADED") else 1


async def _resolve_active_users(cosmos) -> list[tuple[str, str]] | None:
    """``(user_id, ledger_partition_key)`` for every ACTIVE application user.

    Read through the roster projection, which is a single-partition query.
    A missing container (a database provisioned before multi-user) is not an
    error: it simply means "no roster", and the caller falls back to the
    legacy single-owner binding.
    """

    from azure.cosmos.exceptions import CosmosResourceNotFoundError

    from auspex.models.app_user import AppUser, AppUserSummary, UserStatus
    from auspex.persistence.repositories import CosmosRepository as _Repo
    from auspex.users.service import AppUserService

    try:
        service = AppUserService(
            user_repo=_Repo(cosmos, "app_users", AppUser),
            index_repo=_Repo(cosmos, "app_user_index", AppUserSummary),
        )
        summaries = await service.list_users(status=UserStatus.ACTIVE)
    except CosmosResourceNotFoundError:
        logger.info("nightly: no application-user roster available; using legacy owner binding")
        return None

    resolved: list[tuple[str, str]] = []
    for summary in summaries:
        user = await service.get_user(summary.user_id)
        if user is None or user.status is not UserStatus.ACTIVE:
            logger.warning(
                "nightly: ignoring stale ACTIVE roster entry for user %s",
                summary.user_id,
            )
            continue
        resolved.append((user.user_id, user.ledger_partition_key))
    return resolved


async def run_pipeline_wrapper(
    ctx,
    *,
    user_ids: list[str] | None = None,
    portfolio_reader_factory=None,
    user_operation_factory=None,
    concurrency: int = 4,
):
    """Run the nightly pipeline for one or many users.

    Kept as a thin seam so tests can substitute the whole run. With a single
    user and no reader factory this is behaviourally identical to the
    original single-owner runner.
    """

    from auspex.pipeline.fanout import run_multi_user_pipeline

    return await run_multi_user_pipeline(
        ctx,
        user_ids if user_ids is not None else [ctx.user_id],
        portfolio_reader_factory=portfolio_reader_factory,
        user_operation_factory=user_operation_factory,
        concurrency=concurrency,
    )


async def _performance_command(as_of_date: date) -> int:
    """arc42 §5.8 weekly self-measurement job (``job-auspex-performance``,
    Sunday 03:00 UTC per ``infra/modules/containerapps.bicep``) — recomputes
    composite/leg IC performance metrics over every date already present in
    the ``scores`` container, reusing
    :meth:`auspex.cli.bootstrap.BootstrapRunner.compute_performance_metrics`
    (arc42 §6.3 step 10) rather than duplicating the cross-sectional IC
    computation here.

    Unlike ``_run_pipeline_command``/``_bootstrap_command``, this job publishes
    into the shared ``performance`` container (arc42 §5.8), which is
    partitioned by ``/metric_type`` rather than ``user_id``: composite/leg IC,
    leg correlation and cohort quality measure the *research*, and are the
    same population-level facts for everybody.

    Attribution is different. Suggestion hit rate and disposition outcome
    describe what one person did with their suggestions, so they are scoped to
    the single ledger owner whose transactions supply
    ``accepted_recommendation_ids``; blending several users' recommendations
    into a shared metric would both double-count the same decision and leak
    one user's behaviour to another. The ``user_id`` on the throwaway
    :class:`~auspex.pipeline.context.PipelineContext` built below remains a
    fixed service literal — never an operator argument.
    """

    from auspex.cli.bootstrap import BootstrapRunner
    from auspex.config import load_fees, load_policy, load_universe
    from auspex.models.performance import PerformanceMetric
    from auspex.models.policy import Recommendation
    from auspex.models.scoring import ScoreSnapshot
    from auspex.persistence.blob_client import get_blob_context
    from auspex.persistence.cosmos_client import (
        get_cosmos_context,
        get_source_ledger_context,
    )
    from auspex.persistence.repositories import (
        CosmosDocumentSink,
        CosmosFundamentalSink,
        CosmosFxSink,
        CosmosPriceSink,
        CosmosWatermarkStore,
    )
    from auspex.pipeline.context import (
        PipelineContext,
        PipelineRepos,
        resolve_hard_timeout_minutes,
        resolve_step_timeout_minutes,
    )
    from auspex.portfolio.adapter import PortfolioAdapter
    from auspex.portfolio.event_ledger import effective_transactions
    from auspex.portfolio.mapping import load_portfolio_mapping

    logger.info("performance: weekly self-measurement job invoked for %s (arc42 §5.8)", as_of_date)

    universe = load_universe()
    cosmos = get_cosmos_context()
    blob = get_blob_context()

    # Reuse the same ready-made Cosmos sink adapters bootstrap wires (arc42
    # §5.3, §5.4, §5.11) so `fetch_all` inside `compute_performance_metrics`
    # actually sees data — the nightly pipeline's local `_CosmosPriceSink`
    # (above) only exposes `upsert_price_bar`, not the `.all()`/`.query()`
    # read protocol `fetch_all` depends on. `document_sink`/`fx_sink`/
    # `fundamental_sink`/`blob_sink`/`watermarks` are unused by this job but
    # required (no default) by `PipelineRepos`.
    repos = PipelineRepos(
        document_sink=CosmosDocumentSink(cosmos),
        price_sink=CosmosPriceSink(cosmos),
        fx_sink=CosmosFxSink(cosmos),
        fundamental_sink=CosmosFundamentalSink(cosmos),
        blob_sink=blob,
        watermarks=CosmosWatermarkStore(cosmos, container_name="config_versions"),
        score_repo=CosmosRepository(cosmos, "scores", ScoreSnapshot),
        recommendation_repo=CosmosRepository(
            cosmos,
            "recommendations",
            Recommendation,
        ),
    )
    performance_repo = CosmosRepository(cosmos, "performance", PerformanceMetric)

    policy_config = load_policy()
    ctx = PipelineContext(
        universe=universe,
        config={"fees": load_fees(), "policy": policy_config},
        as_of_date=as_of_date,
        user_id="system",
        repos=repos,
        hard_timeout_minutes=resolve_hard_timeout_minutes({"policy": policy_config}),
        step_timeout_minutes=resolve_step_timeout_minutes({"policy": policy_config}),
    )

    runner = BootstrapRunner(universe=universe, context_factory=lambda _as_of: ctx)
    metrics = await runner.compute_performance_metrics(
        ctx,
        performance_repo=performance_repo,
        include_recommendation_metrics=False,
    )

    source_ledger = get_source_ledger_context()
    mapping = load_portfolio_mapping()
    active_users = await _resolve_active_users(cosmos)
    uses_multi_user_roster = active_users is not None
    if active_users is None:
        try:
            legacy = PortfolioAdapter(source_ledger, mapping)
            legacy_user_id = await legacy.resolve_owner_user_sk()
            active_users = [(legacy_user_id, legacy_user_id)]
        except Exception:  # noqa: BLE001 - no owner means shared metrics only
            active_users = []
            logger.info(
                "performance: no resolvable ledger owner; publishing shared score metrics only"
            )

    user_performance_repo = CosmosRepository(
        cosmos, "user_performance", PerformanceMetric
    )

    performance_user_service = None
    if uses_multi_user_roster:
        from auspex.models.app_user import AppUser, AppUserSummary
        from auspex.users.service import AppUserService

        performance_user_service = AppUserService(
            user_repo=CosmosRepository(cosmos, "app_users", AppUser),
            index_repo=CosmosRepository(
                cosmos,
                "app_user_index",
                AppUserSummary,
            ),
        )

    async def compute_private_metrics(
        user_id: str,
        ledger_partition: str,
    ) -> None:
        adapter = PortfolioAdapter(
            source_ledger,
            mapping,
            owner_user_sk=ledger_partition,
        )
        accepted_recommendation_ids = {
            transaction.recommendation_id
            for transaction in effective_transactions(
                await adapter.read_transactions()
            )
            if transaction.followed_auspex and transaction.recommendation_id
        }
        user_metrics = await runner.compute_performance_metrics(
            ctx,
            performance_repo=None,
            accepted_recommendation_ids=accepted_recommendation_ids,
            attribution_user_id=user_id,
        )
        for metric in user_metrics:
            if metric.metric_type not in {
                "suggestion_hit_rate",
                "disposition_outcome",
            }:
                continue
            await user_performance_repo.upsert(
                metric.model_copy(
                    update={
                        "id": f"{user_id}:{metric.id}",
                        "user_id": user_id,
                    }
                )
            )

    for user_id, ledger_partition in active_users:
        try:
            if performance_user_service is None:
                await compute_private_metrics(user_id, ledger_partition)
            else:
                async with performance_user_service.user_operation(
                    user_id,
                    require_active=True,
                ):
                    await compute_private_metrics(
                        user_id,
                        ledger_partition,
                    )
        except Exception:  # noqa: BLE001 - isolate one user's private attribution
            logger.error(
                "performance: private attribution failed for user %s",
                user_id,
                exc_info=True,
            )

    logger.info(
        "performance: complete — metrics_computed=%d (arc42 §5.8, job-auspex-performance)",
        len(metrics),
    )
    await _aclose_unique(source_ledger, blob, cosmos)
    return 0


async def _shadow_command(as_of_date: date, *, publish: bool = False) -> int:
    """Run the pre-registered champion/challenger shadow study (arc42 §5.8).

    This is measurement, not production scoring: no weight, formula or portfolio
    policy is touched, and nothing is written unless ``--publish`` is given. The
    study exists to answer whether a named challenger — notably the
    ``corrected_fixed`` denominator variant — would have out-predicted the
    champion on the history we already have, before anybody argues about
    promoting it.
    """

    from decimal import Decimal

    from auspex.cli.bootstrap import _forward_return_usd
    from auspex.cli.shadow_cli import run_shadow_study
    from auspex.config import load_weights
    from auspex.models.enums import LegName
    from auspex.models.performance import PerformanceMetric
    from auspex.models.scoring import ScoreSnapshot
    from auspex.performance.shadow import assert_matches_production_weights
    from auspex.persistence.cosmos_client import get_cosmos_context
    from auspex.persistence.repositories import CosmosPriceSink
    from auspex.pipeline.repo_access import fetch_all

    logger.info("shadow: pre-registered comparison invoked for %s (publish=%s)", as_of_date, publish)

    domestic = load_weights().get("domestic", {})
    try:
        assert_matches_production_weights(
            {leg: Decimal(str(domestic[leg.value])) for leg in LegName if leg.value in domestic}
        )
    except ValueError:
        logger.error(
            "shadow: aborting — the champion weight snapshot no longer matches config/weights.yaml, "
            "so a comparison against it would be measuring the wrong champion",
            exc_info=True,
        )
        return 1

    cosmos = get_cosmos_context()
    score_repo = CosmosRepository(cosmos, "scores", ScoreSnapshot)

    snapshots = await fetch_all(score_repo)
    all_bars = await fetch_all(CosmosPriceSink(cosmos))

    bars_by_security: dict[str, list] = {}
    for bar in all_bars:
        bars_by_security.setdefault(bar.security_id, []).append(bar)
    for bars in bars_by_security.values():
        bars.sort(key=lambda item: item.session_date)
    dates_by_security = {
        security_id: [bar.session_date for bar in bars] for security_id, bars in bars_by_security.items()
    }

    def forward_return(security_id: str, as_of: date, horizon_days: int):
        return _forward_return_usd(bars_by_security, security_id, as_of, horizon_days, dates_by_security)

    performance_repo = CosmosRepository(cosmos, "performance", PerformanceMetric) if publish else None
    report, metrics = await run_shadow_study(
        snapshots,
        forward_return,
        as_of_date=as_of_date,
        publish=publish,
        performance_repo=performance_repo,
    )

    logger.info(
        "shadow: complete — dates_evaluated=%d metrics=%d published=%s (arc42 §5.8)",
        report.dates_evaluated,
        len(metrics),
        publish,
    )
    await _aclose_unique(cosmos)
    return 0


async def _bootstrap_audit_command() -> int:
    from auspex.cli.bootstrap import (
        MIN_SCORED_SECURITIES,
        TOTAL_RECENT_SESSIONS,
        extraction_backfill_start,
    )
    from auspex.config import load_universe
    from auspex.models.market import PriceBar
    from auspex.models.scoring import ScoreSnapshot
    from auspex.persistence.cosmos_client import get_cosmos_context

    today = datetime.now().date()
    repository = CosmosRepository(
        get_cosmos_context(), "scores", ScoreSnapshot
    )
    counts = await repository.valid_score_counts_by_date(
        extraction_backfill_start(today), today
    )
    expected_dates = [
        item
        for item in (
            extraction_backfill_start(today) + timedelta(days=offset)
            for offset in range(
                (today - extraction_backfill_start(today)).days + 1
            )
        )
        if item.weekday() < 5
    ][-TOTAL_RECENT_SESSIONS:]
    incomplete = [
        (item, counts.get(item, 0))
        for item in expected_dates
        if counts.get(item, 0) < MIN_SCORED_SECURITIES
    ]
    incomplete_dates = {item for item, _ in incomplete}
    snapshots = await repository.for_dates(incomplete_dates)
    valid_by_date: dict[date, set[str]] = {}
    snapshot_by_date_security = {}
    for snapshot in snapshots:
        snapshot_by_date_security[(snapshot.as_of_date, snapshot.security_id)] = snapshot
        if snapshot.percentile is not None:
            valid_by_date.setdefault(snapshot.as_of_date, set()).add(
                snapshot.security_id
            )
    universe = load_universe()
    ticker_by_id = {security.id: security.ticker for security in universe.securities}
    missing_by_date = {
        item.isoformat(): sorted(
            ticker_by_id[security.id]
            for security in universe.securities
            if security.id not in valid_by_date.get(item, set())
        )
        for item in sorted(incomplete_dates)
    }
    market_repository = CosmosRepository(
        get_cosmos_context(), "market_daily", PriceBar
    )

    async def earliest_price(security_id: str) -> str | None:
        rows = await market_repository.query(
            (
                "SELECT TOP 1 * FROM c WHERE c.security_id=@security_id "
                "ORDER BY c.session_date ASC"
            ),
            [{"name": "@security_id", "value": security_id}],
            partition_key=security_id,
        )
        return rows[0].session_date.isoformat() if rows else None

    missing_security_ids = {
        security.id
        for item in incomplete_dates
        for security in universe.securities
        if security.id not in valid_by_date.get(item, set())
    }
    earliest_prices = {
        ticker_by_id[security_id]: earliest
        for security_id, earliest in zip(
            sorted(missing_security_ids),
            await asyncio.gather(
                *(earliest_price(security_id) for security_id in sorted(missing_security_ids))
            ),
            strict=True,
        )
    }
    qualifying = len(expected_dates) - len(incomplete)
    logger.info(
        "bootstrap audit — expected_sessions=%d, qualifying_sessions=%d, "
        "incomplete_sessions=%d, incomplete=%s",
        len(expected_dates),
        qualifying,
        len(incomplete),
        [(item.isoformat(), count) for item, count in incomplete],
    )
    logger.info("bootstrap audit missing securities — %s", missing_by_date)
    logger.info("bootstrap audit earliest prices — %s", earliest_prices)
    print(
        "AUSPEX_BOOTSTRAP_AUDIT_SUMMARY "
        f"expected_sessions={len(expected_dates)} "
        f"qualifying_sessions={qualifying} "
        f"incomplete_sessions={len(incomplete)} "
        f"incomplete={[(item.isoformat(), count) for item, count in incomplete]}",
        flush=True,
    )
    print(
        f"AUSPEX_BOOTSTRAP_AUDIT_MISSING_SECURITIES {missing_by_date}",
        flush=True,
    )
    print(
        f"AUSPEX_BOOTSTRAP_AUDIT_EARLIEST_PRICES {earliest_prices}",
        flush=True,
    )
    diagnostic_dates = {
        item
        for item in (
            min(incomplete_dates) if incomplete_dates else None,
            max(incomplete_dates) if incomplete_dates else None,
        )
        if item is not None
    }
    leg_diagnostics = {}
    for as_of_date in sorted(diagnostic_dates):
        rows = {}
        for security in universe.securities:
            if security.id in valid_by_date.get(as_of_date, set()):
                continue
            snapshot = snapshot_by_date_security.get((as_of_date, security.id))
            rows[security.ticker] = (
                {
                    "coverage": snapshot.coverage,
                    "excluded_stale": snapshot.excluded_stale,
                    "computable_legs": sorted(
                        leg.value
                        for leg, result in snapshot.legs.items()
                        if result.computable
                    ),
                    "non_computable_legs": {
                        leg.value: {"raw": result.raw, "z": result.z}
                        for leg, result in snapshot.legs.items()
                        if not result.computable
                    },
                }
                if snapshot is not None
                else {"missing_snapshot": True}
            )
        leg_diagnostics[as_of_date.isoformat()] = rows
    logger.info("bootstrap audit leg diagnostics — %s", leg_diagnostics)
    for as_of_date, rows in leg_diagnostics.items():
        print(
            f"AUSPEX_BOOTSTRAP_AUDIT_LEGS date={as_of_date} rows={rows}",
            flush=True,
        )
    return 0


async def _seed_edgar_watermarks_command() -> int:
    from auspex.cli.bootstrap import extraction_backfill_start, raw_backfill_start
    from auspex.collectors.base import watermark_key
    from auspex.collectors.filing_collector import INTERESTING_FORMS
    from auspex.config import load_universe
    from auspex.models.document import Document
    from auspex.persistence.cosmos_client import get_cosmos_context
    from auspex.persistence.repositories import CosmosRepository, CosmosWatermarkStore
    from auspex.providers.edgar import (
        EdgarClient,
        latest_accession_for_forms,
        latest_filing_date_for_forms,
    )
    from auspex.settings import get_settings

    today = datetime.now().date()
    universe = load_universe()
    cosmos = get_cosmos_context()
    watermarks = CosmosWatermarkStore(cosmos)
    documents = await CosmosRepository(cosmos, "documents", Document).all()

    persisted: dict[tuple[str, str], str] = {}
    for document in documents:
        if document.accession_number is None:
            continue
        collector = "filing" if document.form_type in INTERESTING_FORMS else None
        if collector is None:
            continue
        key = (collector, document.security_id)
        persisted[key] = max(
            persisted.get(key, document.accession_number),
            document.accession_number,
        )

    settings = get_settings()
    edgar = EdgarClient(
        base_url=settings.edgar_base_url,
        www_base_url=settings.edgar_www_base_url,
        user_agent=settings.edgar_user_agent,
        rate_limit_per_second=settings.edgar_rate_limit_per_second,
    )
    updated = 0
    try:
        for security in universe.securities:
            submissions = await edgar.get_submissions(security.cik)
            for collector, forms, cutoff in (
                ("filing", INTERESTING_FORMS, extraction_backfill_start(today)),
                ("insider", frozenset({"4"}), raw_backfill_start(today)),
            ):
                if collector == "insider":
                    target_date = latest_filing_date_for_forms(
                        submissions, forms, filed_before=None
                    )
                    target = (
                        target_date.isoformat() if target_date is not None else None
                    )
                else:
                    target = persisted.get((collector, security.id))
                    if target is None:
                        target = latest_accession_for_forms(
                            submissions, forms, filed_before=cutoff
                        )
                if target is None:
                    continue
                key = watermark_key(collector, security.id)
                current = await watermarks.get_watermark(key)
                current_is_date = (
                    current is not None
                    and len(current) == 10
                    and current[4] == "-"
                    and current[7] == "-"
                )
                if (
                    current is None
                    or (collector == "insider" and not current_is_date)
                    or target > current
                ):
                    await watermarks.set_watermark(key, target)
                    updated += 1
    finally:
        await _aclose_unique(edgar, cosmos)

    print(
        f"AUSPEX_EDGAR_WATERMARKS seeded={updated} securities={len(universe.securities)}",
        flush=True,
    )
    return 0


def _serve_command(host: str, port: int) -> int:
    import uvicorn

    uvicorn.run("auspex.api.app:app", host=host, port=port)
    return 0


async def _bootstrap_command(
    *, recovery_only: bool = False, replay_all: bool = False
) -> int:
    """arc42 §6.3 cold-start bootstrap — orchestrates
    :class:`auspex.cli.bootstrap.BootstrapRunner` end to end (steps 1-12):
    CIK/filer-profile verification, bulk ``submissions.zip``/``companyfacts.zip``
    streaming, the 36-month raw and 18-month extraction/scoring backfill
    windows (prices/FX, filings, Form 4, Channel A/B extraction,
    fundamentals, news), day-by-day score replay, performance metrics, and
    the confirmation-gated, read-only portfolio binding validation.

    Repository/provider wiring mirrors :func:`_run_pipeline_command` (same
    :mod:`auspex.persistence.repositories` Cosmos sinks, same
    :func:`auspex.providers.factory.build_default_providers` provider set) —
    ``_run_pipeline_command`` itself is left untouched. The exit code
    reflects ``BootstrapReport.validation_passed`` (step 12: >=85 securities
    scored on >=370 of the last 378 sessions).

    ``user_id`` is not an operator-supplied argument: it is resolved from the
    real source-ledger owner via ``PortfolioAdapter.resolve_owner_user_sk()``
    (arc42 §5.7) immediately after the adapter is constructed, and is
    used for every user_id-partitioned write this run makes. Binding absence
    or ambiguity (no ``config/portfolio_mapping.yaml``, no resolvable/unique
    ``app_users`` document, etc) is a hard failure — there is no well-defined
    identity to bind writes to, so bootstrapping under a placeholder like the
    literal ``"owner"`` would silently populate data nobody could read back.
    """

    from auspex.cli.bootstrap import (
        MIN_SESSIONS_SCORED,
        BootstrapRunner,
        PortfolioBindingNotConfirmedError,
        extraction_backfill_start,
    )
    from auspex.config import (
        build_config_version,
        load_cohorts,
        load_fees,
        load_label_mappings,
        load_policy,
        load_taxonomy,
        load_universe,
        load_weights,
        load_xbrl_concepts,
    )
    from auspex.models.common import utc_now
    from auspex.models.config_version import ConfigVersion
    from auspex.models.performance import PerformanceMetric
    from auspex.models.policy import Recommendation
    from auspex.models.portfolio import PortfolioProjection
    from auspex.models.run import RunManifest
    from auspex.models.scoring import LegChange, ScoreSnapshot
    from auspex.models.user_settings import UserSettings
    from auspex.persistence.blob_client import get_blob_context
    from auspex.persistence.cosmos_client import get_cosmos_context, get_source_ledger_context
    from auspex.persistence.repositories import (
        CosmosChannelAExtractionSink,
        CosmosChannelBDigestSink,
        CosmosDocumentSink,
        CosmosFundamentalSink,
        CosmosFxSink,
        CosmosNarrativeSink,
        CosmosPriceSink,
        CosmosWatermarkStore,
    )
    from auspex.pipeline.context import (
        PipelineContext,
        PipelineProviders,
        PipelineRepos,
        resolve_hard_timeout_minutes,
        resolve_step_timeout_minutes,
    )
    from auspex.portfolio.adapter import PortfolioAdapter
    from auspex.portfolio.mapping import load_portfolio_mapping
    from auspex.providers.factory import build_default_providers
    from auspex.providers.openai_provider import AzureOpenAIClient
    from auspex.providers.secrets import get_secret_resolver
    from auspex.settings import get_settings

    logger.info("bootstrap invoked (arc42 §6.3)")

    today = datetime.now().date()
    settings = get_settings()
    universe = load_universe()
    config = {
        "weights": load_weights(),
        "policy": load_policy(),
        "xbrl_concepts": load_xbrl_concepts(),
        "label_mappings": load_label_mappings(),
        "cohorts": load_cohorts(),
        "taxonomy": load_taxonomy(),
        "fees": load_fees(),
    }
    config_version = build_config_version(f"{today.isoformat()}-bootstrap", utc_now())

    # Read-only binding to the owner-owned portfolio ledger (arc42 §5.7,
    # resolved before any provider/repo wiring so a bad binding
    # fails fast. `resolve_owner_user_sk` is also the single source of truth
    # for the `user_id` this run writes under — see docstring above.
    source_ledger = None
    try:
        mapping = load_portfolio_mapping()
        source_ledger = get_source_ledger_context()
        adapter = PortfolioAdapter(source_ledger, mapping)
        user_id = await adapter.resolve_owner_user_sk()
    except Exception as exc:  # noqa: BLE001 - fatal: no unambiguous owner to bind writes to
        logger.error(
            "bootstrap: could not resolve the portfolio owner via PortfolioAdapter — cannot proceed "
            "without an unambiguous user_sk for user-scoped writes: %s",
            exc,
            exc_info=True,
        )
        await _aclose_unique(source_ledger)
        return 1

    logger.info("bootstrap: resolved portfolio owner user_sk=%s", user_id)

    secret_resolver = get_secret_resolver(settings.key_vault_url)
    default_providers = await build_default_providers(settings, secret_resolver)

    cosmos = get_cosmos_context()
    blob = get_blob_context()

    # Persist the config version bundle actually used by this run (arc42
    # §5.11) — every `scores` row cites `config_version_id`, so without this
    # write no historical score replayed by this bootstrap could be
    # reproduced under its original weights/policy/taxonomy. No pipeline
    # step performs this write; it must happen here, once, before the run.
    config_version_repo = CosmosRepository(cosmos, "config_versions", ConfigVersion)
    await config_version_repo.upsert(config_version)

    # Channel A/B extraction + narrative generation (arc42 §5.4, §6.3 step 7),
    # same construction as the nightly pipeline — absent if the endpoint
    # can't be reached, in which case extraction steps are skipped rather
    # than aborting the multi-hour run.
    openai_client = None
    try:
        openai_client = AzureOpenAIClient(
            endpoint=settings.aoai_endpoint,
            api_version=settings.aoai_api_version,
            tokens_per_minute=settings.aoai_tokens_per_minute,
            tokens_per_minute_by_deployment={
                settings.aoai_deployment_narrative: settings.aoai_narrative_tokens_per_minute,
                settings.aoai_deployment_answer: settings.aoai_narrative_tokens_per_minute,
            },
        )
    except Exception:  # noqa: BLE001 - degrade to no LLM extraction, do not abort the run
        logger.warning(
            "could not construct Azure OpenAI client; Channel A/B extraction will be skipped "
            "for this bootstrap run",
            exc_info=True,
        )

    providers = PipelineProviders(
        price_provider=default_providers.price_and_fx,
        fx_provider=default_providers.price_and_fx,
        news_provider=default_providers.news,
        edgar_client=default_providers.edgar,
        openai_client=openai_client,
        portfolio_reader=adapter,
    )

    # Reuse the ready-made Cosmos sink adapters from auspex.persistence.repositories
    # (arc42 §5.3, §5.4, §5.11) rather than re-declaring local duplicates —
    # channel_a_sink/channel_b_sink/narrative_sink are wired here (mirrored by
    # _run_pipeline_command) since bootstrap's whole purpose (arc42 §6.3 step 7)
    # is to actually run extraction over the 18-month window.
    repos = PipelineRepos(
        document_sink=CosmosDocumentSink(cosmos),
        price_sink=CosmosPriceSink(cosmos),
        fx_sink=CosmosFxSink(cosmos),
        fundamental_sink=CosmosFundamentalSink(cosmos),
        blob_sink=blob,
        watermarks=CosmosWatermarkStore(cosmos),
        channel_a_sink=CosmosChannelAExtractionSink(cosmos),
        channel_b_sink=CosmosChannelBDigestSink(cosmos),
        narrative_sink=CosmosNarrativeSink(cosmos),
        score_repo=CosmosRepository(cosmos, "scores", ScoreSnapshot),
        leg_change_repo=CosmosRepository(cosmos, "leg_changes", LegChange),
        recommendation_repo=CosmosRepository(cosmos, "recommendations", Recommendation),
        run_repo=CosmosRepository(cosmos, "runs", RunManifest),
        portfolio_projection_repo=CosmosRepository(cosmos, "portfolio_projection", PortfolioProjection),
        user_settings_repo=CosmosRepository(
            cosmos,
            "user_settings",
            UserSettings,
        ),
        config_version_repo=config_version_repo,
    )

    def context_factory(as_of: date) -> PipelineContext:
        """Shares the same repos/providers across the whole backfill window
        (per ``BootstrapRunner``'s contract) while giving every step/replayed
        day its own scratch state (new-document/accession tracking, etc)."""

        ctx = PipelineContext(
            universe=universe,
            config=config,
            as_of_date=as_of,
            user_id=user_id,
            repos=repos,
            providers=providers,
            hard_timeout_minutes=resolve_hard_timeout_minutes(config, settings),
            step_timeout_minutes=resolve_step_timeout_minutes(config, settings),
        )
        ctx.__dict__["_config_version_id"] = config_version.id
        return ctx

    runner = BootstrapRunner(universe=universe, context_factory=context_factory)

    try:
        if recovery_only:
            seed_ctx = context_factory(today)
            binding = await runner.bind_and_validate_portfolio(
                adapter,
                today,
                confirmed=settings.confirm_portfolio_binding,
            )
            await runner.extract_and_collect_fundamentals(
                seed_ctx,
                include_fundamentals=True,
            )
            start_date = extraction_backfill_start(today)
            (
                sessions_scored,
                sessions_meeting_security_threshold,
                completed_dates,
            ) = await runner.existing_replay_coverage(seed_ctx, start_date, today)
            if replay_all or sessions_meeting_security_threshold < MIN_SESSIONS_SCORED:
                await runner.replay_scoring(
                    start_date,
                    today,
                    completed_dates=set() if replay_all else completed_dates,
                )
                (
                    sessions_scored,
                    sessions_meeting_security_threshold,
                    _,
                ) = await runner.existing_replay_coverage(seed_ctx, start_date, today)
            metrics = await runner.compute_performance_metrics(
                seed_ctx,
                performance_repo=CosmosRepository(
                    cosmos, "performance", PerformanceMetric
                ),
            )
            passed = (
                runner.validate(
                    sessions_scored, sessions_meeting_security_threshold
                )
                and binding.is_valid
                and bool(metrics)
            )
            logger.info(
                "bootstrap recovery complete — sessions_scored=%d, "
                "sessions_meeting_security_threshold=%d, performance_metrics=%d, "
                "validation_passed=%s",
                sessions_scored,
                sessions_meeting_security_threshold,
                len(metrics),
                passed,
            )
            return 0 if passed else 1

        company_tickers = await default_providers.edgar.get_company_tickers()
        report = await runner.run(
            as_of_date=today,
            company_tickers=company_tickers,
            edgar_client=default_providers.edgar,
            user_agent=settings.edgar_user_agent,
            rate_limit_per_second=settings.edgar_rate_limit_per_second,
            portfolio_adapter=adapter,
            confirmed=settings.confirm_portfolio_binding,
            blob_sink=blob,
            performance_repo=CosmosRepository(cosmos, "performance", PerformanceMetric),
        )
    except PortfolioBindingNotConfirmedError as exc:
        logger.error("bootstrap: halted before proceeding — %s", exc)
        return 1
    finally:
        await _aclose_unique(
            default_providers.price_and_fx,
            default_providers.news,
            default_providers.edgar,
            openai_client,
            secret_resolver,
            blob,
            source_ledger,
            cosmos,
        )

    if report.cik_mismatches:
        logger.warning("bootstrap: %d CIK mismatch(es): %s", len(report.cik_mismatches), report.cik_mismatches)
    if report.filer_profile_mismatches:
        logger.warning(
            "bootstrap: %d filer_profile mismatch(es): %s",
            len(report.filer_profile_mismatches),
            report.filer_profile_mismatches,
        )

    logger.info(
        "bootstrap: complete — sessions_scored=%d, sessions_meeting_security_threshold=%d, "
        "bytes_transferred=%d, performance_metrics=%d, validation_passed=%s",
        report.sessions_scored,
        report.sessions_meeting_security_threshold,
        report.bytes_transferred,
        len(report.performance_metrics),
        report.validation_passed,
    )
    return 0 if report.validation_passed else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Provider credentials are query parameters for some upstream APIs.
    # httpx logs full request URLs at INFO, so production logs must never
    # inherit that level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "nightly":
        return asyncio.run(_run_pipeline_command(_parse_date(args.date)))
    if args.command == "bootstrap":
        return asyncio.run(_bootstrap_command())
    if args.command == "bootstrap-recover":
        return asyncio.run(
            _bootstrap_command(
                recovery_only=True,
                replay_all=args.replay_all,
            )
        )
    if args.command == "bootstrap-audit":
        return asyncio.run(_bootstrap_audit_command())
    if args.command == "seed-edgar-watermarks":
        return asyncio.run(_seed_edgar_watermarks_command())
    if args.command == "migrate-multi-user":
        return asyncio.run(_migrate_multi_user_command())
    if args.command == "performance":
        return asyncio.run(_performance_command(_parse_date(args.date)))
    if args.command == "shadow":
        return asyncio.run(_shadow_command(_parse_date(args.date), publish=args.publish))
    if args.command == "engine-baseline-export":
        from auspex.cli.engine_baseline import export_engine_baseline_command

        return asyncio.run(export_engine_baseline_command(args.label))
    if args.command == "derived-cleanup":
        from auspex.cli.derived_cleanup import cleanup_derived_command

        return asyncio.run(cleanup_derived_command(apply=args.apply))
    if args.command == "market-data-diagnose":
        from auspex.cli.market_data import market_data_diagnose_command

        return asyncio.run(market_data_diagnose_command(args.ticker, as_json=args.json))
    if args.command == "market-data-repair":
        from auspex.cli.market_data import market_data_repair_command

        return asyncio.run(
            market_data_repair_command(args.ticker, dry_run=args.dry_run, as_json=args.json)
        )
    if args.command == "serve":
        return _serve_command(args.host, args.port)

    parser.print_help()  # pragma: no cover - defensive, argparse enforces required subcommand
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
