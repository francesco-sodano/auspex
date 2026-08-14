"""Fake Azure SDK-boundary doubles for production-adapter integration tests
(arc42 §6.1).

These fake only the Cosmos/Blob/OpenAI *SDK* boundary — ``ContainerProxy``'s
``upsert_item``/``read_item``/``query_items``,
``BlobServiceClient.get_container_client(...)``'s ``upload_blob``/
``get_blob_client``, and ``AsyncAzureOpenAI().chat.completions.create``.
Every layer above that boundary (``CosmosRepository`` and the
``Cosmos*Sink`` adapters in :mod:`auspex.persistence.repositories`,
:class:`auspex.persistence.blob_client.BlobContext`,
:class:`auspex.providers.openai_provider.AzureOpenAIClient`, and the
extraction/narrative classes built on top of it) runs unmodified — this
module exists purely so those production adapters can be exercised without
real network/credentials.
"""

from __future__ import annotations

import re
from typing import Any

_FIELD_EQ_RE = re.compile(r"c\.(\w+)\s*=\s*(@\w+)")


class FakeCosmosResourceNotFoundError(Exception):
    """Stands in for ``azure.cosmos.exceptions.CosmosResourceNotFoundError``.

    Every call site that catches "item not found" in this codebase
    (:class:`~auspex.persistence.repositories.CosmosRepository.get`,
    ``CosmosNarrativeSink.find_by_cache_key``,
    ``CosmosWatermarkStore.get_watermark``) catches a bare ``Exception``, so
    any exception type exercises the exact same code path a real Cosmos
    404 would.
    """


class FakeCosmosContainer:
    """Fakes ``azure.cosmos.aio.ContainerProxy`` over a plain in-process dict."""

    def __init__(self) -> None:
        self._items: dict[str, dict] = {}

    async def upsert_item(self, body: dict) -> dict:
        self._items[body["id"]] = body
        return body

    async def read_item(self, item: str, partition_key: str | None = None) -> dict:
        if item not in self._items:
            raise FakeCosmosResourceNotFoundError(item)
        return self._items[item]

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict] | None = None,
        partition_key: str | None = None,
        enable_cross_partition_query: bool | None = None,
    ):
        """A deliberately simple query "engine": ``SELECT * FROM c`` (no
        WHERE) returns every row; ``WHERE c.field=@param [AND ...]``
        filters by equality on each named field — exactly the two query
        shapes every ``Cosmos*Sink``/``CosmosRepository.all()`` in
        :mod:`auspex.persistence.repositories` actually issues.
        """

        param_values = {p["name"]: p["value"] for p in (parameters or [])}
        field_params = _FIELD_EQ_RE.findall(query)  # [(field, "@param"), ...]

        def _matches(item: dict) -> bool:
            return all(str(item.get(field)) == str(param_values.get(param)) for field, param in field_params)

        matched = [item for item in self._items.values() if _matches(item)]

        async def _iterate():
            for item in matched:
                yield item

        return _iterate()


class FakeCosmosDatabase:
    def __init__(self) -> None:
        self._containers: dict[str, FakeCosmosContainer] = {}

    def get_container_client(self, name: str) -> FakeCosmosContainer:
        return self._containers.setdefault(name, FakeCosmosContainer())


class FakeCosmosContext:
    """Duck-types :class:`auspex.persistence.cosmos_client.CosmosContext` —
    only the ``container(name)`` surface ``CosmosRepository``/``Cosmos*Sink``
    actually call."""

    def __init__(self) -> None:
        self._database = FakeCosmosDatabase()

    async def container(self, name: str) -> FakeCosmosContainer:
        return self._database.get_container_client(name)


class _FakeBlobStream:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def readall(self) -> bytes:
        return self._data


class _FakeBlobClient:
    def __init__(self, store: dict[str, bytes], path: str) -> None:
        self._store = store
        self._path = path

    async def download_blob(self) -> _FakeBlobStream:
        return _FakeBlobStream(self._store[self._path])


class FakeBlobContainerClient:
    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store

    async def upload_blob(self, name: str, data: bytes, overwrite: bool = True) -> None:
        self._store[name] = data

    def get_blob_client(self, path: str) -> _FakeBlobClient:
        return _FakeBlobClient(self._store, path)


class FakeBlobServiceClient:
    """Duck-types ``azure.storage.blob.aio.BlobServiceClient`` — only the
    ``get_container_client`` surface :class:`~auspex.persistence.blob_client.BlobContext`
    actually calls."""

    def __init__(self) -> None:
        self._containers: dict[str, dict[str, bytes]] = {}

    def get_container_client(self, name: str) -> FakeBlobContainerClient:
        return FakeBlobContainerClient(self._containers.setdefault(name, {}))


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeChatResponse:
    def __init__(self, content: str | None) -> None:
        self.choices = [_FakeChoice(content)]


class QueuedFakeChatCompletions:
    """Fakes ``openai.AsyncAzureOpenAI().chat.completions.create`` for
    :class:`auspex.providers.openai_provider.AzureOpenAIClient` — the real,
    unmodified client class is exercised end to end (TPM budgeting,
    deployment/model routing included); only the outbound HTTP call is
    stubbed.

    Both Channel A and Channel B extraction share one deployment/model
    version by design (arc42 — a single "extraction" AOAI deployment), so
    the deployment name alone can't distinguish a Channel A call from a
    Channel B call; this routes JSON-mode calls by sniffing the user
    payload for Channel A's ``"taxonomy"`` key instead, and routes any
    non-JSON-mode call to the canned narrative text.
    """

    def __init__(self, *, channel_a_json: str, channel_b_json: str, narrative_text: str) -> None:
        self._channel_a_json = channel_a_json
        self._channel_b_json = channel_b_json
        self._narrative_text = narrative_text
        self.calls: list[dict[str, Any]] = []

    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
        response_format: dict | None = None,
        temperature: float = 0.0,
        stream: bool = False,
        **_: object,
    ) -> _FakeChatResponse:
        self.calls.append({"model": model, "messages": messages, "response_format": response_format})
        user_content = messages[1]["content"]
        if response_format == {"type": "json_object"}:
            content = self._channel_a_json if '"taxonomy"' in user_content else self._channel_b_json
        else:
            content = self._narrative_text
        return _FakeChatResponse(content)
