"""Pure E21 narrative feature extraction and deterministic aggregation."""

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Mapping

from .sentiment import evidence_candidates


PROMPT_VERSION = "e21_narrative_v1"
PROMPT_SHA256 = "70987525ba240b9008ec684c5cab346cfd02b10f8315d7c2f66adff381c930a5"
MAX_THEMES = 5

COMPONENT_WEIGHTS = {
    "sentiment_strength": 0.10,
    "sentiment_velocity_strength": 0.10,
    "theme_concentration": 0.15,
    "forward_promise_ratio": 0.25,
    "hype_density": 0.20,
    "news_attention": 0.15,
    "insider_divergence": 0.05,
}


@dataclass(frozen=True)
class NarrativeDocumentFeatures:
    sentiment: float
    relevance: float
    forward_promise_ratio: float
    hype_density: float
    themes: tuple[str, ...]
    evidence_quotes: Mapping[str, str]
    theme_evidence: Mapping[str, str]


@dataclass(frozen=True)
class NarrativeInputs:
    eligible_document_count: int
    extracted_document_count: int
    sentiment_level: float | None
    sentiment_velocity_z: float | None
    theme_concentration: float | None
    forward_promise_ratio: float | None
    hype_density: float | None
    news_volume_z_30d: float | None
    insider_net_buy_ratio_90d: float | None
    mgmt_reality_gap: float | None
    revision_dispersion_z: float | None
    options_skew: float | None


@dataclass(frozen=True)
class NarrativeIntensityResult:
    narrative_intensity: float | None
    coverage_status: str
    available_weight: float
    extraction_coverage: float
    components: Mapping[str, float]
    coverage_reasons: tuple[str, ...]


def narrative_cache_key(
    document_revision_hash: str,
    model_version: str,
    prompt_version: str = PROMPT_VERSION,
) -> str:
    if not document_revision_hash or not model_version or not prompt_version:
        raise ValueError("document revision, model version, and prompt version are required")
    identity = f"{document_revision_hash}|{model_version}|{prompt_version}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def normalize_theme(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    if not normalized:
        raise ValueError("theme label must contain letters or numbers")
    return normalized[:64]


def narrative_messages(
    title: str,
    content: str,
    prompt_text: str,
    *,
    max_excerpt_chars: int = 200,
) -> list[dict[str, str]]:
    if not prompt_text.strip():
        raise ValueError("narrative prompt is required")
    excerpts = evidence_candidates(content, max_chars=max_excerpt_chars)
    return [
        {"role": "system", "content": prompt_text.strip()},
        {
            "role": "user",
            "content": (
                f"Title: {title.strip()}\n\nEvidence excerpts:\n"
                + "\n".join(f"[{index}] {excerpt}" for index, excerpt in enumerate(excerpts))
            ),
        },
    ]


def _bounded_number(payload: dict, field: str, minimum: float, maximum: float) -> float:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise ValueError(f"{field} must be between {minimum:g} and {maximum:g}")
    return number


def _evidence_index(value, excerpts: list[str], field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} evidence index must be an integer")
    if value < 0 or value >= len(excerpts):
        raise ValueError(f"{field} evidence index is outside the supplied excerpts")
    return value


