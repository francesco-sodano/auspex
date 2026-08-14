"""Integration test: full 20-step pipeline over seeded fixture evidence (arc42 §6.1).

Feeds realistic documents/extractions/fundamentals/prices directly into the
in-memory sinks (bypassing network collectors, which are skipped when no
provider is configured) and asserts the deterministic core — scoring,
cohort assignment, policy, ledger — produces a coherent, evidence-backed
result end to end.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from auspex.models.enums import Action, CohortConfidence, RunStatus
from auspex.pipeline.runner import run_nightly_pipeline
from tests.integration.conftest import (
    build_repos,
    seed_channel_a_extraction,
    seed_fundamentals,
    seed_fx,
    seed_insider_form4,
    seed_prices,
)


def test_pipeline_produces_coherent_scores_from_seeded_evidence(universe, config_bundle):
    as_of_date = date(2026, 8, 8)
    repos = build_repos()

    nvda = universe.by_ticker()["NVDA"]
    amd = universe.by_ticker()["AMD"]  # same cohort (semi-compute) — needed for a non-degenerate cross-section

    seed_fundamentals(repos, nvda.id, as_of_date)
    seed_channel_a_extraction(repos, nvda.id, as_of_date)
    seed_insider_form4(repos, nvda.id, as_of_date)
    seed_prices(repos, nvda.id, as_of_date, "180")

    seed_fundamentals(repos, amd.id, as_of_date, variant="b")
    seed_channel_a_extraction(repos, amd.id, as_of_date, variant="b")
    seed_prices(repos, amd.id, as_of_date, "220")

    seed_fx(repos, as_of_date)

    from auspex.pipeline.context import PipelineContext

    ctx = PipelineContext(universe=universe, config=config_bundle, as_of_date=as_of_date, user_id="owner", repos=repos)

    manifest = asyncio.run(run_nightly_pipeline(ctx))

    assert manifest.status in (RunStatus.SUCCESS, RunStatus.DEGRADED)
    assert manifest.scored_security_count == 104

    nvda_score = asyncio.run(repos.score_repo.get(f"{nvda.id}:{as_of_date.isoformat()}"))
    assert nvda_score is not None
    assert nvda_score.cohort_used == "semi-compute"
    assert nvda_score.cohort_confidence == CohortConfidence.HIGH  # semi-compute has 15 members
    assert Decimal(nvda_score.coverage) > Decimal("0")  # at least some legs computable from seeded evidence

    thesis_leg = nvda_score.legs["thesis_linkage"]
    assert thesis_leg.computable  # seeded STRONG theme claim feeds this leg
    fundamental_leg = nvda_score.legs["fundamental_health"]
    assert fundamental_leg.computable  # seeded XBRL facts feed this leg
    narrative_leg = nvda_score.legs["narrative_premium"]
    assert narrative_leg.computable  # revenue-growth percentile is computed within the score scope
    smart_money_leg = nvda_score.legs["smart_money"]
    assert smart_money_leg.computable  # seeded Form 4 purchase feeds this leg

    nvda_recommendation = asyncio.run(repos.recommendation_repo.get(f"owner:{nvda.id}:{as_of_date.isoformat()}"))
    assert nvda_recommendation is not None
    assert nvda_recommendation.action in set(Action)
    assert len(nvda_recommendation.gate_trace) > 0  # every gate outcome recorded (arc42 §5.6)


def test_pipeline_checkpoints_every_step(universe, config_bundle):
    as_of_date = date(2026, 8, 8)
    repos = build_repos()
    from auspex.models.run import PIPELINE_STEPS
    from auspex.pipeline.context import PipelineContext

    ctx = PipelineContext(universe=universe, config=config_bundle, as_of_date=as_of_date, user_id="owner", repos=repos)
    manifest = asyncio.run(run_nightly_pipeline(ctx))

    assert [s.step for s in manifest.steps] == PIPELINE_STEPS
    assert all(s.status in ("SUCCESS", "SKIPPED") for s in manifest.steps)
    assert manifest.watermarks_committed is True  # only set at step 20 (END_RUN)


def test_degraded_run_is_still_published_with_reasons(universe, config_bundle):
    """arc42 §5.6: a degraded day is still published, visibly flagged, not rolled back."""

    as_of_date = date(2026, 8, 8)
    repos = build_repos()
    from auspex.pipeline.context import PipelineContext

    ctx = PipelineContext(universe=universe, config=config_bundle, as_of_date=as_of_date, user_id="owner", repos=repos)
    manifest = asyncio.run(run_nightly_pipeline(ctx))

    # with no seeded evidence at all, coverage will be ~0 for every security,
    # which should trip the "HOLD_INSUFFICIENT_DATA fraction < 30%" assertion.
    assert manifest.status == RunStatus.DEGRADED
    assert manifest.degraded_reasons  # reasons recorded, run not silently marked healthy
    assert manifest.scored_security_count == 104  # still published despite being flagged
