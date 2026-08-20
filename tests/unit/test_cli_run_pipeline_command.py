"""Unit tests for `_run_pipeline_command`'s end-to-end wiring (arc42 §6.1):
owner-identity resolution (fatal-if-it-fails, same contract as
`_bootstrap_command`), default-provider/Cosmos-repo construction, the
Azure OpenAI client's construction-from-`Settings`/wiring-into-
`PipelineProviders`, and the `finally`-block cleanup that must release both
the EDGAR client and the Azure OpenAI client regardless of how
`run_pipeline_wrapper` (and thus `run_nightly_pipeline`) exits.

`run_nightly_pipeline`'s own 20-step orchestration is exercised by the
pipeline/integration test suites; here `run_pipeline_wrapper` is faked out so
these tests stay fast/deterministic and focus purely on `_run_pipeline_command`'s
own construction/branching/cleanup contract, mirroring the pattern already
established for `_bootstrap_command` in `test_cli_bootstrap_command.py`.
"""

from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from auspex.cli.main import _run_pipeline_command
from auspex.config.loader import Universe
from auspex.models.enums import FilerProfile, RunStatus
from auspex.models.security import Security
from auspex.portfolio.port import PortfolioSnapshot
from auspex.settings import Settings


class FakePortfolioAdapter:
    def __init__(
        self,
        snapshot: PortfolioSnapshot,
        sample: dict | None = None,
        *,
        user_sk: str = "resolved-owner-user-sk",
    ) -> None:
        self._snapshot = snapshot
        self._sample = sample
        self._user_sk = user_sk

    async def read_snapshot(self, as_of: date, fx_rate_to_chf=None) -> PortfolioSnapshot:
        return self._snapshot

    async def sample_holding_document(self) -> dict | None:
        return self._sample

    async def resolve_owner_user_sk(self) -> str:
        return self._user_sk


class FakeEdgarClient:
    """Stands in for `DefaultProviders.edgar` — only `aclose` (the `finally`-
    block cleanup `_run_pipeline_command` performs) is exercised."""

    def __init__(self) -> None:
        self.aclose_called = False

    async def aclose(self) -> None:
        self.aclose_called = True


