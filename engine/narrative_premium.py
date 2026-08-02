"""Pure deterministic E22 narrative-premium attribution."""

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
import math
from statistics import fmean, stdev
from typing import Mapping


MODEL_VERSION = "e22_v4"
SNAPSHOT_FINGERPRINT_VERSION = "e22_portable_sha256_v1"
MIN_ELIGIBLE_SECURITIES = 8
MAP_THRESHOLD = 0.5
CONVERGENCE_DELTA = 0.25


@dataclass(frozen=True)
class PremiumObservation:
    security_sk: int
    as_of: date
    fundamental_anchor_z: float | None
    anchor_method: str | None
    narrative_intensity: float | None
    narrative_coverage_status: str | None
    narrative_available_weight: float | None
    narrative_extraction_coverage: float | None
    narrative_component_mask: tuple[str, ...]
    narrative_coverage_reasons: tuple[str, ...]
    anchor_event_date: date | None
    anchor_knowledge_date: date | None
    anchor_n_peers: int | None
    anchor_r2_sector: float | None
    anchor_imputed_flags: str | None
    narrative_event_date: date
    narrative_knowledge_date: date
    evidence_document_ids: tuple[str, ...]
    e20_model_version: str | None
    e20_generation: str | None
    e20_manifest_fingerprint: str | None
    e21_model_version: str
    prompt_version: str
    input_generation: str
    extraction_generation: str
    e21_manifest_fingerprint: str


@dataclass(frozen=True)
class PreviousPremiumState:
    decision_id: str
    as_of: date
    generation: str
    narrative_premium: float
    fit_context_hash: str


@dataclass(frozen=True)
class PremiumResult:
    decision_id: str
    security_sk: int
    as_of: date
    fundamental_anchor_z: float | None
    narrative_intensity: float | None
    narrative_intensity_z: float | None
    attribution_intercept: float | None
    attribution_beta: float | None
    attribution_r2: float | None
    narrative_premium: float | None
    unexplained_residual: float | None
    anchor_support_z: float | None
    divergence_state: str | None
    is_converging: bool | None
    eligible_security_count: int
    coverage_status: str
    coverage_reasons: tuple[str, ...]
    input_snapshot_hash: str
    fit_context_hash: str
    evidence_pack: Mapping[str, object]
    model_version: str
    event_date: date
    knowledge_date: date


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=lambda value: value.isoformat() if isinstance(value, date) else value,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def premium_input_snapshot_hash(
    observations: list[PremiumObservation],
) -> str:
    if not observations:
        raise ValueError("at least one premium observation is required")
    payload = {
        "model_version": MODEL_VERSION,
        "minimum_eligible_securities": MIN_ELIGIBLE_SECURITIES,
        "map_threshold": MAP_THRESHOLD,
        "convergence_delta": CONVERGENCE_DELTA,
        "snapshot_fingerprint_version": SNAPSHOT_FINGERPRINT_VERSION,
        "observations": [
            {
                **asdict(row),
                "evidence_document_ids": sorted(set(row.evidence_document_ids)),
            }
            for row in sorted(observations, key=lambda item: item.security_sk)
        ],
    }
    return _canonical_hash(payload)


def premium_decision_id(
    security_sk: int,
    as_of: date,
    input_snapshot_hash: str,
    previous_state: PreviousPremiumState | None = None,
) -> str:
    if not input_snapshot_hash:
        raise ValueError("input snapshot hash is required")
    return _canonical_hash({
        "security_sk": int(security_sk),
        "date_sk": int(as_of.strftime("%Y%m%d")),
        "model_version": MODEL_VERSION,
        "input_snapshot_hash": input_snapshot_hash,
        "previous_state": asdict(previous_state) if previous_state is not None else None,
    })


def premium_fit_context_hash(
    eligible_security_ids: list[int],
    component_mask: tuple[str, ...],
) -> str:
    return _canonical_hash({
        "model_version": MODEL_VERSION,
        "eligible_security_ids": sorted(eligible_security_ids),
        "component_mask": sorted(component_mask),
    })


