"""Deterministic current-state company opportunity packages."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
import math
from typing import Optional

from .thesis import (
    LEG_RESULT_FIELDS,
    LEG_WEIGHTS,
    MODEL_VERSION,
    WEIGHT_VERSION,
    OpportunityResult,
)


PACKAGE_VERSION = "company_opportunity_v1"
OUTLOOK_HORIZON_DAYS = 90
OUTLOOK_DIRECTIONS = {"ACCELERATING", "STABLE", "DETERIORATING", "UNCERTAIN"}
COVERAGE_STATUSES = {"READY", "PARTIAL", "WITHHELD"}
LEG_DIRECTIONS = {"RAISED", "LOWERED", "NEUTRAL", "UNAVAILABLE"}


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    source_type: str
    source_id: str
    revision_hash: str
    event_date: date
    knowledge_date: date
    retention_class: str
    url: Optional[str] = None
    excerpt: Optional[str] = None


@dataclass(frozen=True)
class CompanyLegState:
    leg_name: str
    normalized_value: Optional[float]
    contribution: Optional[float]
    direction: str
    available_component_weight: float
    coverage_reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    max_knowledge_date: Optional[date]


@dataclass(frozen=True)
class CompanySourceCursor:
    source_class: str
    source_id: str
    latest_record_id: str
    latest_revision_hash: str
    latest_knowledge_date: date


@dataclass(frozen=True)
class CompanyOpportunityPackage:
    package_version: str
    security_sk: int
    ticker: str
    company_name: str
    as_of: date
    outlook_horizon_days: int
    outlook_direction: str
    theme_id: str
    classification_provenance: str
    classification_id: str
    candidate_count: int
    coverage_status: str
    coverage_reasons: tuple[str, ...]
    opportunity_score_raw: Optional[float]
    opportunity_score: Optional[float]
    model_version: str
    weight_version: str
    max_knowledge_date: date
    source_cursors: tuple[CompanySourceCursor, ...]
    legs: tuple[CompanyLegState, ...]
    evidence: tuple[EvidenceRef, ...]


def classify_outlook(
    opportunity_score_raw: Optional[float],
    coverage_status: str,
) -> str:
    if coverage_status not in COVERAGE_STATUSES:
        raise ValueError("invalid company package coverage status")
    if coverage_status == "WITHHELD" or opportunity_score_raw is None:
        return "UNCERTAIN"
    raw_score = _finite(opportunity_score_raw, "opportunity_score_raw")
    if raw_score > 0:
        return "ACCELERATING"
    if raw_score < 0:
        return "DETERIORATING"
    return "STABLE"


def package_fingerprint(package: CompanyOpportunityPackage) -> str:
    validate_company_package(package)
    payload = asdict(package)
    payload["coverage_reasons"] = sorted(payload["coverage_reasons"])
    payload["legs"] = sorted(
        (
            {
                **leg,
                "coverage_reasons": sorted(leg["coverage_reasons"]),
                "evidence_ids": sorted(leg["evidence_ids"]),
            }
            for leg in payload["legs"]
        ),
        key=lambda leg: leg["leg_name"],
    )
    payload["evidence"] = sorted(
        payload["evidence"],
        key=lambda evidence: evidence["evidence_id"],
    )
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def package_changed(
    previous: CompanyOpportunityPackage | None,
    current: CompanyOpportunityPackage,
) -> bool:
    return previous is None or package_fingerprint(previous) != package_fingerprint(current)


def package_document(package: CompanyOpportunityPackage) -> dict:
    fingerprint = package_fingerprint(package)
    payload = json.loads(json.dumps(asdict(package), default=str))
    return {
        "id": f"package:{fingerprint}",
        "security_sk": package.security_sk,
        "document_type": "revision",
        "package_fingerprint": fingerprint,
        **payload,
    }


def build_company_package(
    result: OpportunityResult,
    *,
    ticker: str,
    company_name: str,
    leg_available_component_weights: dict[str, float],
    leg_evidence: dict[str, tuple[EvidenceRef, ...]],
    leg_coverage_reasons: dict[str, tuple[str, ...]] | None = None,
    source_cursors: tuple[CompanySourceCursor, ...] = (),
) -> CompanyOpportunityPackage:
    if set(leg_available_component_weights) != set(LEG_WEIGHTS):
        raise ValueError("component availability must cover all six legs")
    unknown_evidence_legs = set(leg_evidence) - set(LEG_WEIGHTS)
    if unknown_evidence_legs:
        raise ValueError("evidence mapping contains an unknown leg")
    leg_coverage_reasons = leg_coverage_reasons or {}
    if set(leg_coverage_reasons) - set(LEG_WEIGHTS):
        raise ValueError("coverage mapping contains an unknown leg")

    evidence_by_id: dict[str, EvidenceRef] = {}
    legs = []
    for leg_name in LEG_WEIGHTS:
        normalized_value = getattr(result, LEG_RESULT_FIELDS[leg_name])
        contribution = getattr(result, f"{leg_name}_contribution")
        references = tuple(leg_evidence.get(leg_name, ()))
        for reference in references:
            existing = evidence_by_id.get(reference.evidence_id)
            if existing is not None and existing != reference:
                raise ValueError("evidence id has conflicting company package records")
            evidence_by_id[reference.evidence_id] = reference
        direction = (
            "UNAVAILABLE"
            if normalized_value is None or contribution is None
            else "RAISED"
            if contribution > 0
            else "LOWERED"
            if contribution < 0
            else "NEUTRAL"
        )
        legs.append(CompanyLegState(
            leg_name=leg_name,
            normalized_value=normalized_value,
            contribution=contribution,
            direction=direction,
            available_component_weight=leg_available_component_weights[leg_name],
            coverage_reasons=tuple(sorted(set(leg_coverage_reasons.get(leg_name, ())))),
            evidence_ids=tuple(sorted(reference.evidence_id for reference in references)),
            max_knowledge_date=(
                max(reference.knowledge_date for reference in references)
                if references
                else None
            ),
        ))
    package = CompanyOpportunityPackage(
        package_version=PACKAGE_VERSION,
        security_sk=result.security_sk,
        ticker=ticker.strip().upper(),
        company_name=company_name.strip(),
        as_of=result.as_of,
        outlook_horizon_days=OUTLOOK_HORIZON_DAYS,
        outlook_direction=classify_outlook(
            result.opportunity_score_raw,
            result.coverage_status,
        ),
        theme_id=result.theme_id,
        classification_provenance=result.classification_provenance,
        classification_id=result.classification_id,
        candidate_count=result.candidate_count,
        coverage_status=result.coverage_status,
        coverage_reasons=tuple(sorted(set(result.coverage_reasons))),
        opportunity_score_raw=result.opportunity_score_raw,
        opportunity_score=result.opportunity_score,
        model_version=result.model_version,
        weight_version=result.weight_version,
        max_knowledge_date=result.max_knowledge_date,
        source_cursors=source_cursors,
        legs=tuple(legs),
        evidence=tuple(sorted(evidence_by_id.values(), key=lambda row: row.evidence_id)),
    )
    validate_company_package(package)
    return package


def validate_company_package(package: CompanyOpportunityPackage) -> None:
    if package.package_version != PACKAGE_VERSION:
        raise ValueError("unsupported company package version")
    if package.security_sk <= 0 or not package.ticker.strip() or not package.company_name.strip():
        raise ValueError("company package identity is incomplete")
    if package.outlook_horizon_days != OUTLOOK_HORIZON_DAYS:
        raise ValueError("company package outlook horizon must be 90 days")
    if package.outlook_direction not in OUTLOOK_DIRECTIONS:
        raise ValueError("invalid company package outlook direction")
    if package.coverage_status not in COVERAGE_STATUSES:
        raise ValueError("invalid company package coverage status")
    if package.model_version != MODEL_VERSION or package.weight_version != WEIGHT_VERSION:
        raise ValueError("company package score version is invalid")
    if package.candidate_count < 1:
        raise ValueError("company package candidate count must be positive")
    if package.max_knowledge_date > package.as_of:
        raise ValueError("company package contains future knowledge")
    cursor_keys = [
        (cursor.source_class, cursor.source_id)
        for cursor in package.source_cursors
    ]
    if len(cursor_keys) != len(set(cursor_keys)):
        raise ValueError("company package contains duplicate source cursors")
    for cursor in package.source_cursors:
        if not all((
            cursor.source_class.strip(),
            cursor.source_id.strip(),
            cursor.latest_record_id.strip(),
            cursor.latest_revision_hash.strip(),
        )):
            raise ValueError("company package source cursor identity is incomplete")
        if cursor.latest_knowledge_date > package.as_of:
            raise ValueError("company package source cursor contains future knowledge")
    expected_direction = classify_outlook(
        package.opportunity_score_raw,
        package.coverage_status,
    )
    if package.outlook_direction != expected_direction:
        raise ValueError("company package outlook does not match its raw score and coverage")
    if package.opportunity_score is not None:
        score = _finite(package.opportunity_score, "opportunity_score")
        if score < 0 or score > 100:
            raise ValueError("company package score must be between 0 and 100")

    leg_names = [leg.leg_name for leg in package.legs]
    if len(leg_names) != len(set(leg_names)) or set(leg_names) != set(LEG_WEIGHTS):
        raise ValueError("company package must contain each of the six legs exactly once")
    evidence_by_id = {evidence.evidence_id: evidence for evidence in package.evidence}
    if len(evidence_by_id) != len(package.evidence):
        raise ValueError("company package contains duplicate evidence ids")
    for evidence in package.evidence:
        _validate_evidence(evidence, package.as_of)
    for leg in package.legs:
        _validate_leg(leg, evidence_by_id, package.as_of)


def _validate_evidence(evidence: EvidenceRef, as_of: date) -> None:
    if not all((
        evidence.evidence_id.strip(),
        evidence.source_type.strip(),
        evidence.source_id.strip(),
        evidence.revision_hash.strip(),
        evidence.retention_class.strip(),
    )):
        raise ValueError("company package evidence identity is incomplete")
    if evidence.event_date > evidence.knowledge_date:
        raise ValueError("evidence event date exceeds knowledge date")
    if evidence.knowledge_date > as_of:
        raise ValueError("company package evidence contains future knowledge")


def _validate_leg(
    leg: CompanyLegState,
    evidence_by_id: dict[str, EvidenceRef],
    as_of: date,
) -> None:
    if leg.leg_name not in LEG_WEIGHTS or leg.direction not in LEG_DIRECTIONS:
        raise ValueError("invalid company package leg")
    available_weight = _finite(
        leg.available_component_weight,
        f"{leg.leg_name}.available_component_weight",
    )
    if available_weight < 0 or available_weight > 1:
        raise ValueError("leg available component weight must be between 0 and 1")
    if len(leg.evidence_ids) != len(set(leg.evidence_ids)):
        raise ValueError("company package leg contains duplicate evidence ids")
    if set(leg.evidence_ids) - set(evidence_by_id):
        raise ValueError("company package leg references unknown evidence")
    if leg.max_knowledge_date is not None and leg.max_knowledge_date > as_of:
        raise ValueError("company package leg contains future knowledge")
    if leg.direction == "UNAVAILABLE":
        if leg.normalized_value is not None or leg.contribution is not None:
            raise ValueError("unavailable leg cannot contain a value or contribution")
        return
    if leg.normalized_value is None or leg.contribution is None:
        raise ValueError("available leg requires a value and contribution")
    _finite(leg.normalized_value, f"{leg.leg_name}.normalized_value")
    contribution = _finite(leg.contribution, f"{leg.leg_name}.contribution")
    expected_direction = (
        "RAISED" if contribution > 0 else "LOWERED" if contribution < 0 else "NEUTRAL"
    )
    if leg.direction != expected_direction:
        raise ValueError("leg direction does not match its signed contribution")
    if leg.direction != "NEUTRAL" and not leg.evidence_ids:
        raise ValueError("directional leg requires evidence lineage")


def _finite(value: float, field_name: str) -> float:
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{field_name} must be finite")
    return numeric