class FakeAzureOpenAIClient:
    """Stands in for `providers.openai_provider.AzureOpenAIClient` — records
    its construction kwargs (so tests can assert they came from `Settings`)
    and whether `aclose` was invoked during cleanup."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.aclose_called = False

    async def aclose(self) -> None:
        self.aclose_called = True


def _no_llm(**_kw):
    raise RuntimeError("no Azure OpenAI endpoint reachable in tests")


class FakeContainer:
    """Stands in for `azure.cosmos.ContainerProxy` — records every
    `upsert_item` call (e.g. the `config_version_repo.upsert(...)` write
    `_run_pipeline_command` performs directly, ahead of the pipeline run)
    without touching Cosmos DB."""

    def __init__(self) -> None:
        self.upserted_items: list[dict] = []

    async def upsert_item(self, body: dict) -> dict:
        self.upserted_items.append(body)
        return body


class FakeCosmosContext:
    """Stands in for `auspex.persistence.cosmos_client.CosmosContext` — just
    enough of the `.container(name)` protocol for `CosmosRepository.upsert`
    to succeed, keyed by container name so a test can inspect exactly which
    container a write landed in (e.g. asserting `config_versions` received
    the run's `ConfigVersion`, not some other container)."""

    def __init__(self) -> None:
        self.containers: dict[str, FakeContainer] = {}

    async def container(self, name: str) -> FakeContainer:
        return self.containers.setdefault(name, FakeContainer())


class FakeDefaultProviders:
    def __init__(self) -> None:
        self.price_and_fx = None
        self.news = None
        self.edgar = FakeEdgarClient()


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


def make_manifest(status: RunStatus = RunStatus.SUCCESS):
    return SimpleNamespace(status=status)


def make_run_result(status: RunStatus = RunStatus.SUCCESS):
    """Shape returned by the multi-user runner: a shared manifest plus
    per-user outcomes (see :mod:`auspex.pipeline.fanout`)."""

    return SimpleNamespace(manifest=make_manifest(status), user_results=[], failed_user_ids=[])


def patch_common(
    monkeypatch,
    *,
    settings: Settings,
    adapter,
    wrapper=None,
    openai_client_factory=_no_llm,
    cosmos: FakeCosmosContext | None = None,
) -> list:
    """Patches every dependency `_run_pipeline_command` constructs, mirroring
    `_bootstrap_command`'s test harness. Returns the list of `ctx` values
    `run_pipeline_wrapper` was actually invoked with (usually length <=1).
    Pass a `cosmos=FakeCosmosContext()` explicitly to inspect its
    `.containers[name].upserted_items` after the call (e.g. asserting the
    `config_version` write actually reached the `config_versions`
    container)."""

    monkeypatch.setattr("auspex.config.load_universe", make_universe)
    monkeypatch.setattr("auspex.settings.get_settings", lambda: settings)
    monkeypatch.setattr("auspex.portfolio.mapping.load_portfolio_mapping", lambda: object())
    monkeypatch.setattr("auspex.persistence.cosmos_client.get_source_ledger_context", lambda: object())
    monkeypatch.setattr("auspex.persistence.cosmos_client.get_cosmos_context", lambda: cosmos or FakeCosmosContext())
    monkeypatch.setattr("auspex.persistence.blob_client.get_blob_context", lambda: object())
    monkeypatch.setattr("auspex.portfolio.adapter.PortfolioAdapter", lambda *a, **kw: adapter)
    monkeypatch.setattr("auspex.providers.secrets.get_secret_resolver", lambda *a, **kw: object())

    async def fake_build_default_providers(_settings, _secrets):
        return FakeDefaultProviders()

    monkeypatch.setattr("auspex.providers.factory.build_default_providers", fake_build_default_providers)
    monkeypatch.setattr("auspex.providers.openai_provider.AzureOpenAIClient", openai_client_factory)

    seen_ctx: list = []

    async def fake_run_pipeline_wrapper(ctx, **kwargs):
        seen_ctx.append(ctx)
        if wrapper is not None:
            result = await wrapper(ctx)
            if hasattr(result, "manifest"):
                return result
            return SimpleNamespace(manifest=result, user_results=[], failed_user_ids=[])
        return make_run_result()

    # `auspex.cli.__init__` does `from auspex.cli.main import main`, which
    # shadows the `main` submodule with that function as a package attribute
    # — so the dotted-string form ("auspex.cli.main.run_pipeline_wrapper")
    # resolves to the wrong object. Patch the actual submodule (already in
    # `sys.modules` via the top-level `from auspex.cli.main import
    # _run_pipeline_command` import) directly instead.
    main_module = importlib.import_module("auspex.cli.main")
    monkeypatch.setattr(main_module, "run_pipeline_wrapper", fake_run_pipeline_wrapper)

    async def no_roster(_cosmos):
        return None

    monkeypatch.setattr(main_module, "_resolve_active_users", no_roster)
    return seen_ctx


class TestRunPipelineCommandOwnerResolution:
    @pytest.mark.asyncio
    async def test_returns_nonzero_when_owner_resolution_fails(self, monkeypatch):
        """Binding absence/ambiguity must hard-fail the nightly run too — it
        must NOT degrade to an empty book or a placeholder user_id (same
        contract as `_bootstrap_command`)."""

        class _UnresolvableOwnerAdapter(FakePortfolioAdapter):
            async def resolve_owner_user_sk(self) -> str:
                raise RuntimeError("owner_user_sk is a placeholder and identity_mapping is ambiguous")

        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = _UnresolvableOwnerAdapter(snapshot)
        settings = Settings()
        seen_ctx = patch_common(monkeypatch, settings=settings, adapter=adapter)

        exit_code = await _run_pipeline_command(date(2026, 8, 8))

        assert exit_code == 1
        # Must fail before ever invoking run_pipeline_wrapper.
        assert seen_ctx == []

    @pytest.mark.asyncio
    async def test_returns_nonzero_when_mapping_load_fails(self, monkeypatch):
        settings = Settings()
        monkeypatch.setattr("auspex.config.load_universe", make_universe)
        monkeypatch.setattr("auspex.settings.get_settings", lambda: settings)

        def _raise():
            raise RuntimeError("mapping file missing")

        monkeypatch.setattr("auspex.portfolio.mapping.load_portfolio_mapping", _raise)

        exit_code = await _run_pipeline_command(date(2026, 8, 8))

        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_pipeline_context_user_id_is_resolved_owner(self, monkeypatch):
        """The `user_id` every `PipelineContext` is built with must be the
        adapter's *resolved* owner user_sk — never a hardcoded/placeholder
        value such as the literal "owner"."""

        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot, user_sk="cust-abc123")
        settings = Settings()
        seen_ctx = patch_common(monkeypatch, settings=settings, adapter=adapter)

        exit_code = await _run_pipeline_command(date(2026, 8, 8))

        assert exit_code == 0
        assert len(seen_ctx) == 1
        assert seen_ctx[0].user_id == "cust-abc123"
        assert seen_ctx[0].user_id != "owner"


