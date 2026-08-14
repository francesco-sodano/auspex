"""Azure OpenAI client wrapper — managed identity, no API keys (arc42 TC-04, TC-05).

Deployments are pinned to explicit model versions via ``Settings``; no
"auto-update" alias is ever used. Extraction/planner calls use JSON mode;
narrative/answer calls support streaming.

arc42 §6.3 "Runtime budget": bootstrap's Channel A + B extraction is
"bounded entirely by the deployment's tokens-per-minute quota" — the
`gpt-4.1-mini` deployment is confirmed provisioned at 450,000 TPM (Sweden
Central region ceiling: 5,000,000 TPM, so headroom exists for a quota
increase without a region change). Every completion call here is paced
against that budget via a token-based :class:`~auspex.providers.rate_limit.TokenBucket`
*before* the request is sent, and retried with exponential backoff if Azure
still returns a 429 (e.g. concurrent callers, or a shorter-window burst
limit than the steady-state TPM figure) — mirroring the same token-bucket +
backoff pattern used for EDGAR/Alpha Vantage/Finnhub.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import openai
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

from auspex.providers.rate_limit import TokenBucket, backoff_sleep

MAX_RETRIES = 5

# A rough, deliberately conservative chars-per-token estimate (English prose
# and JSON both average closer to 4, but padding the estimate upward means
# the budget is never *under*-reserved, only occasionally slightly generous)
# plus a fixed reserve for the model's own output tokens, which are paid for
# out of the same per-minute budget but aren't known until the response
# arrives.
_CHARS_PER_TOKEN_ESTIMATE = 3.5
_OUTPUT_TOKEN_RESERVE = 5000
_TEXT_OUTPUT_TOKEN_RESERVE = 500
_STREAM_OUTPUT_TOKEN_RESERVE = 2000


def estimate_tokens(*texts: str, output_reserve: int = _OUTPUT_TOKEN_RESERVE) -> float:
    """Conservative pre-flight token estimate for TPM budgeting, not billing:
    it only needs to keep the request rate inside the deployment's quota, not
    match Azure's own tokenizer exactly."""

    input_chars = sum(len(t) for t in texts)
    return (input_chars / _CHARS_PER_TOKEN_ESTIMATE) + output_reserve


class AzureOpenAIClient:
    def __init__(
        self,
        *,
        endpoint: str,
        api_version: str,
        credential: DefaultAzureCredential | None = None,
        tokens_per_minute: float = 450_000.0,
        tokens_per_minute_by_deployment: dict[str, float] | None = None,
    ) -> None:
        self._credential = credential or DefaultAzureCredential()
        token_provider = get_bearer_token_provider(self._credential, "https://cognitiveservices.azure.com/.default")
        self._client = AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            api_version=api_version,
            azure_ad_token_provider=token_provider,
        )
        # Capacity = the full per-minute quota, so a fresh process can burst up
        # to the whole budget immediately rather than waiting for a slow refill
        # on its very first call (same reasoning as the EDGAR/provider buckets).
        self._bucket = TokenBucket(rate_per_second=tokens_per_minute / 60.0, capacity=tokens_per_minute)
        self._deployment_buckets = {
            deployment: TokenBucket(
                rate_per_second=deployment_tpm / 60.0,
                capacity=deployment_tpm,
            )
            for deployment, deployment_tpm in (
                tokens_per_minute_by_deployment or {}
            ).items()
        }

    async def aclose(self) -> None:
        await self._client.close()
        await self._credential.close()

    async def _call_with_retry(self, deployment: str, estimated_tokens: float, call):
        bucket = self._deployment_buckets.get(deployment, self._bucket)
        await bucket.acquire(estimated_tokens)
        for attempt in range(MAX_RETRIES):
            try:
                return await call()
            except openai.RateLimitError as exc:
                if attempt == MAX_RETRIES - 1:
                    raise
                retry_after = 0.0
                if exc.response is not None:
                    headers = exc.response.headers
                    if headers.get("retry-after-ms"):
                        retry_after = float(headers["retry-after-ms"]) / 1000
                    elif headers.get("retry-after"):
                        retry_after = float(headers["retry-after"])
                if retry_after > 0:
                    await asyncio.sleep(retry_after)
                else:
                    await backoff_sleep(attempt + 3)
        raise AssertionError("unreachable")  # pragma: no cover - loop always returns or raises

    async def complete_json(
        self,
        *,
        deployment: str,
        system_prompt: str,
        user_content: str,
        temperature: float = 0.0,
    ) -> str:
        """Non-streaming JSON-mode completion (Channel A/B extraction, planner)."""

        async def _call():
            response = await self._client.chat.completions.create(
                model=deployment,
                temperature=temperature,
                max_tokens=5000,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            return response.choices[0].message.content or "{}"

        return await self._call_with_retry(
            deployment,
            estimate_tokens(system_prompt, user_content),
            _call,
        )

    async def complete_text(
        self,
        *,
        deployment: str,
        system_prompt: str,
        user_content: str,
        temperature: float = 0.2,
    ) -> str:
        """Non-streaming plain-text completion (narrative generator)."""

        async def _call():
            response = await self._client.chat.completions.create(
                model=deployment,
                temperature=temperature,
                max_tokens=500,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            return response.choices[0].message.content or ""

        return await self._call_with_retry(
            deployment,
            estimate_tokens(
                system_prompt,
                user_content,
                output_reserve=_TEXT_OUTPUT_TOKEN_RESERVE,
            ),
            _call,
        )

    async def stream_text(
        self,
        *,
        deployment: str,
        system_prompt: str,
        user_content: str,
        temperature: float = 0.2,
    ) -> AsyncIterator[str]:
        """Streaming plain-text completion (assistant Pass 2 answer, SSE).

        The budget is still acquired up front (the token cost is real
        regardless of streaming), but retry-on-429 only covers the *request*
        that opens the stream — once tokens start arriving there is nothing
        sensible to retry.
        """

        bucket = self._deployment_buckets.get(deployment, self._bucket)
        await bucket.acquire(
            estimate_tokens(
                system_prompt,
                user_content,
                output_reserve=_STREAM_OUTPUT_TOKEN_RESERVE,
            )
        )
        stream = await self._client.chat.completions.create(
            model=deployment,
            temperature=temperature,
            max_tokens=2000,
            stream=True,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content
