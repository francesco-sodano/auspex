"""Constrained LLM classifier for securities not covered by tracked ETF themes."""

from dataclasses import dataclass
import json
import math


@dataclass(frozen=True)
class ThemeClassification:
    theme_id: str
    confidence: float
    rationale: str
    provenance: str = "llm"


class ThemeClassificationService:
    def __init__(self, chat, themes: dict[str, str], confidence_cap: float = 0.85) -> None:
        if not themes:
            raise ValueError("At least one theme is required")
        self._chat = chat
        self._themes = dict(themes)
        self._confidence_cap = float(confidence_cap)

    def classify(
        self,
        *,
        ticker: str,
        company_name: str,
        filing_type: str,
        business_description: str,
    ) -> ThemeClassification:
        description = " ".join(str(business_description or "").split())
        if len(description) < 200:
            raise ValueError("Business description is too short for classification")
        theme_catalog = "\n".join(
            f"- {theme_id}: {theme_name}"
            for theme_id, theme_name in sorted(self._themes.items())
        )
        response = json.loads(self._chat.complete_json([
            {
                "role": "system",
                "content": (
                    "Classify a public company into exactly one allowed investment theme. "
                    "Use only the supplied SEC filing business description. Return JSON with "
                    "exactly theme_id, confidence, and rationale. Confidence must be between "
                    "0 and 1. Do not invent a theme."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Ticker: {ticker}\nCompany: {company_name}\nFiling: {filing_type}\n"
                    f"Allowed themes:\n{theme_catalog}\n\nBusiness description:\n{description[:30000]}"
                ),
            },
        ]))
        if set(response) != {"theme_id", "confidence", "rationale"}:
            raise ValueError("Theme classification response has unexpected fields")
        theme_id = str(response["theme_id"]).strip()
        if theme_id not in self._themes:
            raise ValueError("Theme classification is outside the allowed catalog")
        confidence = float(response["confidence"])
        if not math.isfinite(confidence) or confidence < 0.0:
            raise ValueError("Theme classification confidence is invalid")
        confidence = min(confidence, self._confidence_cap)
        rationale = " ".join(str(response["rationale"] or "").split())
        if not rationale or len(rationale) > 500:
            raise ValueError("Theme classification rationale is invalid")
        return ThemeClassification(
            theme_id=theme_id,
            confidence=confidence,
            rationale=rationale,
        )