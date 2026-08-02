import json
import re
from decimal import Decimal, InvalidOperation


_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?")
_PROHIBITED_CLAIMS = (
    "guaranteed return",
    "guaranteed profit",
    "risk-free",
    "i executed",
    "order placed",
)
_UPPERCASE_TOKEN_PATTERN = re.compile(r"\b[A-Z][A-Z0-9.]{1,7}\b")
_ALLOWED_FINANCIAL_TERMS = {
    "ADD", "AI", "BUY", "CHF", "ETF", "EUR", "GBP", "HOLD", "SEC",
    "SELL", "TRIM", "USD",
}


class GroundingViolation(ValueError):
    pass


def _number_tokens(value: object) -> set[str]:
    return set(_NUMBER_PATTERN.findall(json.dumps(value, sort_keys=True, default=str)))


def _allowed_number_tokens(recommendation: dict, citations: list[dict]) -> set[str]:
    tokens = _number_tokens(recommendation) | _number_tokens(citations)
    for field in ("current_weight", "target_weight"):
        try:
            percentage = Decimal(str(recommendation.get(field))) * 100
        except (InvalidOperation, TypeError):
            continue
        tokens.update({str(percentage), format(percentage, "f").rstrip("0").rstrip(".")})
    return tokens


def validate_narration(
    output: dict,
    recommendation: dict,
    citations: list[dict],
) -> dict:
    if not isinstance(output, dict):
        raise GroundingViolation("narration output must be an object")
    for field in (
        "recommendation_id", "ticker", "action", "explanation",
        "uncertainty", "evidence_ids",
    ):
        if field not in output:
            raise GroundingViolation(f"narration output is missing {field}")

    for field in ("recommendation_id", "ticker", "action"):
        if output[field] != recommendation[field]:
            raise GroundingViolation(f"narration changed deterministic {field}")

    explanation = output["explanation"]
    uncertainty = output["uncertainty"]
    if not isinstance(explanation, str) or not 1 <= len(explanation) <= 1200:
        raise GroundingViolation("explanation length is invalid")
    if not isinstance(uncertainty, str) or not 1 <= len(uncertainty) <= 500:
        raise GroundingViolation("uncertainty length is invalid")

    evidence_ids = output["evidence_ids"]
    if not isinstance(evidence_ids, list) or not evidence_ids:
        raise GroundingViolation("at least one evidence citation is required")
    known_evidence_ids = {citation.get("id") for citation in citations}
    if any(evidence_id not in known_evidence_ids for evidence_id in evidence_ids):
        raise GroundingViolation("narration cited evidence outside the supplied pack")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise GroundingViolation("narration contains duplicate evidence citations")

    allowed_numbers = _allowed_number_tokens(recommendation, citations)
    narrated_numbers = _number_tokens({
        "explanation": explanation,
        "uncertainty": uncertainty,
    })
    invented_numbers = narrated_numbers - allowed_numbers
    if invented_numbers:
        raise GroundingViolation(
            f"narration introduced unsupported numbers: {sorted(invented_numbers)}"
        )

    allowed_tickers = {
        str(recommendation["ticker"]).upper(),
        *(
            str(citation.get("symbol")).upper()
            for citation in citations
            if citation.get("symbol")
        ),
    }
    uppercase_tokens = set(_UPPERCASE_TOKEN_PATTERN.findall(
        f"{explanation} {uncertainty}"
    ))
    unsupported_tickers = uppercase_tokens - allowed_tickers - _ALLOWED_FINANCIAL_TERMS
    if unsupported_tickers:
        raise GroundingViolation(
            f"narration introduced unsupported ticker-like tokens: {sorted(unsupported_tickers)}"
        )

    normalized_text = f"{explanation} {uncertainty}".lower()
    if any(claim in normalized_text for claim in _PROHIBITED_CLAIMS):
        raise GroundingViolation("narration contains a prohibited claim")

    return {
        "recommendation_id": output["recommendation_id"],
        "ticker": output["ticker"],
        "action": output["action"],
        "explanation": explanation.strip(),
        "uncertainty": uncertainty.strip(),
        "evidence_ids": list(evidence_ids),
    }