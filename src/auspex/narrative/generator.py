"""Daily narrative generator (arc42 §5.9).

Runs per company per day. Receives the final deterministic package, the leg
change record, Channel B digests for today's evidence bundle, and the
comparative record. Cannot alter a number, invent a citation, change a
direction, or create an action.
"""

from __future__ import annotations

import json
from typing import Protocol

from auspex.models.extraction import ChannelBDigest, ComparativeDiff
from auspex.narrative.fingerprint import compute_package_fingerprint
from auspex.providers.openai_provider import AzureOpenAIClient


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
        payload = {
            "package": package,
            "leg_changes": leg_changes,
            "digests": [d.model_dump(mode="json") for d in digests],
            "comparative": comparative.model_dump(mode="json") if comparative else None,
        }
        return json.dumps(payload, ensure_ascii=False)

    async def generate(
        self,
        *,
        package: dict,
        leg_changes: list[dict],
        digests: list[ChannelBDigest],
        comparative: ComparativeDiff | None = None,
    ) -> str:
        fingerprint = compute_package_fingerprint(package)
        key = self.cache_key(fingerprint)
        cached = await self._sink.find_by_cache_key(key)
        if cached is not None:
            return cached

        user_content = self.build_user_content(
            package=package, leg_changes=leg_changes, digests=digests, comparative=comparative
        )
        narrative = await self._openai.complete_text(
            deployment=self._deployment, system_prompt=self._system_prompt, user_content=user_content
        )
        await self._sink.store(key, narrative, self._model_version)
        return narrative