class TestRunPipelineCommandExitCode:
    @pytest.mark.asyncio
    async def test_returns_zero_on_success(self, monkeypatch):
        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot)
        settings = Settings()

        async def wrapper(_ctx):
            return make_manifest(RunStatus.SUCCESS)

        patch_common(monkeypatch, settings=settings, adapter=adapter, wrapper=wrapper)

        assert await _run_pipeline_command(date(2026, 8, 8)) == 0

    @pytest.mark.asyncio
    async def test_returns_zero_on_degraded(self, monkeypatch):
        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot)
        settings = Settings()

        async def wrapper(_ctx):
            return make_manifest(RunStatus.DEGRADED)

        patch_common(monkeypatch, settings=settings, adapter=adapter, wrapper=wrapper)

        assert await _run_pipeline_command(date(2026, 8, 8)) == 0

    @pytest.mark.asyncio
    async def test_returns_nonzero_on_failed(self, monkeypatch):
        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot)
        settings = Settings()

        async def wrapper(_ctx):
            return make_manifest(RunStatus.FAILED)

        patch_common(monkeypatch, settings=settings, adapter=adapter, wrapper=wrapper)

        assert await _run_pipeline_command(date(2026, 8, 8)) == 1

    @pytest.mark.asyncio
    async def test_not_a_logging_no_op(self, monkeypatch):
        """`_run_pipeline_command` must actually invoke the real pipeline
        orchestration (`run_pipeline_wrapper` / `run_nightly_pipeline`), not
        merely log and return — regression guard for the original ask."""

        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot)
        settings = Settings()
        seen_ctx = patch_common(monkeypatch, settings=settings, adapter=adapter)

        await _run_pipeline_command(date(2026, 8, 8))

        assert len(seen_ctx) == 1
        ctx = seen_ctx[0]
        assert ctx.as_of_date == date(2026, 8, 8)
        assert ctx.universe.securities  # real universe, not empty/stubbed
        assert ctx.repos.run_repo is not None
        assert ctx.repos.score_repo is not None
        assert ctx.providers.edgar_client is not None
        assert ctx.providers.portfolio_reader is adapter

    @pytest.mark.asyncio
    async def test_pipeline_repos_channel_and_config_version_sinks_are_wired(self, monkeypatch):
        """arc42 §6.1 requires extraction/digest/narration/config-persistence
        steps to actually run — regression guard for the `PipelineRepos`
        construction bug where `channel_a_sink`, `channel_b_sink`,
        `narrative_sink`, and `config_version_repo` were all left `None`
        defaults, silently skipping those steps even with an OpenAI client
        wired."""

        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot)
        settings = Settings()
        cosmos = FakeCosmosContext()
        seen_ctx = patch_common(monkeypatch, settings=settings, adapter=adapter, cosmos=cosmos)

        exit_code = await _run_pipeline_command(date(2026, 8, 8))

        assert exit_code == 0
        assert len(seen_ctx) == 1
        repos = seen_ctx[0].repos
        assert repos.channel_a_sink is not None
        assert repos.channel_b_sink is not None
        assert repos.narrative_sink is not None
        assert repos.config_version_repo is not None
        assert callable(getattr(repos.price_sink, "all", None))
        assert callable(getattr(repos.fx_sink, "all", None))
        assert callable(getattr(repos.fundamental_sink, "all", None))
        # The config_version write must reach the real `config_versions`
        # container, not a mis-wired one such as `watermarks`.
        assert cosmos.containers["config_versions"].upserted_items
        # The watermark store must use its correct default container, not
        # the `config_versions` container it was previously (buggily)
        # overridden to share.
        assert repos.watermarks._context is cosmos  # noqa: SLF001 - white-box regression guard
        assert repos.watermarks._container_name == "watermarks"  # noqa: SLF001


