"""Channel A — scoring-label extraction (arc42 §5.4).

Maximally constrained: enum labels + short verbatim excerpts only. The model
never touches arithmetic; its output only selects which versioned numeric
mapping applies (``config/label_mappings.yaml``, ``config/weights.yaml``).
"""

from __future__ import annotations

import json
from typing import Protocol

from auspex.extraction.cache import channel_a_cache_key
from auspex.extraction.json_response import load_model_json
from auspex.extraction.sections import Section, bound_sections
from auspex.models.common import new_id
from auspex.models.enums import (
    ExtractionConfidence,
    GuidanceDirection,
    Materiality,
    NarrativeClaimType,
    Novelty,
    RiskCategory,
    RiskSeverity,
    Sentiment,
    ThemeStrength,
)
from auspex.models.extraction import ChannelAExtraction
from auspex.providers.openai_provider import AzureOpenAIClient

_DOMAIN_FIELDS = (
    "materiality",
    "sentiment",
    "guidance_direction",
    "novelty",
    "theme_claims",
    "risk_claims",
    "narrative_claims",
    "extraction_confidence",
)


class ChannelAExtractionSink(Protocol):
    async def find_by_cache_key(self, cache_key: str) -> ChannelAExtraction | None: ...
    async def upsert(self, extraction: ChannelAExtraction) -> None: ...


class ChannelAExtractor:
    prompt_version = "extract-a-v1"
    schema_version = "4.0"

    def __init__(
        self,
        *,
        openai_client: AzureOpenAIClient,
        deployment: str,
        system_prompt: str,
        model_version: str,
        taxonomy_version: str,
        sink: ChannelAExtractionSink,
    ) -> None:
        self._openai = openai_client
        self._deployment = deployment
        self._system_prompt = system_prompt
        self._model_version = model_version
        self._taxonomy_version = taxonomy_version
        self._sink = sink

    def build_user_content(
        self,
        *,
        ticker: str,
        form_type: str,
        sections: list[Section],
        taxonomy_theme_ids: list[str],
    ) -> str:
        payload = {
            "security": {"ticker": ticker},
            "document": {"form_type": form_type},
            "sections": [
                {"item": s.item, "text": s.text} for s in bound_sections(sections)
            ],
            "taxonomy": {"theme_ids": taxonomy_theme_ids},
        }
        return json.dumps(payload, ensure_ascii=False)

    def parse_response(
        self,
        raw_json: str,
        *,
        security_id: str,
        document_id: str,
        content_hash: str,
    ) -> ChannelAExtraction:
        data = load_model_json(raw_json)
        domain_data = {k: data[k] for k in _DOMAIN_FIELDS if k in data}
        defaults = {
            "materiality": (Materiality, Materiality.NONE.value),
            "sentiment": (Sentiment, Sentiment.NEUTRAL.value),
            "guidance_direction": (GuidanceDirection, GuidanceDirection.NONE.value),
            "novelty": (Novelty, Novelty.ROUTINE.value),
            "extraction_confidence": (
                ExtractionConfidence,
                ExtractionConfidence.LOW.value,
            ),
        }
        for field, (enum_type, default) in defaults.items():
            if domain_data.get(field) not in {item.value for item in enum_type}:
                domain_data[field] = default

        def valid_claims(field, validators, allowed_fields):
            claims = domain_data.get(field, [])
            return [
                {key: value for key, value in claim.items() if key in allowed_fields}
                for claim in claims
                if isinstance(claim, dict)
                and all(
                    claim.get(key) in {item.value for item in enum_type}
                    for key, enum_type in validators.items()
                )
            ]

        domain_data["theme_claims"] = valid_claims(
            "theme_claims",
            {"strength": ThemeStrength},
            {"theme_id", "strength", "evidence_excerpt", "location_hint"},
        )
        domain_data["risk_claims"] = valid_claims(
            "risk_claims",
            {"category": RiskCategory, "severity": RiskSeverity},
            {"category", "severity", "evidence_excerpt"},
        )
        domain_data["narrative_claims"] = valid_claims(
            "narrative_claims",
            {"claim_type": NarrativeClaimType, "strength": ThemeStrength},
            {"claim_type", "strength", "evidence_excerpt"},
        )
        return ChannelAExtraction(
            id=new_id(),
            security_id=security_id,
            document_id=document_id,
            content_hash=content_hash,
            model_version=self._model_version,
            prompt_version=self.prompt_version,
            schema_version=self.schema_version,
            taxonomy_version=self._taxonomy_version,
            **domain_data,
        )

    async def extract(
        self,
        *,
        security_id: str,
        document_id: str,
        content_hash: str,
        ticker: str,
        form_type: str,
        sections: list[Section],
        taxonomy_theme_ids: list[str],
    ) -> ChannelAExtraction:
        cache_key = channel_a_cache_key(
            content_hash=content_hash,
            model_version=self._model_version,
            prompt_version=self.prompt_version,
            schema_version=self.schema_version,
            taxonomy_version=self._taxonomy_version,
        )
        cached = await self._sink.find_by_cache_key(cache_key)
        if cached is not None:
            return cached

        user_content = self.build_user_content(
            ticker=ticker, form_type=form_type, sections=sections, taxonomy_theme_ids=taxonomy_theme_ids
        )
        raw_json = await self._openai.complete_json(
            deployment=self._deployment, system_prompt=self._system_prompt, user_content=user_content
        )
        extraction = self.parse_response(
            raw_json, security_id=security_id, document_id=document_id, content_hash=content_hash
        )
        await self._sink.upsert(extraction)
        return extraction
