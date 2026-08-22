"""Unit tests for the pipeline timeout budget (arc42 §6.1).

``PipelineContext.hard_timeout_minutes`` used to be a literal 45 that no caller
overrode, so ``Settings.pipeline_hard_timeout_minutes`` and
``config/policy.yaml``'s ``pipeline.hard_timeout_minutes`` were read by nothing.
Worse, the deadline was only evaluated *between* steps, so one step hanging on a
provider call ran until the container job's own replica timeout — 21 600 s, an
8x wider ceiling than the configuration promises.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from auspex.models.enums import RunStatus
from auspex.models.run import PIPELINE_STEPS
from auspex.persistence.memory import (
    InMemoryBlobSink,
    InMemoryDocumentSink,
    InMemoryFundamentalSink,
    InMemoryFxSink,
    InMemoryPriceSink,
    InMemoryWatermarkStore,
)
from auspex.pipeline.context import (
    DEFAULT_HARD_TIMEOUT_MINUTES,
    PipelineContext,
    PipelineRepos,
    resolve_hard_timeout_minutes,
    resolve_step_timeout_minutes,
)
from auspex.pipeline.runner import PipelineRunner
from auspex.settings import Settings

AS_OF = date(2026, 8, 20)


def _repos() -> PipelineRepos:
    return PipelineRepos(
        document_sink=InMemoryDocumentSink(),
        price_sink=InMemoryPriceSink(),
        fx_sink=InMemoryFxSink(),
        fundamental_sink=InMemoryFundamentalSink(),
        blob_sink=InMemoryBlobSink(),
        watermarks=InMemoryWatermarkStore(),
    )


def _context(**kwargs) -> PipelineContext:
    return PipelineContext(
        universe=object(),
        config={"policy": {}},
        as_of_date=AS_OF,
        user_id="owner",
        repos=_repos(),
        **kwargs,
    )


class TestResolution:
    def test_the_versioned_config_value_is_used(self):
        config = {"policy": {"pipeline": {"hard_timeout_minutes": 30}}}
        assert resolve_hard_timeout_minutes(config, Settings()) == 30

    def test_an_explicit_environment_override_wins_over_config(self, monkeypatch):
        """An operator changing a deployment's deadline must not be silently
        overruled by a value committed to the repository."""

        monkeypatch.setenv("AUSPEX_PIPELINE_HARD_TIMEOUT_MINUTES", "12")
        config = {"policy": {"pipeline": {"hard_timeout_minutes": 30}}}
        assert resolve_hard_timeout_minutes(config, Settings()) == 12

    def test_an_override_equal_to_the_default_still_wins(self):
        """Detected from the populated field set, not by comparing values, so
        deliberately pinning the default does not hand control back to YAML."""

        settings = Settings(pipeline_hard_timeout_minutes=DEFAULT_HARD_TIMEOUT_MINUTES)
        config = {"policy": {"pipeline": {"hard_timeout_minutes": 30}}}
        assert resolve_hard_timeout_minutes(config, settings) == DEFAULT_HARD_TIMEOUT_MINUTES

    def test_falls_back_to_settings_without_a_config_value(self):
        assert resolve_hard_timeout_minutes({}, Settings()) == Settings().pipeline_hard_timeout_minutes

    def test_a_malformed_config_value_falls_back_rather_than_raising(self):
        config = {"policy": {"pipeline": {"hard_timeout_minutes": "soon"}}}
        assert resolve_hard_timeout_minutes(config, Settings()) == Settings().pipeline_hard_timeout_minutes

    def test_the_committed_config_is_readable(self):
        from auspex.config import load_policy

        assert resolve_hard_timeout_minutes({"policy": load_policy()}, Settings()) > 0


class TestStepCeilingResolution:
    """The per-step ceiling is configured, not derived from the run deadline.

    Defaulting it to ``hard_timeout_minutes`` let one hung step burn the whole
    night before any later step was attempted — bounded, but only marginally
    better than the container job's own replica timeout.
    """

    def test_the_versioned_config_value_is_used(self):
        config = {"policy": {"pipeline": {"step_timeout_minutes": 9}}}
        assert resolve_step_timeout_minutes(config, Settings()) == 9

    def test_an_explicit_environment_override_wins_over_config(self, monkeypatch):
        monkeypatch.setenv("AUSPEX_PIPELINE_STEP_TIMEOUT_MINUTES", "4")
        config = {"policy": {"pipeline": {"step_timeout_minutes": 9}}}
        assert resolve_step_timeout_minutes(config, Settings()) == 4

    def test_falls_back_to_settings_without_a_config_value(self):
        assert resolve_step_timeout_minutes({}, Settings()) == Settings().pipeline_step_timeout_minutes

    def test_the_committed_config_bounds_a_step_well_inside_the_run(self):
        from auspex.config import load_policy

        config = {"policy": load_policy()}
        step = resolve_step_timeout_minutes(config, Settings())
        run = resolve_hard_timeout_minutes(config, Settings())
        assert 0 < step < run


class TestPerStepBudget:
    def test_a_step_may_never_outlive_the_run_budget(self):
        ctx = _context(hard_timeout_minutes=45)
        assert ctx.step_budget_seconds(0) == 45 * 60
        assert ctx.step_budget_seconds(44 * 60) == 60

    def test_an_exhausted_budget_leaves_no_time_at_all(self):
        ctx = _context(hard_timeout_minutes=45)
        assert ctx.step_budget_seconds(46 * 60) == 0

    def test_an_explicit_step_ceiling_tightens_the_bound(self):
        ctx = _context(hard_timeout_minutes=45, step_timeout_minutes=5)
        assert ctx.step_budget_seconds(0) == 5 * 60

    def test_the_ceiling_never_loosens_the_run_budget(self):
        ctx = _context(hard_timeout_minutes=45, step_timeout_minutes=90)
        assert ctx.step_budget_seconds(0) == 45 * 60

    def test_a_derived_user_context_inherits_both_bounds(self):
        ctx = _context(hard_timeout_minutes=20, step_timeout_minutes=3)
        derived = ctx.derive_for_user("user-bob")
        assert derived.hard_timeout_minutes == 20
        assert derived.step_timeout_minutes == 3


class TestRunnerEnforcement:
    """The deadline must bind *inside* a step, not only between steps."""

    @staticmethod
    def _manifest_started_seconds_ago(seconds: float):
        from auspex.models.common import utc_now
        from auspex.pipeline.manifest import new_manifest

        manifest = new_manifest(AS_OF)
        manifest.started_at = utc_now() - timedelta(seconds=seconds)
        return manifest

    @pytest.mark.asyncio
    async def test_a_hung_step_times_the_run_out_instead_of_running_forever(self, monkeypatch):
        from auspex.pipeline import runner as runner_module

        async def instant(ctx, manifest):
            return None

        async def hangs(ctx, manifest):
            await asyncio.sleep(3600)

        for step_name in PIPELINE_STEPS:
            monkeypatch.setitem(runner_module.STEP_FUNCTIONS, step_name, instant)
        monkeypatch.setitem(runner_module.STEP_FUNCTIONS, "COMPUTE_RAW_LEGS", hangs)

        ctx = _context(hard_timeout_minutes=1)
        # Half a second of budget left: enough to prove the hung step is cut
        # off, without the test itself waiting on a real deadline.
        manifest = await asyncio.wait_for(
            PipelineRunner(ctx).run(self._manifest_started_seconds_ago(59.5)),
            timeout=30,
        )

        assert manifest.status == RunStatus.TIMEOUT
        assert manifest.watermarks_committed is False
        checkpoint = manifest.step_by_name("COMPUTE_RAW_LEGS")
        assert checkpoint.status == "FAILED"
        assert "timeout budget" in checkpoint.detail

    @pytest.mark.asyncio
    async def test_steps_after_the_deadline_are_not_started_at_all(self, monkeypatch):
        from auspex.pipeline import runner as runner_module

        started: list[str] = []

        async def record(ctx, manifest):
            started.append("ran")

        for step_name in PIPELINE_STEPS:
            monkeypatch.setitem(runner_module.STEP_FUNCTIONS, step_name, record)

        ctx = _context(hard_timeout_minutes=1)
        manifest = await PipelineRunner(ctx).run(self._manifest_started_seconds_ago(120))

        assert manifest.status == RunStatus.TIMEOUT
        assert manifest.watermarks_committed is False
        assert started == []

    @pytest.mark.asyncio
    async def test_a_run_inside_its_budget_still_completes_normally(self, monkeypatch):
        from auspex.pipeline import runner as runner_module
        from auspex.pipeline.manifest import complete_step, start_step

        def checkpointing(step_name: str):
            async def _step(ctx, manifest):
                start_step(manifest, step_name)
                complete_step(manifest, step_name)

            return _step

        for step_name in PIPELINE_STEPS:
            monkeypatch.setitem(runner_module.STEP_FUNCTIONS, step_name, checkpointing(step_name))

        manifest = await PipelineRunner(_context(hard_timeout_minutes=45)).run()
        assert manifest.status != RunStatus.TIMEOUT
        assert [c.status for c in manifest.steps] == ["SUCCESS"] * len(PIPELINE_STEPS)


class TestPerUserStageBudget:
    """The fan-out must not restart the run clock for each user.

    Per-user steps checkpoint onto a throwaway scratch manifest created fresh
    for every user. Measuring the deadline against *that* handed the last user
    of an already-overrunning night a full budget, so a roster of users could
    walk the run arbitrarily far past its configured deadline one user at a
    time. The shared run's start is the anchor.
    """

    @staticmethod
    def _shared_manifest(started_seconds_ago: float):
        from auspex.models.common import utc_now
        from auspex.pipeline.manifest import new_manifest

        manifest = new_manifest(AS_OF)
        manifest.started_at = utc_now() - timedelta(seconds=started_seconds_ago)
        return manifest

    @pytest.mark.asyncio
    async def test_a_user_stage_is_bounded_by_the_shared_run_deadline(self, monkeypatch):
        from auspex.pipeline import runner as runner_module
        from auspex.pipeline.fanout import PER_USER_STEPS, run_user_stage

        async def hangs(ctx, manifest):
            await asyncio.sleep(3600)

        for step_name in PER_USER_STEPS:
            monkeypatch.setitem(runner_module.STEP_FUNCTIONS, step_name, hangs)

        ctx = _context(hard_timeout_minutes=1)
        result = await asyncio.wait_for(
            run_user_stage(ctx, self._shared_manifest(59.5), "user-bob"),
            timeout=30,
        )

        assert result.succeeded is False
        assert "timeout budget" in result.detail

    @pytest.mark.asyncio
    async def test_an_exhausted_shared_deadline_starts_no_user_step_at_all(self, monkeypatch):
        from auspex.pipeline import runner as runner_module
        from auspex.pipeline.fanout import PER_USER_STEPS, run_user_stage

        started: list[str] = []

        async def record(ctx, manifest):
            started.append(ctx.user_id)

        for step_name in PER_USER_STEPS:
            monkeypatch.setitem(runner_module.STEP_FUNCTIONS, step_name, record)

        ctx = _context(hard_timeout_minutes=1)
        result = await run_user_stage(ctx, self._shared_manifest(600), "user-bob")

        assert result.succeeded is False
        assert started == []

    @pytest.mark.asyncio
    async def test_a_user_stage_inside_the_shared_budget_still_succeeds(self, monkeypatch):
        from auspex.pipeline import runner as runner_module
        from auspex.pipeline.fanout import PER_USER_STEPS, run_user_stage

        served: list[str] = []

        async def record(ctx, manifest):
            served.append(ctx.user_id)

        for step_name in PER_USER_STEPS:
            monkeypatch.setitem(runner_module.STEP_FUNCTIONS, step_name, record)

        ctx = _context(hard_timeout_minutes=45)
        result = await run_user_stage(ctx, self._shared_manifest(1), "user-bob")

        assert result.succeeded is True
        assert served == ["user-bob"] * len(PER_USER_STEPS)
