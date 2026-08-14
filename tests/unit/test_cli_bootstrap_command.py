"""Unit tests for `_bootstrap_command`'s end-to-end wiring (arc42 §6.3): settings/
universe/config loading, portfolio-adapter construction (fatal-if-it-fails),
provider/repository wiring reused from `_run_pipeline_command`'s established
pattern, and the confirmation-gated `BootstrapRunner.run(...)` call whose
`BootstrapReport.validation_passed` becomes the process exit code.

`BootstrapRunner` itself (bulk archive streaming, 36/18-month backfill,
day-by-day score replay, the >=85-securities/>=370-of-378-sessions validation
gate) is exercised by ``test_bootstrap_bulk_archives.py`` and
``test_bootstrap_portfolio_binding.py``; here it is faked out so these tests
stay fast/deterministic and focus purely on `_bootstrap_command`'s own
orchestration and exit-code contract.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from auspex.cli.bootstrap import BootstrapReport, PortfolioBindingNotConfirmedError
from auspex.cli.main import _bootstrap_command
from auspex.config.loader import Universe
from auspex.models.enums import FilerProfile
from auspex.models.security import Security
from auspex.portfolio.port import PortfolioSnapshot
from auspex.settings import Settings


class FakePortfolioAdapter:
    def __init__(
        self,
        snapshot: PortfolioSnapshot,
        sample: dict | None,
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
    """Stands in for `DefaultProviders.edgar` — only the two methods
    `_bootstrap_command` itself calls directly (`get_company_tickers` before
    the run, `aclose` in the `finally` block) need to exist."""

    def __init__(self) -> None:
        self.aclose_called = False

    async def get_company_tickers(self) -> dict:
        return {"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"}}

    async def aclose(self) -> None:
        self.aclose_called = True


class FakeDefaultProviders:
    def __init__(self) -> None:
        self.price_and_fx = None
        self.news = None
        self.edgar = FakeEdgarClient()


class FakeAzureOpenAIClient:
    """Stands in for `providers.openai_provider.AzureOpenAIClient` — records
    the kwargs it was constructed with (so tests can assert they came from
    `Settings`, not hardcoded) and whether `aclose` (the `finally`-block
    cleanup `_bootstrap_command` must perform) was actually invoked."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.aclose_called = False

    async def aclose(self) -> None:
        self.aclose_called = True


class FakeBootstrapRunner:
    """Replaces `auspex.cli.bootstrap.BootstrapRunner` so tests exercise only
    `_bootstrap_command`'s wiring/branching, not the full 12-step orchestration
    (bulk downloads, ~378-day backfill, etc.) that class implements."""

    last_instance: FakeBootstrapRunner | None = None

    def __init__(self, *, universe, context_factory) -> None:
        self.universe = universe
        self.context_factory = context_factory
        self.run_kwargs: dict | None = None
        FakeBootstrapRunner.last_instance = self

    async def run(self, **kwargs) -> BootstrapReport:
        self.run_kwargs = kwargs
        if not kwargs["confirmed"]:
            raise PortfolioBindingNotConfirmedError(
                "owner has not set AUSPEX_CONFIRM_PORTFOLIO_BINDING=true"
            )
        return BootstrapReport(
            filer_profile_mismatches=[],
            sessions_scored=378,
            sessions_meeting_security_threshold=370,
            portfolio_binding=None,
            validation_passed=True,
            bytes_transferred=1_000,
        )


class FakeFailingBootstrapRunner(FakeBootstrapRunner):
    """Simulates the >=85-securities/>=370-of-378-sessions gate failing
    (arc42 §6.3 step 12) even though confirmation itself succeeded."""

    async def run(self, **kwargs) -> BootstrapReport:
        self.run_kwargs = kwargs
        return BootstrapReport(
            filer_profile_mismatches=[],
            sessions_scored=200,
            sessions_meeting_security_threshold=50,
            portfolio_binding=None,
            validation_passed=False,
        )


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


