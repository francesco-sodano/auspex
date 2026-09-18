"""Daily narrative generator (arc42 §5.9).

Runs per company per day. Receives the final deterministic package, the leg
change record, Channel B digests for today's evidence bundle, and the
comparative record. Cannot alter a number, invent a citation, change a
direction, or create an action.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

from auspex.models.extraction import ChannelBDigest, ComparativeDiff
from auspex.narrative.fingerprint import compute_package_fingerprint
from auspex.providers.openai_provider import AzureOpenAIClient

logger = logging.getLogger(__name__)
MAX_NARRATIVE_INPUT_CHARS = 24_000
MAX_NARRATIVE_DIGESTS = 8
MAX_NARRATIVE_SUMMARY_CHARS = 1_200


class NarrativeSink(Protocol):
    async def find_by_cache_key(self, cache_key: str) -> str | None: ...
    async def store(self, cache_key: str, narrative: str, model_version: str) -> None: ...


class NarrativeGenerator:
    prompt_version = "narrative-v2"

    def __init__(
        self,
        *,
        openai_client: AzureOpenAIClient,
        deployment: str,
        system_prompt: str,
        model_version: str,
        sink: NarrativeSink,
    ) -> None:
        self._openai = openai_client
        self._deployment = deployment
        self._system_prompt = system_prompt
        self._model_version = model_version
        self._sink = sink

    def cache_key(self, package_fingerprint: str) -> str:
        return "|".join([package_fingerprint, self._model_version, self.prompt_version])

    def build_user_content(
        self,
        *,
        package: dict,
        leg_changes: list[dict],
        digests: list[ChannelBDigest],
        comparative: ComparativeDiff | None,
    ) -> str:
        selected = digests[:MAX_NARRATIVE_DIGESTS]
        summaries = [
            {
                "document_id": digest.document_id,
                "headline": digest.headline[:200],
                "summary": (digest.plain_summary or digest.digest)[:MAX_NARRATIVE_SUMMARY_CHARS],
            }
            for digest in selected
        ]
        limited = len(selected) != len(digests) or any(
            len(digest.headline) > 200
            or len(digest.plain_summary or digest.digest) > MAX_NARRATIVE_SUMMARY_CHARS
            for digest in selected
        )
        payload = {
            "package": package,
            "leg_changes": leg_changes,
            "digests": summaries,
            "comparative": comparative.model_dump(mode="json") if comparative else None,
            "context_limited": limited,
        }
        content = json.dumps(payload, ensure_ascii=False)
        if len(content) > MAX_NARRATIVE_INPUT_CHARS and payload["comparative"] is not None:
            payload["comparative"] = None
            payload["context_limited"] = True
            content = json.dumps(payload, ensure_ascii=False)
        while len(content) > MAX_NARRATIVE_INPUT_CHARS and summaries:
            summaries.pop()
            payload["context_limited"] = True
            content = json.dumps(payload, ensure_ascii=False)
        if len(content) > MAX_NARRATIVE_INPUT_CHARS:
            raise ValueError("The authoritative score package exceeds the narrative input budget.")
        if payload["context_limited"]:
            logger.info(
                "Bounded secondary narrative context for %s; retained_digests=%d",
                package.get("security_id"),
                len(summaries),
            )
        return content

    async def generate(
        self,
        *,
        package: dict,
        leg_changes: list[dict],
        digests: list[ChannelBDigest],
        comparative: ComparativeDiff | None = None,
    ) -> str:
        user_content = self.build_user_content(
            package=package, leg_changes=leg_changes, digests=digests, comparative=comparative
        )
        fingerprint = compute_package_fingerprint({
            "package": package,
            "source_input": user_content,
            "system_prompt": self._system_prompt,
        })
        key = self.cache_key(fingerprint)
        cached = await self._sink.find_by_cache_key(key)
        if cached is not None:
            return cached

        narrative = await self._openai.complete_text(
            deployment=self._deployment, system_prompt=self._system_prompt, user_content=user_content
        )
        await self._sink.store(key, narrative, self._model_version)
        return narrative