def classify_divergence(
    fundamental_anchor_z: float,
    narrative_premium: float,
) -> str:
    anchor_support_z = -float(fundamental_anchor_z)
    premium = float(narrative_premium)
    if premium >= MAP_THRESHOLD and anchor_support_z <= -MAP_THRESHOLD:
        return "NARRATIVE_LED_OVEREXTENSION"
    if premium >= MAP_THRESHOLD and anchor_support_z >= MAP_THRESHOLD:
        return "NARRATIVE_ON_STRONG_ANCHOR"
    if premium <= -MAP_THRESHOLD and anchor_support_z >= MAP_THRESHOLD:
        return "NARRATIVE_NEGLECTED"
    if abs(float(fundamental_anchor_z)) < MAP_THRESHOLD and abs(premium) < MAP_THRESHOLD:
        return "FUNDAMENTALLY_ANCHORED"
    return "MIXED"


def _individual_reasons(row: PremiumObservation) -> list[str]:
    reasons = []
    anchor = _finite(row.fundamental_anchor_z)
    intensity = _finite(row.narrative_intensity)
    if row.anchor_method not in {"regression", "percentile"} or anchor is None:
        reasons.append("fundamental_anchor:unusable")
    if row.narrative_coverage_status == "WITHHELD":
        reasons.append("narrative_intensity:withheld")
    elif row.narrative_coverage_status not in {"READY", "PARTIAL"}:
        reasons.append("narrative_intensity:invalid_coverage")
    if intensity is None:
        reasons.append("narrative_intensity:missing")
    elif intensity < 0.0 or intensity > 100.0:
        raise ValueError("narrative intensity must be between 0 and 100")
    available_weight = _finite(row.narrative_available_weight)
    if available_weight is None or not 0.0 <= available_weight <= 1.0:
        reasons.append("narrative_intensity:invalid_available_weight")
    extraction_coverage = _finite(row.narrative_extraction_coverage)
    if extraction_coverage is None or not 0.0 <= extraction_coverage <= 1.0:
        reasons.append("narrative_intensity:invalid_extraction_coverage")
    if not row.narrative_component_mask:
        reasons.append("narrative_intensity:missing_component_mask")
    if row.e20_model_version != "e20_v2":
        reasons.append("fundamental_anchor:unsupported_version")
    if not row.e20_generation or not row.e20_manifest_fingerprint:
        reasons.append("fundamental_anchor:incomplete_snapshot")
    if row.e21_model_version != "gpt-4o:2024-11-20":
        reasons.append("narrative_intensity:unsupported_model")
    if row.prompt_version != "e21_narrative_v1":
        reasons.append("narrative_intensity:unsupported_prompt")
    if not row.e21_manifest_fingerprint:
        reasons.append("narrative_intensity:incomplete_snapshot")
    return reasons


def _validate_observations(observations: list[PremiumObservation]) -> date:
    if not observations:
        raise ValueError("at least one premium observation is required")
    as_of_values = {row.as_of for row in observations}
    if len(as_of_values) != 1:
        raise ValueError("premium observations must share one as_of date")
    security_keys = [row.security_sk for row in observations]
    if len(security_keys) != len(set(security_keys)):
        raise ValueError("premium observations must be unique by security")
    as_of = next(iter(as_of_values))
    for row in observations:
        if row.narrative_event_date > row.narrative_knowledge_date:
            raise ValueError("narrative event date cannot exceed knowledge date")
        if row.narrative_knowledge_date > as_of:
            raise ValueError("narrative knowledge date cannot exceed as_of")
        if row.anchor_event_date is not None and row.anchor_knowledge_date is not None:
            if row.anchor_event_date > row.anchor_knowledge_date:
                raise ValueError("anchor event date cannot exceed knowledge date")
            if row.anchor_knowledge_date > as_of:
                raise ValueError("anchor knowledge date cannot exceed as_of")
    return as_of


