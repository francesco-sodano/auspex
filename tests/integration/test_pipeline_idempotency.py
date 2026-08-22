"""Idempotency tests (arc42 §6.1).

"Every write upserts on `security_id + as_of_date`. Re-running a date
replaces that date's rows and produces identical output." This module
verifies that running the nightly pipeline twice for the same date is a
no-op in terms of stored state — same values, same row counts, not
duplicated rows.
"""

from __future__ import annotations

import asyncio
from datetime import date

from auspex.models.enums import RunStatus
from auspex.pipeline.context import PipelineContext
from auspex.pipeline.manifest import resume_step_index
from auspex.pipeline.runner import run_nightly_pipeline
from tests.integration.conftest import (
    build_repos,
    seed_channel_a_extraction,
    seed_fundamentals,
    seed_fx,
    seed_insider_form4,
    seed_prices,
    seed_universe_prices,
)


def _seed_two_securities(repos, universe, as_of_date: date) -> None:
    nvda = universe.by_ticker()["NVDA"]
    amd = universe.by_ticker()["AMD"]
    seed_universe_prices(repos, universe, as_of_date)
    seed_fundamentals(repos, nvda.id, as_of_date)
    seed_channel_a_extraction(repos, nvda.id, as_of_date)
    seed_insider_form4(repos, nvda.id, as_of_date)
    seed_prices(repos, nvda.id, as_of_date, "180")
    seed_fundamentals(repos, amd.id, as_of_date, variant="b")
    seed_channel_a_extraction(repos, amd.id, as_of_date, variant="b")
    seed_prices(repos, amd.id, as_of_date, "220")
    seed_fx(repos, as_of_date)


def test_rerunning_same_date_produces_identical_scores(universe, config_bundle):
    as_of_date = date(2026, 8, 8)
    repos = build_repos()
    _seed_two_securities(repos, universe, as_of_date)
    nvda_id = universe.by_ticker()["NVDA"].id

    ctx1 = PipelineContext(universe=universe, config=config_bundle, as_of_date=as_of_date, user_id="owner", repos=repos)
    manifest1 = asyncio.run(run_nightly_pipeline(ctx1))
    score_run1 = asyncio.run(repos.score_repo.get(f"{nvda_id}:{as_of_date.isoformat()}"))

    # re-run for the exact same date against the same repositories (fresh
    # scratch state, since PipelineContext scratch is per-instance)
    ctx2 = PipelineContext(universe=universe, config=config_bundle, as_of_date=as_of_date, user_id="owner", repos=repos)
    manifest2 = asyncio.run(run_nightly_pipeline(ctx2))
    score_run2 = asyncio.run(repos.score_repo.get(f"{nvda_id}:{as_of_date.isoformat()}"))

    assert manifest1.status == manifest2.status
    assert score_run1.composite == score_run2.composite
    assert score_run1.percentile == score_run2.percentile
    assert score_run1.coverage == score_run2.coverage
    assert score_run1.package_fingerprint == score_run2.package_fingerprint
    for leg_name in score_run1.legs:
        assert score_run1.legs[leg_name].raw == score_run2.legs[leg_name].raw
        assert score_run1.legs[leg_name].z == score_run2.legs[leg_name].z


def test_rerunning_same_date_does_not_duplicate_rows(universe, config_bundle):
    as_of_date = date(2026, 8, 8)
    repos = build_repos()
    _seed_two_securities(repos, universe, as_of_date)

    ctx1 = PipelineContext(universe=universe, config=config_bundle, as_of_date=as_of_date, user_id="owner", repos=repos)
    asyncio.run(run_nightly_pipeline(ctx1))
    scores_after_first_run = len(repos.score_repo.all())
    recommendations_after_first_run = len(repos.recommendation_repo.all())

    ctx2 = PipelineContext(universe=universe, config=config_bundle, as_of_date=as_of_date, user_id="owner", repos=repos)
    asyncio.run(run_nightly_pipeline(ctx2))
    scores_after_second_run = len(repos.score_repo.all())
    recommendations_after_second_run = len(repos.recommendation_repo.all())

    # upsert-on-id semantics: re-running the same date replaces rows, it never appends
    assert scores_after_second_run == scores_after_first_run == 104
    assert recommendations_after_second_run == recommendations_after_first_run == 104


def test_rerunning_different_dates_produces_distinct_rows(universe, config_bundle):
    repos = build_repos()
    day1 = date(2026, 8, 8)
    day2 = date(2026, 8, 9)
    _seed_two_securities(repos, universe, day1)
    _seed_two_securities(repos, universe, day2)

    ctx1 = PipelineContext(universe=universe, config=config_bundle, as_of_date=day1, user_id="owner", repos=repos)
    asyncio.run(run_nightly_pipeline(ctx1))
    ctx2 = PipelineContext(universe=universe, config=config_bundle, as_of_date=day2, user_id="owner", repos=repos)
    asyncio.run(run_nightly_pipeline(ctx2))

    # 104 securities x 2 distinct dates = 208 distinct score rows, not merged
    assert len(repos.score_repo.all()) == 104 * 2

    nvda_id = universe.by_ticker()["NVDA"].id
    score_day1 = asyncio.run(repos.score_repo.get(f"{nvda_id}:{day1.isoformat()}"))
    score_day2 = asyncio.run(repos.score_repo.get(f"{nvda_id}:{day2.isoformat()}"))
    assert score_day1 is not None and score_day2 is not None
    assert score_day1.as_of_date != score_day2.as_of_date


def test_resume_from_last_successful_step_skips_completed_work(universe, config_bundle):
    """A crashed/interrupted run resumes from the last successful step (arc42 §6.1)."""

    as_of_date = date(2026, 8, 8)
    repos = build_repos()
    _seed_two_securities(repos, universe, as_of_date)

    ctx = PipelineContext(universe=universe, config=config_bundle, as_of_date=as_of_date, user_id="owner", repos=repos)
    manifest = asyncio.run(run_nightly_pipeline(ctx))
    assert manifest.status in (RunStatus.SUCCESS, RunStatus.DEGRADED)

    # a fully completed manifest has nothing left to resume
    assert resume_step_index(manifest) == len(manifest.steps)

    # simulate a manifest that crashed partway through by resetting later steps to PENDING
    for cp in manifest.steps[10:]:
        cp.status = "PENDING"
    resume_index = resume_step_index(manifest)
    assert resume_index == 10

    ctx2 = PipelineContext(universe=universe, config=config_bundle, as_of_date=as_of_date, user_id="owner", repos=repos)
    resumed_manifest = asyncio.run(run_nightly_pipeline(ctx2, existing_manifest=manifest))
    assert all(cp.status in ("SUCCESS", "SKIPPED") for cp in resumed_manifest.steps)
    # the first 10 steps' checkpoints were left untouched by the resumed run
    assert resumed_manifest.steps[0].detail == manifest.steps[0].detail
