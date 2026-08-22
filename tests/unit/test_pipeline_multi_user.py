"""Multi-user nightly: shared research once, isolated per-user fan-out.

The three properties that make the nightly job safe and affordable with more
than one user:

1. **Shared work runs once.** Ingestion, extraction and scoring are identical
   for everybody and are the expensive parts; running them per user would
   multiply provider quota and LLM spend for no information gain. Global
   score performance therefore remains a shared, population-level measure.
2. **Per-user work is isolated.** One user's portfolio, settings and
   recommendations can never bleed into another's evaluation.
3. **Failures are contained.** One user's stage failing must still leave
   everybody else with their recommendations.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

import pytest

from auspex.models.run import PIPELINE_STEPS, RunManifest
from auspex.pipeline import steps as step_fns
from auspex.pipeline.context import PipelineContext, PipelineProviders, PipelineRepos
from auspex.pipeline.fanout import (
    PER_USER_STEPS,
    SHARED_POST_STEPS,
    SHARED_PRE_STEPS,
    SHARED_STEPS,
    run_multi_user_pipeline,
    run_user_stage,
)
from auspex.pipeline.manifest import new_manifest


class NullSink:
    async def upsert(self, item) -> None:  # pragma: no cover - never exercised here
        return None


def make_repos() -> PipelineRepos:
    return PipelineRepos(
        document_sink=NullSink(),
        price_sink=NullSink(),
        fx_sink=NullSink(),
        fundamental_sink=NullSink(),
        blob_sink=NullSink(),
        watermarks=NullSink(),
    )


def make_context(user_id: str = "user-alice") -> PipelineContext:
    return PipelineContext(
        universe=object(),
        config={"policy": {}, "fees": {}},
        as_of_date=date(2026, 8, 20),
        user_id=user_id,
        repos=make_repos(),
        providers=PipelineProviders(),
    )


class TestStepPartition:
    def test_every_step_is_either_shared_or_per_user(self):
        assert set(SHARED_STEPS) | set(PER_USER_STEPS) == set(PIPELINE_STEPS)
        assert not set(SHARED_STEPS) & set(PER_USER_STEPS)

    def test_only_portfolio_dependent_steps_are_per_user(self):
        assert set(PER_USER_STEPS) == {"PROJECT_PORTFOLIO", "RUN_POLICY", "ASSERT"}

    def test_projection_is_named_before_the_cascade_that_consumes_it(self):
        """The projection is an input to every gate, not an output of them.

        The policy step computes and caches the projection through
        ``_get_portfolio_projection``; naming ``PROJECT_PORTFOLIO`` afterwards
        made the manifest read as though the book were valued after the trades
        had been decided. The effects were always in this order — now the step
        names are too, and ``PROJECT_PORTFOLIO`` populates the cache the policy
        cascade then reuses.
        """

        assert PER_USER_STEPS == ("PROJECT_PORTFOLIO", "RUN_POLICY", "ASSERT")
        assert PIPELINE_STEPS.index("PROJECT_PORTFOLIO") < PIPELINE_STEPS.index("RUN_POLICY")
        assert PIPELINE_STEPS.index("RUN_POLICY") < PIPELINE_STEPS.index("ASSERT")

    def test_phases_preserve_the_documented_step_order(self):
        assert list(SHARED_PRE_STEPS) + list(PER_USER_STEPS) + list(SHARED_POST_STEPS) == PIPELINE_STEPS

    def test_validation_and_closure_happen_after_the_fan_out(self):
        """`VALIDATE` reconciles recommendation counts and `END_RUN`
        summarises the action mix, so both are meaningless — and would report
        a phantom degradation — if they ran before any policy had executed."""

        assert "VALIDATE" in SHARED_POST_STEPS
        assert "END_RUN" in SHARED_POST_STEPS
        assert "NARRATE" in SHARED_POST_STEPS
        assert "WRITE_SNAPSHOT" in SHARED_PRE_STEPS

    def test_expensive_research_steps_are_shared(self):
        for step in (
            "COLLECT_PRICES",
            "COLLECT_FILINGS",
            "EXTRACT_CHANNEL_A",
            "EXTRACT_CHANNEL_B",
            "COMPUTE_RAW_LEGS",
            "NORMALISE",
            "WRITE_SNAPSHOT",
            "NARRATE",
        ):
            assert step in SHARED_STEPS


class TestDerivedContext:
    def test_shared_research_is_reused_by_reference(self):
        shared = make_context()
        scores = {"sec-nvda": object()}
        shared.__dict__["_score_results"] = scores
        shared.__dict__["_snapshots"] = ["snapshot"]
        shared.__dict__["_config_version_id"] = "cfg-1"

        derived = shared.derive_for_user("user-bob")

        assert derived.user_id == "user-bob"
        assert derived.__dict__["_score_results"] is scores
        assert derived.__dict__["_snapshots"] == ["snapshot"]
        assert derived.__dict__["_config_version_id"] == "cfg-1"

    def test_per_user_scratch_never_leaks_between_users(self):
        shared = make_context()
        shared.__dict__["_portfolio_projection"] = "alice-projection"
        shared.__dict__["_portfolio_snapshot"] = "alice-snapshot"
        shared.__dict__["_actions"] = ["BUY"]
        shared.__dict__["_eligible_but_no_cash_count"] = 3
        shared.__dict__["_assertion_violations"] = ["boom"]

        derived = shared.derive_for_user("user-bob")

        for key in PipelineContext.PER_USER_SCRATCH_KEYS:
            assert key not in derived.__dict__

    def test_derived_context_gets_its_own_ledger_binding(self):
        shared = make_context()
        shared.providers.portfolio_reader = "alice-reader"

        derived = shared.derive_for_user("user-bob", portfolio_reader="bob-reader")

        assert derived.providers.portfolio_reader == "bob-reader"
        assert shared.providers.portfolio_reader == "alice-reader"

    def test_omitting_a_reader_keeps_the_shared_binding_rather_than_blanking_it(self):
        """Blanking it would degrade the projection to an empty book silently.

        The policy step would then emit BUY/TRIM suggestions against a
        phantom portfolio with no exception and no degraded-step marker —
        wrong output that looks entirely healthy.
        """

        shared = make_context()
        shared.providers.portfolio_reader = "shared-reader"

        derived = shared.derive_for_user("user-bob")

        assert derived.providers.portfolio_reader == "shared-reader"


class TestFanOut:
    @pytest.mark.asyncio
    async def test_shared_steps_run_once_and_per_user_steps_run_per_user(self, monkeypatch):
        calls: list[tuple[str, str]] = []

        def record(step_name: str):
            async def _step(ctx: PipelineContext, manifest: RunManifest) -> None:
                calls.append((step_name, ctx.user_id))

            return _step

        for step_name in PIPELINE_STEPS:
            monkeypatch.setitem(
                __import__("auspex.pipeline.runner", fromlist=["STEP_FUNCTIONS"]).STEP_FUNCTIONS,
                step_name,
                record(step_name),
            )

        result = await run_multi_user_pipeline(
            make_context("user-alice"), ["user-alice", "user-bob", "user-carol"], concurrency=2
        )

        shared_calls = [call for call in calls if call[0] in SHARED_STEPS]
        assert len(shared_calls) == len(SHARED_STEPS)
        assert {call[1] for call in shared_calls} == {"user-alice"}

        policy_calls = [call for call in calls if call[0] == "RUN_POLICY"]
        assert {call[1] for call in policy_calls} == {"user-alice", "user-bob", "user-carol"}
        assert result.succeeded_user_ids == ["user-alice", "user-bob", "user-carol"]

    @pytest.mark.asyncio
    async def test_each_user_stage_holds_its_durable_operation_fence(
        self,
        monkeypatch,
    ):
        module = __import__("auspex.pipeline.runner", fromlist=["STEP_FUNCTIONS"])
        held: list[str] = []
        released: list[str] = []

        async def noop(ctx, manifest):
            if manifest.run_type.startswith("nightly-user-"):
                assert ctx.user_id in held

        for step_name in PIPELINE_STEPS:
            monkeypatch.setitem(module.STEP_FUNCTIONS, step_name, noop)

        @asynccontextmanager
        async def operation(user_id):
            if user_id == "user-bob":
                raise RuntimeError("deletion is in progress")
            held.append(user_id)
            try:
                yield
            finally:
                held.remove(user_id)
                released.append(user_id)

        result = await run_multi_user_pipeline(
            make_context("user-alice"),
            ["user-alice", "user-bob"],
            user_operation_factory=operation,
        )

        assert result.succeeded_user_ids == ["user-alice"]
        assert result.failed_user_ids == ["user-bob"]
        assert released == ["user-alice"]

    @pytest.mark.asyncio
    async def test_closing_steps_observe_the_fan_outs_results(self, monkeypatch):
        """`VALIDATE`/`END_RUN` must see a user's policy outcome, not an empty one."""

        module = __import__("auspex.pipeline.runner", fromlist=["STEP_FUNCTIONS"])
        observed: dict[str, object] = {}

        async def noop(ctx, manifest):
            return None

        async def policy(ctx, manifest):
            ctx.__dict__["_actions"] = [f"action-for-{ctx.user_id}"]

        async def validate(ctx, manifest):
            observed["user_id"] = ctx.user_id
            observed["actions"] = ctx.__dict__.get("_actions")

        for step_name in PIPELINE_STEPS:
            monkeypatch.setitem(module.STEP_FUNCTIONS, step_name, noop)
        monkeypatch.setitem(module.STEP_FUNCTIONS, "RUN_POLICY", policy)
        monkeypatch.setitem(module.STEP_FUNCTIONS, "VALIDATE", validate)

        await run_multi_user_pipeline(make_context("user-alice"), ["user-alice", "user-bob"])

        assert observed["user_id"] == "user-alice"
        assert observed["actions"] == ["action-for-user-alice"]

    @pytest.mark.asyncio
    async def test_closing_steps_fall_back_to_a_surviving_user(self, monkeypatch):
        module = __import__("auspex.pipeline.runner", fromlist=["STEP_FUNCTIONS"])
        observed: dict[str, object] = {}

        async def noop(ctx, manifest):
            return None

        async def policy(ctx, manifest):
            if ctx.user_id == "user-alice":
                raise RuntimeError("alice's ledger is unreadable")
            ctx.__dict__["_actions"] = [f"action-for-{ctx.user_id}"]

        async def validate(ctx, manifest):
            observed["user_id"] = ctx.user_id

        for step_name in PIPELINE_STEPS:
            monkeypatch.setitem(module.STEP_FUNCTIONS, step_name, noop)
        monkeypatch.setitem(module.STEP_FUNCTIONS, "RUN_POLICY", policy)
        monkeypatch.setitem(module.STEP_FUNCTIONS, "VALIDATE", validate)

        await run_multi_user_pipeline(make_context("user-alice"), ["user-alice", "user-bob"])

        assert observed["user_id"] == "user-bob"

    @pytest.mark.asyncio
    async def test_one_user_failing_does_not_deny_the_others(self, monkeypatch):
        served: list[str] = []

        async def shared_noop(ctx, manifest):
            return None

        async def policy(ctx, manifest):
            if ctx.user_id == "user-bob":
                raise RuntimeError("bob's ledger is unreadable")
            served.append(ctx.user_id)

        module = __import__("auspex.pipeline.runner", fromlist=["STEP_FUNCTIONS"])
        for step_name in PIPELINE_STEPS:
            monkeypatch.setitem(module.STEP_FUNCTIONS, step_name, shared_noop)
        monkeypatch.setitem(module.STEP_FUNCTIONS, "RUN_POLICY", policy)

        result = await run_multi_user_pipeline(
            make_context("user-alice"), ["user-alice", "user-bob", "user-carol"]
        )

        assert result.failed_user_ids == ["user-bob"]
        assert served == ["user-alice", "user-carol"] or sorted(served) == [
            "user-alice",
            "user-carol",
        ]
        # The run degrades rather than failing: everyone else's work is valid.
        policy_step = result.manifest.step_by_name("RUN_POLICY")
        assert policy_step.degraded is True
        assert "failed=1" in policy_step.detail
        assert "user-bob" not in policy_step.detail
        assert "ledger is unreadable" not in policy_step.detail

    @pytest.mark.asyncio
    async def test_shared_failure_aborts_before_any_user_work(self, monkeypatch):
        module = __import__("auspex.pipeline.runner", fromlist=["STEP_FUNCTIONS"])
        touched: list[str] = []

        async def ok(ctx, manifest):
            return None

        async def boom(ctx, manifest):
            raise RuntimeError("provider outage")

        async def policy(ctx, manifest):
            touched.append(ctx.user_id)

        for step_name in PIPELINE_STEPS:
            monkeypatch.setitem(module.STEP_FUNCTIONS, step_name, ok)
        monkeypatch.setitem(module.STEP_FUNCTIONS, "COMPUTE_RAW_LEGS", boom)
        monkeypatch.setitem(module.STEP_FUNCTIONS, "RUN_POLICY", policy)

        result = await run_multi_user_pipeline(make_context(), ["user-alice", "user-bob"])

        assert result.manifest.status.value == "FAILED"
        assert touched == []
        assert result.user_results == []

    @pytest.mark.asyncio
    async def test_user_stage_failure_is_reported_not_raised(self, monkeypatch):
        module = __import__("auspex.pipeline.runner", fromlist=["STEP_FUNCTIONS"])

        async def boom(ctx, manifest):
            raise RuntimeError("no settings")

        monkeypatch.setitem(module.STEP_FUNCTIONS, "RUN_POLICY", boom)

        outcome = await run_user_stage(
            make_context(), new_manifest(date(2026, 8, 20)), "user-bob"
        )

        assert outcome.succeeded is False
        assert "no settings" in outcome.detail


