"""Unit tests for the Azure OpenAI client wrapper's TPM budgeting and
429 retry-with-backoff (arc42 §6.3 "Runtime budget").

No real network/credential resolution happens here: `AzureOpenAIClient`
construction never makes a network call (the Azure SDK client and the
managed-identity token provider are both lazy), and every test replaces the
underlying `chat.completions.create` with a fake async callable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from auspex.providers.openai_provider import AzureOpenAIClient, estimate_tokens


def make_rate_limit_error() -> openai.RateLimitError:
    request = httpx.Request("POST", "https://aoai-test.openai.azure.com/")
    response = httpx.Response(429, request=request)
    return openai.RateLimitError("rate limited", response=response, body=None)


def make_client(
    tokens_per_minute: float = 450_000.0,
    tokens_per_minute_by_deployment: dict[str, float] | None = None,
) -> AzureOpenAIClient:
    client = AzureOpenAIClient(
        endpoint="https://aoai-test.openai.azure.com/",
        api_version="2024-10-21",
        tokens_per_minute=tokens_per_minute,
        tokens_per_minute_by_deployment=tokens_per_minute_by_deployment,
    )
    return client


class FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = type("M", (), {"content": content})()


class FakeResponse:
    def __init__(self, content: str | None) -> None:
        self.choices = [FakeChoice(content)]


class TestEstimateTokens:
    def test_longer_text_estimates_more_tokens(self):
        short = estimate_tokens("hello")
        long = estimate_tokens("hello " * 1000)
        assert long > short

    def test_output_reserve_always_included(self):
        assert estimate_tokens("") >= 5000

    def test_multiple_texts_summed(self):
        combined = estimate_tokens("abc", "def")
        single = estimate_tokens("abcdef")
        assert combined == pytest.approx(single, abs=0.01)


class TestAzureOpenAIClientBudgeting:
    def test_bucket_sized_from_tokens_per_minute(self):
        client = make_client(tokens_per_minute=120_000.0)
        assert client._bucket.capacity == 120_000.0
        assert client._bucket.rate == pytest.approx(2000.0)

    def test_default_tokens_per_minute_matches_confirmed_quota(self):
        client = make_client()
        assert client._bucket.capacity == 450_000.0

    def test_deployment_specific_bucket_uses_its_own_quota(self):
        client = make_client(
            tokens_per_minute_by_deployment={"gpt-4.1": 30_000.0}
        )
        bucket = client._deployment_buckets["gpt-4.1"]
        assert bucket.capacity == 30_000.0
        assert bucket.rate == pytest.approx(500.0)

    @pytest.mark.asyncio
    async def test_text_completion_charges_deployment_specific_bucket(self):
        client = make_client(
            tokens_per_minute_by_deployment={"gpt-4.1": 30_000.0}
        )
        client._client.chat.completions.create = AsyncMock(
            return_value=FakeResponse("ok")
        )
        deployment_bucket = client._deployment_buckets["gpt-4.1"]
        default_tokens = client._bucket._tokens

        await client.complete_text(
            deployment="gpt-4.1",
            system_prompt="system",
            user_content="user",
        )

        assert deployment_bucket._tokens < 30_000.0
        assert 30_000.0 - deployment_bucket._tokens < 1_000.0
        assert client._bucket._tokens == default_tokens


class TestCompleteJson:
    @pytest.mark.asyncio
    async def test_returns_content_and_calls_create_once_on_success(self):
        client = make_client()
        client._client.chat.completions.create = AsyncMock(return_value=FakeResponse('{"ok": true}'))

        result = await client.complete_json(
            deployment="gpt-4.1-mini", system_prompt="system", user_content="user"
        )

        assert result == '{"ok": true}'
        assert client._client.chat.completions.create.call_count == 1

    @pytest.mark.asyncio
    async def test_none_content_defaults_to_empty_json_object(self):
        client = make_client()
        client._client.chat.completions.create = AsyncMock(return_value=FakeResponse(None))

        result = await client.complete_json(deployment="gpt-4.1-mini", system_prompt="s", user_content="u")

        assert result == "{}"

    @pytest.mark.asyncio
    async def test_acquires_tokens_from_budget_before_calling(self):
        client = make_client(tokens_per_minute=1_000_000.0)
        client._client.chat.completions.create = AsyncMock(return_value=FakeResponse("{}"))
        tokens_before = client._bucket._tokens

        await client.complete_json(deployment="gpt-4.1-mini", system_prompt="x" * 100, user_content="y" * 100)

        assert client._bucket._tokens < tokens_before

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit_error_then_succeeds(self, monkeypatch):
        client = make_client()
        calls = {"n": 0}

        async def flaky_create(**kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise make_rate_limit_error()
            return FakeResponse('{"ok": true}')

        client._client.chat.completions.create = flaky_create
        # Skip real sleeping between retries in the test.
        monkeypatch.setattr("auspex.providers.openai_provider.backoff_sleep", AsyncMock(return_value=None))

        result = await client.complete_json(deployment="gpt-4.1-mini", system_prompt="s", user_content="u")

        assert result == '{"ok": true}'
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_exhausting_retries_raises_rate_limit_error(self, monkeypatch):
        client = make_client()

        async def always_fails(**kwargs):
            raise make_rate_limit_error()

        client._client.chat.completions.create = always_fails
        monkeypatch.setattr("auspex.providers.openai_provider.backoff_sleep", AsyncMock(return_value=None))

        with pytest.raises(openai.RateLimitError):
            await client.complete_json(deployment="gpt-4.1-mini", system_prompt="s", user_content="u")


class TestCompleteText:
    @pytest.mark.asyncio
    async def test_returns_text_content(self):
        client = make_client()
        client._client.chat.completions.create = AsyncMock(return_value=FakeResponse("hello world"))

        result = await client.complete_text(deployment="gpt-4.1", system_prompt="s", user_content="u")

        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_none_content_defaults_to_empty_string(self):
        client = make_client()
        client._client.chat.completions.create = AsyncMock(return_value=FakeResponse(None))

        result = await client.complete_text(deployment="gpt-4.1", system_prompt="s", user_content="u")

        assert result == ""


class FakeDelta:
    def __init__(self, content: str | None) -> None:
        self.content = content


class FakeStreamChoice:
    def __init__(self, content: str | None) -> None:
        self.delta = FakeDelta(content)


class FakeStreamChunk:
    def __init__(self, choices: list) -> None:
        self.choices = choices


class TestStreamText:
    @pytest.mark.asyncio
    async def test_yields_only_non_empty_content_chunks(self):
        client = make_client()

        async def fake_stream():
            yield FakeStreamChunk([FakeStreamChoice("Hello")])
            yield FakeStreamChunk([])  # no choices — skipped
            yield FakeStreamChunk([FakeStreamChoice(None)])  # empty delta — skipped
            yield FakeStreamChunk([FakeStreamChoice(" world")])

        client._client.chat.completions.create = AsyncMock(return_value=fake_stream())

        chunks = [c async for c in client.stream_text(deployment="gpt-4.1", system_prompt="s", user_content="u")]

        assert chunks == ["Hello", " world"]

    @pytest.mark.asyncio
    async def test_acquires_budget_before_streaming(self):
        client = make_client(tokens_per_minute=1_000_000.0)
        tokens_before = client._bucket._tokens

        async def fake_stream():
            return
            yield  # pragma: no cover - unreachable, makes this an async generator

        client._client.chat.completions.create = AsyncMock(return_value=fake_stream())

        async for _ in client.stream_text(deployment="gpt-4.1", system_prompt="x" * 200, user_content="y" * 200):
            pass

        assert client._bucket._tokens < tokens_before