def build_narrative_premiums(
    observations: list[PremiumObservation],
    *,
    previous_premiums: Mapping[int, PreviousPremiumState] | None = None,
) -> list[PremiumResult]:
    as_of = _validate_observations(observations)
    previous = previous_premiums or {}
    snapshot_hash = premium_input_snapshot_hash(observations)
    reasons_by_security = {
        row.security_sk: _individual_reasons(row)
        for row in observations
    }
    eligible = [
        row for row in observations
        if not reasons_by_security[row.security_sk]
    ]

    cohort_reasons = []
    if len(eligible) < MIN_ELIGIBLE_SECURITIES:
        cohort_reasons.append("attribution:insufficient_eligible_securities")

    component_masks = {tuple(sorted(row.narrative_component_mask)) for row in eligible}
    if len(component_masks) > 1:
        cohort_reasons.append("attribution:heterogeneous_component_mask")
    common_component_mask = next(iter(component_masks)) if len(component_masks) == 1 else ()
    fit_context_hash = premium_fit_context_hash(
        [row.security_sk for row in eligible],
        common_component_mask,
    )

    intensity_values = [float(row.narrative_intensity) for row in eligible]
    intensity_stddev = stdev(intensity_values) if len(intensity_values) >= 2 else 0.0
    if eligible and intensity_stddev <= 1e-12:
        cohort_reasons.append("attribution:zero_intensity_variance")

    anchor_values = [float(row.fundamental_anchor_z) for row in eligible]
    anchor_stddev = stdev(anchor_values) if len(anchor_values) >= 2 else 0.0
    if eligible and anchor_stddev <= 1e-12:
        cohort_reasons.append("attribution:zero_anchor_variance")

    intensity_z: dict[int, float] = {}
    intercept = None
    beta = None
    r2 = None
    if not cohort_reasons:
        intensity_mean = fmean(intensity_values)
        intensity_z = {
            row.security_sk: (float(row.narrative_intensity) - intensity_mean) / intensity_stddev
            for row in eligible
        }
        intercept = fmean(anchor_values)
        denominator = sum(value * value for value in intensity_z.values())
        beta = sum(
            intensity_z[row.security_sk] * (float(row.fundamental_anchor_z) - intercept)
            for row in eligible
        ) / denominator
        predictions = [intercept + beta * intensity_z[row.security_sk] for row in eligible]
        total = sum((value - intercept) ** 2 for value in anchor_values)
        error = sum(
            (observed - predicted) ** 2
            for observed, predicted in zip(anchor_values, predictions)
        )
        r2 = 1.0 - error / total if total > 1e-12 else 0.0
        r2 = max(-1.0, min(1.0, r2))
        if beta <= 0.0:
            cohort_reasons.append("attribution:nonpositive_beta")
            intensity_z = {}
            intercept = None
            beta = None
            r2 = None

    results = []
    for row in sorted(observations, key=lambda item: item.security_sk):
        reasons = sorted(set(reasons_by_security[row.security_sk] + cohort_reasons))
        prior_state = previous.get(row.security_sk)
        decision_id = premium_decision_id(
            row.security_sk,
            as_of,
            snapshot_hash,
            prior_state,
        )
        event_dates = [row.narrative_event_date]
        knowledge_dates = [row.narrative_knowledge_date]
        if row.anchor_event_date is not None:
            event_dates.append(row.anchor_event_date)
        if row.anchor_knowledge_date is not None:
            knowledge_dates.append(row.anchor_knowledge_date)
        event_date = max(event_dates)
        knowledge_date = max(knowledge_dates)

        evidence_document_ids = sorted(set(row.evidence_document_ids))
        evidence_pack = {
            "decision_id": decision_id,
            "security_sk": row.security_sk,
            "as_of_date": as_of.isoformat(),
            "anchor": {
                "fundamental_anchor_z": row.fundamental_anchor_z,
                "anchor_method": row.anchor_method,
                "n_peers": row.anchor_n_peers,
                "r2_sector": row.anchor_r2_sector,
                "imputed_flags": row.anchor_imputed_flags,
                "model_version": row.e20_model_version,
                "generation": row.e20_generation,
                "manifest_fingerprint": row.e20_manifest_fingerprint,
                "event_date": row.anchor_event_date.isoformat() if row.anchor_event_date else None,
                "knowledge_date": row.anchor_knowledge_date.isoformat() if row.anchor_knowledge_date else None,
            },
            "narrative": {
                "narrative_intensity": row.narrative_intensity,
                "coverage_status": row.narrative_coverage_status,
                "available_weight": row.narrative_available_weight,
                "extraction_coverage": row.narrative_extraction_coverage,
                "component_mask": list(sorted(row.narrative_component_mask)),
                "coverage_reasons": list(sorted(row.narrative_coverage_reasons)),
                "model_version": row.e21_model_version,
                "prompt_version": row.prompt_version,
                "input_generation": row.input_generation,
                "extraction_generation": row.extraction_generation,
                "manifest_fingerprint": row.e21_manifest_fingerprint,
                "evidence_document_count": len(evidence_document_ids),
                "evidence_document_hash": _canonical_hash(evidence_document_ids),
                "event_date": row.narrative_event_date.isoformat(),
                "knowledge_date": row.narrative_knowledge_date.isoformat(),
            },
            "attribution": {
                "model_version": MODEL_VERSION,
                "minimum_eligible_securities": MIN_ELIGIBLE_SECURITIES,
                "map_threshold": MAP_THRESHOLD,
                "convergence_delta": CONVERGENCE_DELTA,
                "eligible_security_count": len(eligible),
                "input_snapshot_hash": snapshot_hash,
                "fit_context_hash": fit_context_hash,
                "previous_premium": (
                    {
                        "decision_id": previous[row.security_sk].decision_id,
                        "as_of_date": previous[row.security_sk].as_of.isoformat(),
                        "generation": previous[row.security_sk].generation,
                        "narrative_premium": previous[row.security_sk].narrative_premium,
                        "fit_context_hash": previous[row.security_sk].fit_context_hash,
                    }
                    if row.security_sk in previous
                    else None
                ),
            },
        }

        if reasons:
            evidence_pack = {
                **evidence_pack,
                "output": {
                    "coverage_status": "WITHHELD",
                    "coverage_reasons": reasons,
                    "narrative_intensity_z": None,
                    "attribution_intercept": None,
                    "attribution_beta": None,
                    "attribution_r2": None,
                    "narrative_premium": None,
                    "unexplained_residual": None,
                    "anchor_support_z": None,
                    "divergence_state": None,
                    "is_converging": None,
                },
            }
            result = PremiumResult(
                decision_id=decision_id,
                security_sk=row.security_sk,
                as_of=as_of,
                fundamental_anchor_z=_finite(row.fundamental_anchor_z),
                narrative_intensity=_finite(row.narrative_intensity),
                narrative_intensity_z=None,
                attribution_intercept=None,
                attribution_beta=None,
                attribution_r2=None,
                narrative_premium=None,
                unexplained_residual=None,
                anchor_support_z=None,
                divergence_state=None,
                is_converging=None,
                eligible_security_count=len(eligible),
                coverage_status="WITHHELD",
                coverage_reasons=tuple(reasons),
                input_snapshot_hash=snapshot_hash,
                fit_context_hash=fit_context_hash,
                evidence_pack=evidence_pack,
                model_version=MODEL_VERSION,
                event_date=event_date,
                knowledge_date=knowledge_date,
            )
        else:
            anchor = float(row.fundamental_anchor_z)
            normalized_intensity = intensity_z[row.security_sk]
            premium = beta * normalized_intensity
            unexplained = anchor - intercept - premium
            prior = _finite(
                prior_state.narrative_premium if prior_state is not None else None
            )
            converging = (
                None
                if (
                    prior is None
                    or prior_state.fit_context_hash != fit_context_hash
                    or prior_state.as_of >= as_of
                )
                else abs(premium) <= abs(prior) - CONVERGENCE_DELTA
            )
            coverage_status = (
                "PARTIAL" if row.narrative_coverage_status == "PARTIAL" else "READY"
            )
            coverage_reasons = (
                ("narrative_intensity:partial",)
                if row.narrative_coverage_status == "PARTIAL"
                else ()
            )
            divergence_state = classify_divergence(anchor, premium)
            evidence_pack = {
                **evidence_pack,
                "output": {
                    "coverage_status": coverage_status,
                    "coverage_reasons": list(coverage_reasons),
                    "narrative_intensity_z": normalized_intensity,
                    "attribution_intercept": intercept,
                    "attribution_beta": beta,
                    "attribution_r2": r2,
                    "narrative_premium": premium,
                    "unexplained_residual": unexplained,
                    "anchor_support_z": -anchor,
                    "divergence_state": divergence_state,
                    "is_converging": converging,
                },
            }
            result = PremiumResult(
                decision_id=decision_id,
                security_sk=row.security_sk,
                as_of=as_of,
                fundamental_anchor_z=anchor,
                narrative_intensity=float(row.narrative_intensity),
                narrative_intensity_z=normalized_intensity,
                attribution_intercept=intercept,
                attribution_beta=beta,
                attribution_r2=r2,
                narrative_premium=premium,
                unexplained_residual=unexplained,
                anchor_support_z=-anchor,
                divergence_state=divergence_state,
                is_converging=converging,
                eligible_security_count=len(eligible),
                coverage_status=coverage_status,
                coverage_reasons=coverage_reasons,
                input_snapshot_hash=snapshot_hash,
                fit_context_hash=fit_context_hash,
                evidence_pack=evidence_pack,
                model_version=MODEL_VERSION,
                event_date=event_date,
                knowledge_date=knowledge_date,
            )
        results.append(result)
    return results