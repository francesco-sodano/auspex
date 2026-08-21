"""In-memory fakes for tests and pipeline fixtures.

These implement the same protocols the real Cosmos/Blob-backed sinks
implement (:mod:`auspex.collectors.base`, :mod:`auspex.persistence.repositories`)
so pipeline and integration tests can run the full 20-step pipeline without
any Azure dependency.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

from auspex.marketdata.quarantine import exclude_quarantined
from auspex.models.document import Document
from auspex.models.market_integrity import MarketDataRepairManifest

T = TypeVar("T", bound=BaseModel)


class InMemoryRepository(Generic[T]):
    """Generic keyed store standing in for a Cosmos container."""

    def __init__(self) -> None:
        self._items: dict[str, T] = {}

    async def upsert(self, item: T) -> None:
        self._items[item.id] = item  # type: ignore[attr-defined]

    async def get(self, id_: str, partition_key: str | None = None) -> T | None:
        return self._items.get(id_)

    async def query(self, predicate=None) -> list[T]:
        values = list(self._items.values())
        if predicate is None:
            return values
        return [v for v in values if predicate(v)]

    def all(self) -> list[T]:
        return list(self._items.values())


class InMemoryWatermarkStore:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get_watermark(self, key: str) -> str | None:
        return self._data.get(key)

    async def set_watermark(self, key: str, value: str) -> None:
        self._data[key] = value


class InMemoryDocumentSink:
    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}

    async def upsert_document(self, doc: Document) -> None:
        self._docs[doc.id] = doc

    async def find_by_content_hash(self, security_id: str, content_hash: str) -> Document | None:
        for doc in self._docs.values():
            if doc.security_id == security_id and doc.content_hash == content_hash:
                return doc
        return None

    def all(self) -> list[Document]:
        return list(self._docs.values())


class InMemoryPriceSink:
    """Quarantine-aware price sink.

    ``all()`` mirrors :class:`auspex.persistence.repositories.CosmosPriceSink`
    and hides quarantined bars from scoring/performance; ``raw_all()`` is the
    unfiltered view the integrity pass needs.
    """

    def __init__(self) -> None:
        self._bars: dict[str, object] = {}

    async def upsert_price_bar(self, bar) -> None:  # noqa: ANN001
        self._bars[bar.id] = bar

    def raw_all(self) -> list:
        return list(self._bars.values())

    def all(self) -> list:
        return exclude_quarantined(self._bars.values())


class InMemoryPriceIntegrityStore:
    """Unfiltered, partition-style view over an :class:`InMemoryPriceSink`.

    Satisfies :class:`auspex.marketdata.service.PriceIntegrityStore`.
    """

    def __init__(self, sink: InMemoryPriceSink) -> None:
        self._sink = sink

    async def security_ids(self) -> list[str]:
        return sorted({str(bar.security_id) for bar in self._sink.raw_all()})

    async def bars_for_security(self, security_id: str) -> list:
        rows = [bar for bar in self._sink.raw_all() if bar.security_id == security_id]
        return sorted(rows, key=lambda bar: (bar.session_date, bar.id))

    async def upsert_bar(self, bar) -> None:  # noqa: ANN001
        await self._sink.upsert_price_bar(bar)


class InMemoryRepairManifestStore:
    """Append-only manifest store standing in for ``config_versions``."""

    def __init__(self) -> None:
        self._items: dict[str, MarketDataRepairManifest] = {}

    async def latest(self) -> MarketDataRepairManifest | None:
        rows = await self.history(limit=1)
        return rows[0] if rows else None

    async def history(self, limit: int = 20) -> list[MarketDataRepairManifest]:
        rows = sorted(self._items.values(), key=lambda item: item.revision, reverse=True)
        return rows[: max(1, limit)]

    async def upsert(self, manifest: MarketDataRepairManifest) -> None:
        self._items[manifest.id] = manifest


class InMemoryFxSink:
    def __init__(self) -> None:
        self._rates: dict[str, object] = {}

    async def upsert_fx_rate(self, rate) -> None:  # noqa: ANN001
        self._rates[rate.id] = rate

    def all(self) -> list:
        return list(self._rates.values())


class InMemoryFundamentalSink:
    def __init__(self) -> None:
        self._snapshots: dict[str, object] = {}

    async def upsert_fundamental_snapshot(self, snapshot) -> None:  # noqa: ANN001
        self._snapshots[snapshot.id] = snapshot

    def all(self) -> list:
        return list(self._snapshots.values())


class InMemoryBlobSink:
    """Stores blob content in-process; returns the same path convention as the real client."""

    def __init__(self) -> None:
        self.documents: dict[str, str | bytes] = {}
        self.sections: dict[str, str] = {}
        self.exports: dict[str, bytes] = {}

    async def upload_document_blob(self, security_id: str, document_id: str, ext: str, content: bytes | str) -> str:
        path = f"documents/{security_id}/{document_id}.{ext}"
        self.documents[path] = content
        return path

    async def upload_section_blob(self, security_id: str, document_id: str, item: str, content: str) -> str:
        path = f"sections/{security_id}/{document_id}/{item}.txt"
        self.sections[path] = content
        return path

    async def upload_export_blob(self, user_id: str, upload_id: str, ext: str, content: bytes) -> str:
        path = f"exports/{user_id}/{upload_id}.{ext}"
        self.exports[path] = content
        return path


class InMemoryReadOnlyContainer:
    """Fake standing in for a read-only Cosmos container (arc42 §5.7).

    Only exposes ``query_items`` — the same read-only surface as
    :class:`auspex.portfolio.adapter.ReadOnlyContainer` — over an in-memory
    list of documents. Used to test :class:`~auspex.portfolio.adapter.PortfolioAdapter`
    and to prove (arc42 A-11) that it never attempts a write: this fake simply
    has no write methods to call.
    """

    def __init__(self, documents: list[dict]) -> None:
        self._documents = documents

    def query_items(self, query: str, parameters: list[dict] | None = None, partition_key: str | None = None):
        # A deliberately simplistic query "engine": returns every document for
        # `SELECT * FROM c ...` and unwraps a single field for `SELECT VALUE c.field FROM c`.
        docs = list(self._documents)

        async def _iterate():
            value_field = None
            if query.strip().upper().startswith("SELECT VALUE"):
                # "SELECT VALUE c.<field> FROM c ..." — pull out <field>
                after_value = query.split("VALUE", 1)[1].strip()
                path = after_value.split("FROM")[0].strip()
                value_field = path.split(".", 1)[1] if "." in path else path
            for doc in docs:
                yield doc.get(value_field) if value_field else doc

        return _iterate()


class InMemoryReadOnlyDatabase:
    """Fake standing in for the source ledger's read-only Cosmos database."""

    def __init__(self, containers: dict[str, list[dict]] | None = None) -> None:
        self._containers = containers or {}

    def get_container_client(self, name: str) -> InMemoryReadOnlyContainer:
        return InMemoryReadOnlyContainer(self._containers.get(name, []))
