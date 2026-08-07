"""Fresh-data six-leg company opportunity engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from statistics import fmean, stdev
from typing import Optional

from .company_package import (
    LEG_WEIGHTS,
    MODEL_VERSION,
    OUTLOOK_HORIZON_DAYS,
    PACKAGE_VERSION,
    WEIGHT_VERSION,
    CompanyLegState,
    CompanyOpportunityPackage,
    CompanySourceCursor,
    EvidenceRef,
    classify_outlook,
    validate_company_package,
)


MIN_COHORT_SIZE = 3
MIN_AVAILABLE_LEG_WEIGHT = 0.50


@dataclass(frozen=True)
class FreshCompanySignal:
    security_sk: int
    ticker: str
    company_name: str
    as_of: date
    theme_id: str
    classification_provenance: str
    classification_id: str
    raw_leg_values: dict[str, Optional[float]]
    leg_evidence: dict[str, tuple[EvidenceRef, ...]]
    leg_coverage_reasons: dict[str, tuple[str, ...]]
    source_cursors: tuple[CompanySourceCursor, ...]


def score_fresh_theme(signals: list[FreshCompanySignal]) -> list[CompanyOpportunityPackage]:
    if not signals:
        return []
    ordered = sorted(signals, key=lambda row: row.security_sk)
    _validate_signals(ordered)
    candidate_count = len(ordered)
    normalized_by_leg = {
        leg_name: _z_scores({
            signal.security_sk: signal.raw_leg_values[leg_name]
            for signal in ordered
        })
        for leg_name in LEG_WEIGHTS
    }
    raw_scores = {}
    available_weights = {}
    for signal in ordered:
        available = [
            leg_name
            for leg_name in LEG_WEIGHTS
            if normalized_by_leg[leg_name][signal.security_sk] is not None
        ]
        available_weight = sum(LEG_WEIGHTS[leg_name] for leg_name in available)
        available_weights[signal.security_sk] = available_weight
        if candidate_count < MIN_COHORT_SIZE or available_weight < MIN_AVAILABLE_LEG_WEIGHT:
            continue
        raw_scores[signal.security_sk] = sum(
            LEG_WEIGHTS[leg_name]
            * float(normalized_by_leg[leg_name][signal.security_sk])
            for leg_name in available
        ) / available_weight
    percentiles = _percentile_scores(raw_scores)

    packages = []
    for signal in ordered:
        security_sk = signal.security_sk
        available_weight = available_weights[security_sk]
        if candidate_count < MIN_COHORT_SIZE:
            coverage_status = "WITHHELD"
            package_reasons = ("theme_cohort_below_minimum",)
        elif security_sk not in raw_scores:
            coverage_status = "WITHHELD"
            package_reasons = ("available_leg_weight_below_minimum",)
        elif available_weight < 1.0 - 1e-12:
            coverage_status = "PARTIAL"
            package_reasons = tuple(sorted({
                reason
                for leg_name in LEG_WEIGHTS
                for reason in signal.leg_coverage_reasons.get(leg_name, ())
            }))
        else:
            coverage_status = "READY"
            package_reasons = ()
        evidence_by_id = {}
        legs = []
        for leg_name, leg_weight in LEG_WEIGHTS.items():
            normalized = (
                normalized_by_leg[leg_name][security_sk]
                if security_sk in raw_scores
                else None
            )
            references = tuple(signal.leg_evidence.get(leg_name, ()))
            for reference in references:
                existing = evidence_by_id.get(reference.evidence_id)
                if existing is not None and existing != reference:
                    raise ValueError("evidence id has conflicting fresh-engine records")
                evidence_by_id[reference.evidence_id] = reference
            contribution = (
                None
                if normalized is None or security_sk not in raw_scores
                else leg_weight * float(normalized) / available_weight
            )
            direction = (
                "UNAVAILABLE"
                if contribution is None
                else "RAISED"
                if contribution > 0
                else "LOWERED"
                if contribution < 0
                else "NEUTRAL"
            )
            legs.append(CompanyLegState(
                leg_name=leg_name,
                normalized_value=normalized,
                contribution=contribution,
                direction=direction,
                available_component_weight=1.0 if normalized is not None else 0.0,
                coverage_reasons=tuple(sorted(set(
                    signal.leg_coverage_reasons.get(leg_name, ())
                ))),
                evidence_ids=tuple(sorted(reference.evidence_id for reference in references)),
                max_knowledge_date=(
                    max(reference.knowledge_date for reference in references)
                    if references else None
                ),
            ))
        max_knowledge_date = max(
            [signal.as_of]
            + [reference.knowledge_date for reference in evidence_by_id.values()]
            + [cursor.latest_knowledge_date for cursor in signal.source_cursors]
        )
        raw_score = raw_scores.get(security_sk)
        package = CompanyOpportunityPackage(
            package_version=PACKAGE_VERSION,
            security_sk=security_sk,
            ticker=signal.ticker.strip().upper(),
            company_name=signal.company_name.strip(),
            as_of=signal.as_of,
            outlook_horizon_days=OUTLOOK_HORIZON_DAYS,
            outlook_direction=classify_outlook(raw_score, coverage_status),
            theme_id=signal.theme_id,
            classification_provenance=signal.classification_provenance,
            classification_id=signal.classification_id,
            candidate_count=candidate_count,
            coverage_status=coverage_status,
            coverage_reasons=package_reasons,
            opportunity_score_raw=raw_score,
            opportunity_score=percentiles.get(security_sk),
            model_version=MODEL_VERSION,
            weight_version=WEIGHT_VERSION,
            max_knowledge_date=max_knowledge_date,
            source_cursors=signal.source_cursors,
            legs=tuple(legs),
            evidence=tuple(sorted(evidence_by_id.values(), key=lambda row: row.evidence_id)),
        )
        validate_company_package(package)
        packages.append(package)
    return packages


def _validate_signals(signals: list[FreshCompanySignal]) -> None:
    theme_id = signals[0].theme_id
    as_of = signals[0].as_of
    security_keys = set()
    for signal in signals:
        if signal.theme_id != theme_id or signal.as_of != as_of:
            raise ValueError("fresh theme scoring requires one theme and as-of date")
        if signal.security_sk in security_keys:
            raise ValueError("fresh theme scoring contains duplicate security")
        security_keys.add(signal.security_sk)
        if set(signal.raw_leg_values) != set(LEG_WEIGHTS):
            raise ValueError("fresh company signal must contain all six raw legs")
        for leg_name, value in signal.raw_leg_values.items():
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"{leg_name} raw value must be finite")
            references = signal.leg_evidence.get(leg_name, ())
            if value is not None and not references:
                raise ValueError(f"{leg_name} requires evidence lineage")
            if any(reference.knowledge_date > signal.as_of for reference in references):
                raise ValueError("fresh company signal contains future knowledge")


def _z_scores(values: dict[int, Optional[float]]) -> dict[int, Optional[float]]:
    observed = [float(value) for value in values.values() if value is not None]
    if not observed:
        return {security_sk: None for security_sk in values}
    if len(observed) < 2:
        return {
            security_sk: None if value is None else 0.0
            for security_sk, value in values.items()
        }
    mean = fmean(observed)
    deviation = stdev(observed)
    if deviation <= 0:
        return {
            security_sk: None if value is None else 0.0
            for security_sk, value in values.items()
        }
    return {
        security_sk: None if value is None else (float(value) - mean) / deviation
        for security_sk, value in values.items()
    }


def _percentile_scores(values: dict[int, float]) -> dict[int, float]:
    if not values:
        return {}
    ordered_values = sorted(values.values())
    if len(set(ordered_values)) == 1:
        return {security_sk: 50.0 for security_sk in values}
    positions = {}
    for index, value in enumerate(ordered_values, start=1):
        positions.setdefault(value, []).append(index)
    average_positions = {
        value: fmean(indexes) for value, indexes in positions.items()
    }
    return {
        security_sk: round(
            100.0 * (average_positions[value] - 0.375) / (len(values) + 0.25),
            4,
        )
        for security_sk, value in values.items()
    }