"""Unit tests for the uniform read-access bridge between in-memory test
fixtures and production Cosmos/Blob-backed sinks (arc42 §6.1).
"""

from __future__ import annotations

from auspex.pipeline.repo_access import fetch_all, read_blob_text


class SyncAllSink:
    """Shape of an in-memory test fixture — a plain synchronous ``.all()``."""

    def __init__(self, items: list) -> None:
        self._items = items

    def all(self) -> list:
        return list(self._items)


class AsyncAllSink:
    """Shape of a Cosmos-backed production adapter — an async ``.all()``."""

    def __init__(self, items: list) -> None:
        self._items = items

    async def all(self) -> list:
        return list(self._items)


class AsyncQueryOnlySink:
    """Shape of a bare CosmosRepository passed directly as a sink (no
    ``.all()``, only the lower-level ``.query()``)."""

    def __init__(self, items: list) -> None:
        self._items = items
        self.queries: list[str] = []

    async def query(self, query: str, parameters=None, partition_key=None) -> list:
        self.queries.append(query)
        return list(self._items)


class TestFetchAll:
    async def test_none_sink_returns_empty_list(self):
        assert await fetch_all(None) == []

    async def test_sync_all_sink(self):
        sink = SyncAllSink([1, 2, 3])
        assert await fetch_all(sink) == [1, 2, 3]

    async def test_async_all_sink(self):
        sink = AsyncAllSink(["a", "b"])
        assert await fetch_all(sink) == ["a", "b"]

    async def test_query_only_sink_issues_select_star(self):
        sink = AsyncQueryOnlySink([{"id": "x"}])
        result = await fetch_all(sink)
        assert result == [{"id": "x"}]
        assert sink.queries == ["SELECT * FROM c"]

    async def test_sink_with_neither_all_nor_query_returns_empty_list(self):
        class Bare:
            pass

        assert await fetch_all(Bare()) == []


class InMemoryBlobLikeSink:
    def __init__(self, documents: dict[str, str]) -> None:
        self.documents = documents


class ProductionBlobLikeSink:
    def __init__(self, contents: dict[str, str]) -> None:
        self._contents = contents
        self.calls: list[str] = []

    async def download_document_text(self, blob_path: str) -> str:
        self.calls.append(blob_path)
        return self._contents[blob_path]


class TestReadBlobText:
    async def test_none_blob_path_returns_empty_string(self):
        assert await read_blob_text(InMemoryBlobLikeSink({}), None) == ""

    async def test_none_sink_returns_empty_string(self):
        assert await read_blob_text(None, "documents/sec/doc.htm") == ""

    async def test_reads_from_in_memory_documents_dict(self):
        sink = InMemoryBlobLikeSink({"documents/sec-a/doc-1.htm": "raw filing text"})
        assert await read_blob_text(sink, "documents/sec-a/doc-1.htm") == "raw filing text"

    async def test_missing_path_in_memory_sink_returns_empty_string(self):
        sink = InMemoryBlobLikeSink({})
        assert await read_blob_text(sink, "documents/sec-a/doc-missing.htm") == ""

    async def test_reads_via_production_download_document_text(self):
        sink = ProductionBlobLikeSink({"documents/sec-a/doc-1.htm": "production raw text"})
        result = await read_blob_text(sink, "documents/sec-a/doc-1.htm")
        assert result == "production raw text"
        assert sink.calls == ["documents/sec-a/doc-1.htm"]

    async def test_sink_with_neither_capability_returns_empty_string(self):
        class Bare:
            pass

        assert await read_blob_text(Bare(), "documents/sec-a/doc-1.htm") == ""