def _no_llm(**_kw):
    raise RuntimeError("no Azure OpenAI endpoint reachable in tests")


class FakeContainer:
    """Stands in for `azure.cosmos.ContainerProxy` — records every
    `upsert_item` call (e.g. the `config_version_repo.upsert(...)` write
    `_bootstrap_command` performs directly, ahead of the `BootstrapRunner`
    run) without touching Cosmos DB."""

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


def patch_common(
    monkeypatch,
    *,
    settings: Settings,
    adapter,
    runner_cls=FakeBootstrapRunner,
    openai_client_factory=_no_llm,
    cosmos: FakeCosmosContext | None = None,
) -> None:
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

    # Default: no reachable AOAI endpoint, `openai_client` stays None — the
    # dedicated `TestBootstrapCommandOpenAIClientCleanup` tests below override
    # this with a working `FakeAzureOpenAIClient` factory to exercise the
    # construction/wiring/`aclose` cleanup path instead.
    monkeypatch.setattr("auspex.providers.openai_provider.AzureOpenAIClient", openai_client_factory)
    monkeypatch.setattr("auspex.cli.bootstrap.BootstrapRunner", runner_cls)


class TestBootstrapCommandConfirmationWiring:
    @pytest.mark.asyncio
    async def test_returns_nonzero_when_not_confirmed(self, monkeypatch):
        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot, sample=None)
        settings = Settings(confirm_portfolio_binding=False)
        patch_common(monkeypatch, settings=settings, adapter=adapter)

        exit_code = await _bootstrap_command()

        assert exit_code == 1
        # The confirmation gate is enforced inside BootstrapRunner.run(...) —
        # confirm it was actually invoked (and thus reached) rather than the
        # command short-circuiting before wiring the runner at all.
        assert FakeBootstrapRunner.last_instance is not None
        assert FakeBootstrapRunner.last_instance.run_kwargs["confirmed"] is False
        assert FakeBootstrapRunner.last_instance.run_kwargs["edgar_client"].aclose_called is True

    @pytest.mark.asyncio
    async def test_returns_zero_when_confirmed(self, monkeypatch):
        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot, sample=None, user_sk="cust-abc123")
        settings = Settings(confirm_portfolio_binding=True)
        patch_common(monkeypatch, settings=settings, adapter=adapter)

        exit_code = await _bootstrap_command()

        assert exit_code == 0
        runner = FakeBootstrapRunner.last_instance
        assert runner is not None
        assert runner.run_kwargs["confirmed"] is True
        assert runner.run_kwargs["user_agent"] == settings.edgar_user_agent
        assert runner.run_kwargs["rate_limit_per_second"] == settings.edgar_rate_limit_per_second
        assert runner.run_kwargs["portfolio_adapter"] is adapter
        assert runner.run_kwargs["edgar_client"].aclose_called is True
        # company_tickers came from the (faked) EDGAR client, not a stub/no-op.
        assert runner.run_kwargs["company_tickers"] == {
            "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"}
        }
        # The `user_id` every PipelineContext is built with must be the
        # adapter's *resolved* owner user_sk — never a CLI-arg/placeholder
        # value such as the literal "owner".
        ctx = runner.context_factory(date(2026, 8, 8))
        assert ctx.user_id == "cust-abc123"

    @pytest.mark.asyncio
    async def test_pipeline_repos_channel_and_config_version_sinks_are_wired(self, monkeypatch):
        """arc42 §6.3 step 7 requires the 18-month extraction/digest/
        narration window to actually persist — regression guard for the
        `PipelineRepos` construction bug where `channel_a_sink`,
        `channel_b_sink`, `narrative_sink`, and `config_version_repo` were
        all left `None` defaults, and for the watermark store being
        mis-wired onto the `config_versions` container instead of its own
        `watermarks` container."""

        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot, sample=None, user_sk="cust-abc123")
        settings = Settings(confirm_portfolio_binding=True)
        cosmos = FakeCosmosContext()
        patch_common(monkeypatch, settings=settings, adapter=adapter, cosmos=cosmos)

        exit_code = await _bootstrap_command()

        assert exit_code == 0
        runner = FakeBootstrapRunner.last_instance
        assert runner is not None
        ctx = runner.context_factory(date(2026, 8, 8))
        repos = ctx.repos
        assert repos.channel_a_sink is not None
        assert repos.channel_b_sink is not None
        assert repos.narrative_sink is not None
        assert repos.config_version_repo is not None
        # The config_version write must reach the real `config_versions`
        # container, not a mis-wired one such as `watermarks`.
        assert cosmos.containers["config_versions"].upserted_items
        # The watermark store must use its correct default container, not
        # the `config_versions` container it was previously (buggily)
        # overridden to share (`container_name="config_versions"`).
        assert repos.watermarks._context is cosmos  # noqa: SLF001 - white-box regression guard
        assert repos.watermarks._container_name == "watermarks"  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_returns_nonzero_when_adapter_construction_fails(self, monkeypatch):
        settings = Settings(confirm_portfolio_binding=True)
        monkeypatch.setattr("auspex.config.load_universe", make_universe)
        monkeypatch.setattr("auspex.settings.get_settings", lambda: settings)

        def _raise():
            raise RuntimeError("mapping file missing")

        monkeypatch.setattr("auspex.portfolio.mapping.load_portfolio_mapping", _raise)

        exit_code = await _bootstrap_command()

        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_returns_nonzero_when_owner_resolution_fails(self, monkeypatch):
        """Binding absence/ambiguity (e.g. no static owner_user_sk and no
        unique resolvable app_users document) must hard-fail the command —
        it must NOT degrade to an empty book or a placeholder user_id."""

        class _UnresolvableOwnerAdapter(FakePortfolioAdapter):
            async def resolve_owner_user_sk(self) -> str:
                raise RuntimeError("owner_user_sk is a placeholder and identity_mapping is ambiguous")

        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = _UnresolvableOwnerAdapter(snapshot, sample=None)
        settings = Settings(confirm_portfolio_binding=True)
        patch_common(monkeypatch, settings=settings, adapter=adapter)
        FakeBootstrapRunner.last_instance = None

        exit_code = await _bootstrap_command()

        assert exit_code == 1
        # Must fail before ever constructing/invoking BootstrapRunner.
        assert FakeBootstrapRunner.last_instance is None

    @pytest.mark.asyncio
    async def test_returns_nonzero_when_validation_gate_fails(self, monkeypatch):
        """arc42 §6.3 step 12: confirmation can succeed while the
        >=85-securities/>=370-of-378-sessions coverage gate still fails —
        the command must still exit non-zero in that case."""

        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot, sample=None)
        settings = Settings(confirm_portfolio_binding=True)
        patch_common(monkeypatch, settings=settings, adapter=adapter, runner_cls=FakeFailingBootstrapRunner)

        exit_code = await _bootstrap_command()

        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_edgar_client_closed_even_when_runner_raises_unexpectedly(self, monkeypatch):
        """The EDGAR client must be released (arc42 TC — long-running process
        hygiene) even when `BootstrapRunner.run` fails for a reason other
        than the confirmation gate."""

        class FakeExplodingBootstrapRunner(FakeBootstrapRunner):
            async def run(self, **kwargs):
                self.run_kwargs = kwargs
                raise RuntimeError("bulk archive download failed")

        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot, sample=None)
        settings = Settings(confirm_portfolio_binding=True)
        patch_common(monkeypatch, settings=settings, adapter=adapter, runner_cls=FakeExplodingBootstrapRunner)

        with pytest.raises(RuntimeError, match="bulk archive download failed"):
            await _bootstrap_command()

        assert FakeBootstrapRunner.last_instance.run_kwargs["edgar_client"].aclose_called is True


