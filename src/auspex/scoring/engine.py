"""Scoring orchestrator: wires legs, cohort scope, composite, coverage, staleness.

arc42 §5.5 end to end for one as-of date across a set of securities. Pure
Python over already-collected inputs; persistence and pipeline concerns live
in :mod:`auspex.pipeline`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auspex.config.loader import Universe
from auspex.models.enums import FilerProfile, LegName
from auspex.scoring.composite import CompositeResult, compute_percentile, compute_security_composite
from auspex.scoring.coverage import coverage
from auspex.scoring.normalize import CohortScope, assign_cohort_scope


@dataclass(frozen=True)
class SecurityScoringInput:
    security_id: str
    filer_profile: FilerProfile
    is_stale: bool
    leg_raw: dict[LegName, Decimal | None]


@dataclass(frozen=True)
class SecurityScoreResult:
    security_id: str
    excluded_stale: bool
    cohort_scope: CohortScope | None
    composite_result: CompositeResult | None
    coverage: Decimal
    percentile: int | None


def build_cohort_scopes(
    universe: Universe,
    active_security_ids: set[str],
    cohorts_config: dict,
) -> dict[str, CohortScope]:
    """Assign each cohort's fallback scope (arc42 §5.5), using only non-stale members.

    Returns a mapping of ``cohort_name -> CohortScope`` (not per security) so
    the orchestrator can reuse one scope computation per cohort.
    """

    cohort_to_parent = {name: info["parent"] for name, info in cohorts_config["cohorts"].items()}
    parent_to_cohorts: dict[str, list[str]] = {}
    for cohort, parent in cohort_to_parent.items():
        parent_to_cohorts.setdefault(parent, []).append(cohort)

    by_cohort: dict[str, list[str]] = {}
    for sec in universe.securities:
        if sec.id in active_security_ids:
            by_cohort.setdefault(sec.cohort, []).append(sec.id)

    universe_ids = [s.id for s in universe.securities if s.id in active_security_ids]

    scopes: dict[str, CohortScope] = {}
    for cohort_name, parent_name in cohort_to_parent.items():
        cohort_members = by_cohort.get(cohort_name, [])
        parent_members: list[str] = []
        for c in parent_to_cohorts.get(parent_name, []):
            parent_members.extend(by_cohort.get(c, []))
        scopes[cohort_name] = assign_cohort_scope(
            cohort_name=cohort_name,
            cohort_member_ids=cohort_members,
            parent_name=parent_name,
            parent_member_ids=parent_members,
            universe_member_ids=universe_ids,
        )
    return scopes


def score_universe(
    inputs: list[SecurityScoringInput],
    weights_by_profile: dict[FilerProfile, dict[LegName, Decimal]],
    cohort_scope_by_security: dict[str, CohortScope],
    winsor_sigma: Decimal = Decimal("2.5"),
) -> dict[str, SecurityScoreResult]:
    """Score every non-stale security in ``inputs`` cross-sectionally within its scope."""

    by_id = {i.security_id: i for i in inputs}
    results: dict[str, SecurityScoreResult] = {}

    active = [i for i in inputs if not i.is_stale]
    active_ids = {i.security_id for i in active}

    # Build cohort_raw_by_leg per scope: leg -> {security_id: raw} restricted to
    # the members sharing that exact scope (same cohort or same fallback scope).
    scope_members: dict[str, list[str]] = {}
    for sid in active_ids:
        scope = cohort_scope_by_security.get(sid)
        if scope is None:
            continue
        scope_members.setdefault(scope.scope, []).append(sid)

    for sec_input in inputs:
        if sec_input.is_stale:
            results[sec_input.security_id] = SecurityScoreResult(
                security_id=sec_input.security_id,
                excluded_stale=True,
                cohort_scope=None,
                composite_result=None,
                coverage=Decimal(0),
                percentile=None,
            )
            continue

        scope = cohort_scope_by_security.get(sec_input.security_id)
        members = scope_members.get(scope.scope, []) if scope else [sec_input.security_id]
        weights = weights_by_profile[sec_input.filer_profile]

        cohort_raw_by_leg: dict[LegName, dict[str, Decimal | None]] = {}
        for leg in weights:
            cohort_raw_by_leg[leg] = {m: by_id[m].leg_raw.get(leg) for m in members if m in by_id}

        composite_result = compute_security_composite(
            leg_raw_by_leg=sec_input.leg_raw,
            cohort_raw_by_leg=cohort_raw_by_leg,
            weights=weights,
            security_id=sec_input.security_id,
            winsor_sigma=winsor_sigma,
        )

        computable_legs = {leg for leg, r in composite_result.legs.items() if r.computable}
        cov = coverage(computable_legs, sec_input.filer_profile)

        results[sec_input.security_id] = SecurityScoreResult(
            security_id=sec_input.security_id,
            excluded_stale=False,
            cohort_scope=scope,
            composite_result=composite_result,
            coverage=cov,
            percentile=None,  # filled in below once all composites are known
        )

    # second pass: percentile rank within each scope's composite population
    composites_by_scope: dict[str, dict[str, Decimal | None]] = {}
    for sid, res in results.items():
        if res.excluded_stale or res.cohort_scope is None:
            continue
        composites_by_scope.setdefault(res.cohort_scope.scope, {})[sid] = (
            res.composite_result.composite if res.composite_result else None
        )

    final: dict[str, SecurityScoreResult] = {}
    for sid, res in results.items():
        if res.excluded_stale or res.cohort_scope is None:
            final[sid] = res
            continue
        population = composites_by_scope[res.cohort_scope.scope]
        percentile = compute_percentile(sid, population)
        final[sid] = SecurityScoreResult(
            security_id=res.security_id,
            excluded_stale=res.excluded_stale,
            cohort_scope=res.cohort_scope,
            composite_result=res.composite_result,
            coverage=res.coverage,
            percentile=percentile,
        )
    return final