def parse_narrative_response(
    raw_response: str,
    content: str,
    *,
    max_excerpt_chars: int = 200,
) -> NarrativeDocumentFeatures:
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise ValueError("narrative response must be valid JSON") from exc
    expected_fields = {
        "sentiment",
        "relevance",
        "forward_promise_ratio",
        "hype_density",
        "themes",
        "evidence_indices",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise ValueError("narrative response has an invalid field contract")

    sentiment = _bounded_number(payload, "sentiment", -1.0, 1.0)
    relevance = _bounded_number(payload, "relevance", 0.0, 1.0)
    forward_promise_ratio = _bounded_number(payload, "forward_promise_ratio", 0.0, 1.0)
    hype_density = _bounded_number(payload, "hype_density", 0.0, 1.0)
    excerpts = evidence_candidates(content, max_chars=max_excerpt_chars)

    evidence_indices = payload["evidence_indices"]
    expected_evidence_fields = {"sentiment", "forward_promise_ratio", "hype_density"}
    if not isinstance(evidence_indices, dict) or set(evidence_indices) != expected_evidence_fields:
        raise ValueError("evidence_indices has an invalid field contract")
    evidence_quotes = {
        field: excerpts[_evidence_index(evidence_indices[field], excerpts, field)]
        for field in sorted(expected_evidence_fields)
    }

    themes_payload = payload["themes"]
    if not isinstance(themes_payload, list):
        raise ValueError("themes must be an array")
    if len(themes_payload) > MAX_THEMES:
        raise ValueError("themes must contain at most five entries")
    themes = []
    theme_evidence = {}
    for item in themes_payload:
        if not isinstance(item, dict) or set(item) != {"label", "evidence_index"}:
            raise ValueError("theme entries have an invalid field contract")
        theme = normalize_theme(item["label"])
        index = _evidence_index(item["evidence_index"], excerpts, f"theme {theme}")
        if theme not in theme_evidence:
            themes.append(theme)
            theme_evidence[theme] = excerpts[index]

    return NarrativeDocumentFeatures(
        sentiment=sentiment,
        relevance=relevance,
        forward_promise_ratio=forward_promise_ratio,
        hype_density=hype_density,
        themes=tuple(themes),
        evidence_quotes=evidence_quotes,
        theme_evidence=theme_evidence,
    )


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return min(maximum, max(minimum, float(value)))


def _z_attention(value: float) -> float:
    return 0.5 * (math.tanh(float(value) / 2.0) + 1.0)


def compute_narrative_intensity(inputs: NarrativeInputs) -> NarrativeIntensityResult:
    if inputs.eligible_document_count < 0 or inputs.extracted_document_count < 0:
        raise ValueError("document counts must be non-negative")
    if inputs.extracted_document_count > inputs.eligible_document_count:
        raise ValueError("extracted document count cannot exceed eligible count")

    components = {}
    missing_supported = []
    component_values = {
        "sentiment_strength": None if inputs.sentiment_level is None else _clamp(abs(inputs.sentiment_level)),
        "sentiment_velocity_strength": None if inputs.sentiment_velocity_z is None else _clamp(abs(inputs.sentiment_velocity_z) / 3.0),
        "theme_concentration": inputs.theme_concentration,
        "forward_promise_ratio": inputs.forward_promise_ratio,
        "hype_density": inputs.hype_density,
        "news_attention": None if inputs.news_volume_z_30d is None else _z_attention(inputs.news_volume_z_30d),
        "insider_divergence": None if inputs.insider_net_buy_ratio_90d is None else _clamp(-inputs.insider_net_buy_ratio_90d),
    }
    for name, value in component_values.items():
        if value is None:
            missing_supported.append(f"{name}:missing")
            continue
        if not math.isfinite(float(value)):
            raise ValueError(f"{name} must be finite")
        components[name] = _clamp(float(value))

    available_weight = sum(COMPONENT_WEIGHTS[name] for name in components)
    extraction_coverage = (
        inputs.extracted_document_count / inputs.eligible_document_count
        if inputs.eligible_document_count
        else 0.0
    )
    reasons = list(missing_supported)
    if inputs.extracted_document_count < inputs.eligible_document_count:
        reasons.append("document_extraction:incomplete")
    if inputs.mgmt_reality_gap is None:
        reasons.append("mgmt_reality_gap:source_unavailable")
    if inputs.revision_dispersion_z is None:
        reasons.append("revision_dispersion_z:source_unavailable")
    if inputs.options_skew is None:
        reasons.append("options_skew:source_unavailable")

    if inputs.extracted_document_count < 3 or available_weight < 0.5:
        status = "WITHHELD"
        intensity = None
        if inputs.extracted_document_count < 3:
            reasons.append("minimum_documents:not_met")
        if available_weight < 0.5:
            reasons.append("minimum_weight:not_met")
    else:
        weighted_sum = sum(
            components[name] * COMPONENT_WEIGHTS[name]
            for name in components
        )
        intensity = round(100.0 * weighted_sum / available_weight, 6)
        status = "READY" if not reasons else "PARTIAL"

    return NarrativeIntensityResult(
        narrative_intensity=intensity,
        coverage_status=status,
        available_weight=round(available_weight, 6),
        extraction_coverage=round(extraction_coverage, 6),
        components=dict(sorted(components.items())),
        coverage_reasons=tuple(sorted(set(reasons))),
    )