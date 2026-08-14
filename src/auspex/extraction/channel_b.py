"""Channel B — prose digest + comparative diff (arc42 §5.4).

Maximally informative: 150-250 word digest, verbatim key quotes, and a
comparative diff against the prior comparable filing when supplied. Never
touches numbers.
"""

from __future__ import annotations

import json
from typing import Protocol

from auspex.extraction.cache import channel_b_cache_key
from auspex.extraction.json_response import load_model_json
from auspex.extraction.sections import Section, bound_sections
from auspex.models.common import new_id
from auspex.models.enums import (
    GuidanceLanguageShift,
    MdaToneShift,
    RiskCategory,
    RiskDirection,
    RiskSeverity,
)
from auspex.models.extraction import ChannelBDigest
from auspex.providers.openai_provider import AzureOpenAIClient

_DOMAIN_FIELDS = (
    "headline",
    "digest",
    "key_quotes",
    "management_claims",
    "unanswered_questions",
    "comparative",
)


class ChannelBDigestSink(Protocol):
    async def find_by_cache_key(self, cache_key: str) -> ChannelBDigest | None: ...
    async def upsert(self, digest: ChannelBDigest) -> None: ...


class ChannelBExtractor:
    prompt_version = "digest-b-v1"

    def __init__(
        self,
        *,
        openai_client: AzureOpenAIClient,
        deployment: str,
        system_prompt: str,
        model_version: str,
        sink: ChannelBDigestSink,
    ) -> None:
        self._openai = openai_client
        self._deployment = deployment
        self._system_prompt = system_prompt
        self._model_version = model_version
        self._sink = sink

    def build_user_content(
        self,
        *,
        ticker: str,
        form_type: str,
        sections: list[Section],
        prior_sections: list[Section] | None,
    ) -> str:
        payload = {
            "security": {"ticker": ticker},
            "document": {"form_type": form_type},
            "sections": [
                {"item": s.item, "text": s.text}
                for s in bound_sections(sections, max_chars=150_000)
            ],
        }
        if prior_sections is not None:
            payload["prior_document"] = {
                "sections": [
                    {"item": s.item, "text": s.text}
                    for s in bound_sections(prior_sections, max_chars=150_000)
                ]
            }
        return json.dumps(payload, ensure_ascii=False)

    def parse_response(
        self,
        raw_json: str,
        *,
        security_id: str,
        document_id: str,
        content_hash: str,
    ) -> ChannelBDigest:
        data = load_model_json(raw_json)
        domain_data = {k: data[k] for k in _DOMAIN_FIELDS if k in data}
        domain_data.setdefault("headline", "Document update")
        domain_data.setdefault("digest", "No evidence digest was returned.")
        domain_data["key_quotes"] = [
            {
                key: value
                for key, value in quote.items()
                if key in {"text", "section", "why_it_matters"}
            }
            for quote in domain_data.get("key_quotes", [])
            if isinstance(quote, dict)
            and all(key in quote for key in ("text", "section", "why_it_matters"))
        ]
        comparative = domain_data.get("comparative")
        if isinstance(comparative, dict):
            comparative = {
                key: value
                for key, value in comparative.items()
                if key
                in {
                    "prior_document_id",
                    "risk_factors_added",
                    "risk_factors_removed",
                    "risk_factors_reworded",
                    "guidance_language_shift",
                    "mda_tone_shift",
                }
            }
            domain_data["comparative"] = comparative
            added = []
            for risk in comparative.get("risk_factors_added", []):
                if not isinstance(risk, dict) or not all(
                    key in risk for key in ("summary", "verbatim")
                ):
                    continue
                risk = {
                    key: value
                    for key, value in risk.items()
                    if key in {"summary", "verbatim", "category", "severity"}
                }
                if risk.get("category") not in {item.value for item in RiskCategory}:
                    risk["category"] = RiskCategory.OTHER.value
                if risk.get("severity") not in {item.value for item in RiskSeverity}:
                    risk["severity"] = RiskSeverity.LOW.value
                added.append(risk)
            comparative["risk_factors_added"] = added
            comparative["risk_factors_removed"] = [
                {
                    key: value
                    for key, value in risk.items()
                    if key in {"summary", "prior_verbatim"}
                }
                for risk in comparative.get("risk_factors_removed", [])
                if isinstance(risk, dict)
                and all(key in risk for key in ("summary", "prior_verbatim"))
            ]
            comparative["risk_factors_reworded"] = [
                {
                    key: value
                    for key, value in risk.items()
                    if key in {"summary", "before", "after", "direction"}
                }
                for risk in comparative.get("risk_factors_reworded", [])
                if isinstance(risk, dict)
                and all(key in risk for key in ("summary", "before", "after"))
                and risk.get("direction") in {item.value for item in RiskDirection}
            ]
            if comparative.get("guidance_language_shift") not in {
                item.value for item in GuidanceLanguageShift
            }:
                comparative["guidance_language_shift"] = GuidanceLanguageShift.UNCHANGED.value
            if comparative.get("mda_tone_shift") not in {
                item.value for item in MdaToneShift
            }:
                comparative["mda_tone_shift"] = MdaToneShift.UNCHANGED.value
        return ChannelBDigest(
            id=new_id(),
            security_id=security_id,
            document_id=document_id,
            content_hash=content_hash,
            model_version=self._model_version,
            prompt_version=self.prompt_version,
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
        prior_sections: list[Section] | None = None,
    ) -> ChannelBDigest:
        cache_key = channel_b_cache_key(
            content_hash=content_hash, model_version=self._model_version, prompt_version=self.prompt_version
        )
        cached = await self._sink.find_by_cache_key(cache_key)
        if cached is not None:
            return cached

        user_content = self.build_user_content(
            ticker=ticker, form_type=form_type, sections=sections, prior_sections=prior_sections
        )
        raw_json = await self._openai.complete_json(
            deployment=self._deployment, system_prompt=self._system_prompt, user_content=user_content
        )
        digest = self.parse_response(
            raw_json, security_id=security_id, document_id=document_id, content_hash=content_hash
        )
        await self._sink.upsert(digest)
        return digest