class TestSuppressionInPolicyStep:
    @pytest.mark.asyncio
    async def test_absent_disposition_repository_suppresses_nothing(self):
        ctx = make_context()

        assert await step_fns._active_dispositions(ctx) == {}

    @pytest.mark.asyncio
    async def test_dispositions_are_read_only_from_the_users_own_partition(self):
        from auspex.models.common import utc_now
        from auspex.models.enums import DispositionStatus
        from auspex.models.policy import RecommendationDisposition

        recorded: list[str | None] = []

        class Repo:
            def __init__(self, rows):
                self.rows = rows

            async def query(self, query, parameters=None, partition_key=None):
                recorded.append(partition_key)
                wanted = {p["name"].lstrip("@"): p["value"] for p in (parameters or [])}["user_id"]
                return [row for row in self.rows if row.user_id == wanted]

        rows = [
            RecommendationDisposition(
                id=f"{user}:sec-nvda",
                user_id=user,
                security_id="sec-nvda",
                disposition=DispositionStatus.REJECTED,
                decision_signature=f"v1:{user}",
                recorded_at=utc_now(),
            )
            for user in ("user-alice", "user-bob")
        ]
        ctx = make_context("user-alice")
        ctx.repos.recommendation_disposition_repo = Repo(rows)

        found = await step_fns._active_dispositions(ctx)

        assert set(found) == {"sec-nvda"}
        assert found["sec-nvda"].user_id == "user-alice"
        assert recorded == ["user-alice"]


def test_concurrency_setting_bounds_the_fan_out():
    from auspex.settings import Settings

    assert Settings().nightly_user_concurrency >= 1
    assert isinstance(Decimal(str(Settings().nightly_user_concurrency)), Decimal)