class TestBootstrapCommandOpenAIClientCleanup:
    """`PipelineProviders` must actually receive a live `AzureOpenAIClient`
    built from `Settings`' AOAI endpoint/api-version/tokens-per-minute (not
    left `None`/hardcoded) whenever the endpoint is reachable, and that
    client must be released via `aclose()` in the `finally` block regardless
    of whether `BootstrapRunner.run(...)` succeeds or raises — mirroring the
    pre-existing EDGAR-client cleanup contract above."""

    @pytest.mark.asyncio
    async def test_openai_client_constructed_from_settings_and_closed_after_successful_run(self, monkeypatch):
        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot, sample=None)
        settings = Settings(confirm_portfolio_binding=True)
        constructed: list[FakeAzureOpenAIClient] = []

        def _make_client(**kwargs):
            client = FakeAzureOpenAIClient(**kwargs)
            constructed.append(client)
            return client

        patch_common(monkeypatch, settings=settings, adapter=adapter, openai_client_factory=_make_client)

        exit_code = await _bootstrap_command()

        assert exit_code == 0
        assert len(constructed) == 1
        client = constructed[0]
        # Sourced from Settings' AOAI fields, never hardcoded/omitted.
        assert client.kwargs["endpoint"] == settings.aoai_endpoint
        assert client.kwargs["api_version"] == settings.aoai_api_version
        assert client.kwargs["tokens_per_minute"] == settings.aoai_tokens_per_minute
        assert client.kwargs["tokens_per_minute_by_deployment"] == {
            settings.aoai_deployment_narrative: settings.aoai_narrative_tokens_per_minute,
            settings.aoai_deployment_answer: settings.aoai_narrative_tokens_per_minute,
        }
        # Actually reaches PipelineProviders, not discarded after construction.
        ctx = FakeBootstrapRunner.last_instance.context_factory(date(2026, 8, 8))
        assert ctx.providers.openai_client is client
        # Released after the run completes.
        assert client.aclose_called is True

    @pytest.mark.asyncio
    async def test_openai_client_closed_even_when_runner_raises_unexpectedly(self, monkeypatch):
        class FakeExplodingBootstrapRunner(FakeBootstrapRunner):
            async def run(self, **kwargs):
                self.run_kwargs = kwargs
                raise RuntimeError("bulk archive download failed")

        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot, sample=None)
        settings = Settings(confirm_portfolio_binding=True)
        constructed: list[FakeAzureOpenAIClient] = []

        def _make_client(**kwargs):
            client = FakeAzureOpenAIClient(**kwargs)
            constructed.append(client)
            return client

        patch_common(
            monkeypatch,
            settings=settings,
            adapter=adapter,
            runner_cls=FakeExplodingBootstrapRunner,
            openai_client_factory=_make_client,
        )

        with pytest.raises(RuntimeError, match="bulk archive download failed"):
            await _bootstrap_command()

        assert len(constructed) == 1
        assert constructed[0].aclose_called is True

    @pytest.mark.asyncio
    async def test_openai_client_stays_none_when_endpoint_unreachable(self, monkeypatch):
        """Unchanged pre-existing behaviour: a construction failure degrades
        to `openai_client=None` (extraction/narrative skipped) rather than
        aborting the bootstrap run, and the `finally` block must not attempt
        to close a `None` client."""

        snapshot = PortfolioSnapshot(holdings=[], cash_chf=Decimal(0), as_of=date(2026, 8, 8), lot_level=True)
        adapter = FakePortfolioAdapter(snapshot, sample=None)
        settings = Settings(confirm_portfolio_binding=True)
        patch_common(monkeypatch, settings=settings, adapter=adapter)

        exit_code = await _bootstrap_command()

        assert exit_code == 0
        ctx = FakeBootstrapRunner.last_instance.context_factory(date(2026, 8, 8))
        assert ctx.providers.openai_client is None