class TestRunPipelineCommandProviderCleanup:
    @pytest.mark.asyncio
    async def test_edgar_and_openai_clients_closed_after_successful_run(self, monkeypatch):
        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot)
        settings = Settings()
        constructed: list[FakeAzureOpenAIClient] = []

        def _make_client(**kwargs):
            client = FakeAzureOpenAIClient(**kwargs)
            constructed.append(client)
            return client

        seen_ctx = patch_common(
            monkeypatch, settings=settings, adapter=adapter, openai_client_factory=_make_client
        )

        exit_code = await _run_pipeline_command(date(2026, 8, 8))

        assert exit_code == 0
        assert len(seen_ctx) == 1
        edgar_client = seen_ctx[0].providers.edgar_client
        assert edgar_client.aclose_called is True
        assert len(constructed) == 1
        client = constructed[0]
        # Constructed from Settings' AOAI endpoint/api version/TPM fields.
        assert client.kwargs["endpoint"] == settings.aoai_endpoint
        assert client.kwargs["api_version"] == settings.aoai_api_version
        assert client.kwargs["tokens_per_minute"] == settings.aoai_tokens_per_minute
        assert client.kwargs["tokens_per_minute_by_deployment"] == {
            settings.aoai_deployment_narrative: settings.aoai_narrative_tokens_per_minute,
            settings.aoai_deployment_answer: settings.aoai_narrative_tokens_per_minute,
        }
        # Actually reaches PipelineProviders, not discarded after construction.
        assert seen_ctx[0].providers.openai_client is client
        assert client.aclose_called is True

    @pytest.mark.asyncio
    async def test_openai_client_stays_none_when_endpoint_unreachable(self, monkeypatch):
        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot)
        settings = Settings()
        seen_ctx = patch_common(monkeypatch, settings=settings, adapter=adapter)

        exit_code = await _run_pipeline_command(date(2026, 8, 8))

        assert exit_code == 0
        assert seen_ctx[0].providers.openai_client is None
        # And cleanup must not blow up trying to close a None client.
        assert seen_ctx[0].providers.edgar_client.aclose_called is True

    @pytest.mark.asyncio
    async def test_clients_closed_even_when_wrapper_raises_unexpectedly(self, monkeypatch):
        """The EDGAR/Azure OpenAI clients must be released (arc42 TC —
        long-running process hygiene) even when the pipeline run itself
        blows up for a reason other than a normal FAILED/DEGRADED status."""

        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot)
        settings = Settings()
        constructed: list[FakeAzureOpenAIClient] = []

        def _make_client(**kwargs):
            client = FakeAzureOpenAIClient(**kwargs)
            constructed.append(client)
            return client

        async def exploding_wrapper(_ctx):
            raise RuntimeError("collector step raised unexpectedly")

        seen_ctx = patch_common(
            monkeypatch,
            settings=settings,
            adapter=adapter,
            wrapper=exploding_wrapper,
            openai_client_factory=_make_client,
        )

        with pytest.raises(RuntimeError, match="collector step raised unexpectedly"):
            await _run_pipeline_command(date(2026, 8, 8))

        assert len(seen_ctx) == 1
        assert seen_ctx[0].providers.edgar_client.aclose_called is True
        assert len(constructed) == 1
        assert constructed[0].aclose_called is True
