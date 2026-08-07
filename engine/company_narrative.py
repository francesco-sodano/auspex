"""Validated AI narrative contract for company opportunity packages."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from .company_package import CompanyOpportunityPackage, package_fingerprint


PROMPT_VERSION = "company_outlook_v1"
MAX_CLAIMS_PER_SECTION = 5
MAX_CLAIM_CHARS = 600


@dataclass(frozen=True)
class CitedClaim:
    text: str
    citation_ids: tuple[str, ...]


@dataclass(frozen=True)
class CompanyNarrative:
    package_fingerprint: str
    outlook_direction: str
    outlook_horizon_days: int
    summary: CitedClaim
    thesis: CitedClaim
    positive_catalysts: tuple[CitedClaim, ...]
    negative_catalysts: tuple[CitedClaim, ...]
    risks: tuple[CitedClaim, ...]
    invalidators: tuple[CitedClaim, ...]
    uncertainty: CitedClaim
    model_version: str
    prompt_version: str


def narrative_cache_key(
    package_hash: str,
    model_version: str,
    prompt_version: str = PROMPT_VERSION,
) -> str:
    if not package_hash or not model_version or not prompt_version:
        raise ValueError("company narrative cache identity is incomplete")
    return hashlib.sha256(
        f"{package_hash}|{model_version}|{prompt_version}".encode("utf-8")
    ).hexdigest()


def parse_company_narrative(
    raw_response: str,
    package: CompanyOpportunityPackage,
    *,
    model_version: str,
    prompt_version: str = PROMPT_VERSION,
) -> CompanyNarrative:
    if not model_version or not prompt_version:
        raise ValueError("company narrative model and prompt versions are required")
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise ValueError("company narrative response must be valid JSON") from exc
    expected_fields = {
        "outlook_direction",
        "outlook_horizon_days",
        "summary",
        "thesis",
        "positive_catalysts",
        "negative_catalysts",
        "risks",
        "invalidators",
        "uncertainty",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise ValueError("company narrative response has an invalid field contract")
    if payload["outlook_direction"] != package.outlook_direction:
        raise ValueError("company narrative cannot alter the deterministic outlook")
    if payload["outlook_horizon_days"] != package.outlook_horizon_days:
        raise ValueError("company narrative cannot alter the outlook horizon")
    valid_citations = {evidence.evidence_id for evidence in package.evidence}
    if not valid_citations:
        raise ValueError("company narrative requires package evidence")

    narrative = CompanyNarrative(
        package_fingerprint=package_fingerprint(package),
        outlook_direction=package.outlook_direction,
        outlook_horizon_days=package.outlook_horizon_days,
        summary=_claim(payload["summary"], valid_citations, "summary"),
        thesis=_claim(payload["thesis"], valid_citations, "thesis"),
        positive_catalysts=_claim_list(
            payload["positive_catalysts"], valid_citations, "positive_catalysts"
        ),
        negative_catalysts=_claim_list(
            payload["negative_catalysts"], valid_citations, "negative_catalysts"
        ),
        risks=_claim_list(payload["risks"], valid_citations, "risks"),
        invalidators=_claim_list(
            payload["invalidators"], valid_citations, "invalidators"
        ),
        uncertainty=_claim(payload["uncertainty"], valid_citations, "uncertainty"),
        model_version=model_version,
        prompt_version=prompt_version,
    )
    return narrative


def _claim_list(value, valid_citations: set[str], field_name: str) -> tuple[CitedClaim, ...]:
    if not isinstance(value, list) or len(value) > MAX_CLAIMS_PER_SECTION:
        raise ValueError(
            f"{field_name} must be an array with at most {MAX_CLAIMS_PER_SECTION} claims"
        )
    return tuple(
        _claim(item, valid_citations, f"{field_name}[{index}]")
        for index, item in enumerate(value)
    )


def _claim(value, valid_citations: set[str], field_name: str) -> CitedClaim:
    if not isinstance(value, dict) or set(value) != {"text", "citation_ids"}:
        raise ValueError(f"{field_name} has an invalid claim contract")
    text = str(value["text"] or "").strip()
    citations = value["citation_ids"]
    if not text or len(text) > MAX_CLAIM_CHARS:
        raise ValueError(f"{field_name} text is empty or too long")
    if (
        not isinstance(citations, list)
        or not citations
        or any(not isinstance(citation, str) or not citation for citation in citations)
    ):
        raise ValueError(f"{field_name} requires citations")
    if len(citations) != len(set(citations)):
        raise ValueError(f"{field_name} contains duplicate citations")
    unknown = set(citations) - valid_citations
    if unknown:
        raise ValueError(f"{field_name} references unknown package evidence")
    return CitedClaim(text=text, citation_ids=tuple(citations))