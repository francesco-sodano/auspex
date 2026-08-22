"""Generic Cosmos repository — idempotent upsert on `id` (+ implicit partition key).

arc42 §6.1 idempotency: "every write upserts on `security_id + as_of_date`."
Every model in :mod:`auspex.models` exposes a stable ``id`` and a
``partition_key`` property matching its container's partition key path
(§5.11), so a plain Cosmos ``upsert_item`` call is naturally idempotent:
re-running a date replaces that date's rows and produces identical output.

Beyond :class:`CosmosRepository` itself, this module also provides the
concrete Cosmos-backed adapters that satisfy the sink protocols pipeline
steps depend on (:mod:`auspex.collectors.base`,
:mod:`auspex.extraction.channel_a`, :mod:`auspex.extraction.channel_b`,
:mod:`auspex.narrative.generator`) — ``CosmosDocumentSink``,
``CosmosPriceSink``, ``CosmosFxSink``, ``CosmosFundamentalSink``,
``CosmosChannelAExtractionSink``, ``CosmosChannelBDigestSink``,
``CosmosNarrativeSink``, and ``CosmosWatermarkStore``. Each is a thin,
name-translating wrapper around one ``CosmosRepository`` (or, for the two
sinks whose rows aren't a domain model, the raw ``CosmosContext``
container) so :mod:`auspex.pipeline.steps` can read/write against real
Cosmos containers via the exact same duck-typed calls it makes against the
in-memory test fixtures (:mod:`auspex.persistence.memory`).
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Generic, TypeVar

from azure.core import MatchConditions
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from pydantic import BaseModel

from auspex.extraction.cache import channel_a_cache_key
from auspex.marketdata.quarantine import QUARANTINE_SQL_PREDICATE
from auspex.models.document import Document
from auspex.models.extraction import ChannelAExtraction, ChannelBDigest
from auspex.models.fundamentals import FundamentalSnapshot
from auspex.models.market import FxRate, PriceBar
from auspex.models.market_integrity import MANIFEST_CONFIG_TYPE, MarketDataRepairManifest
from auspex.persistence.cosmos_client import CosmosContext

T = TypeVar("T", bound=BaseModel)


def _domain_document(raw: dict) -> dict:
    return {key: value for key, value in raw.items() if not key.startswith("_")}


class CosmosRepository(Generic[T]):
    def __init__(self, context: CosmosContext, container_name: str, model_cls: type[T]) -> None:
        self._context = context
        self._container_name = container_name
        self._model_cls = model_cls

    async def upsert(self, item: T) -> None:
        container = await self._context.container(self._container_name)
        await container.upsert_item(item.model_dump(mode="json"))

    async def delete(self, id_: str, partition_key: str) -> bool:
        """Hard-delete one document. Returns ``False`` if it was already gone.

        Deleting an absent document is not an error: account deletion is
        retried until every partition verifies empty, so "already gone" is the
        success case on a replay.
        """

        container = await self._context.container(self._container_name)
        try:
            await container.delete_item(item=id_, partition_key=partition_key)
        except CosmosResourceNotFoundError:
            return False
        return True

    async def partition_ids(self, partition_key: str) -> list[str]:
        """Every document id inside one logical partition.

        Deliberately partition-scoped: this is the only enumeration account
        deletion needs, and it never becomes a cross-partition scan.
        """

        container = await self._context.container(self._container_name)
        items = container.query_items(
            query="SELECT VALUE c.id FROM c",
            parameters=[],
            partition_key=partition_key,
        )
        return [str(raw) async for raw in items]

    async def count_partition(self, partition_key: str) -> int:
        """Number of documents left in one logical partition."""

        container = await self._context.container(self._container_name)
        items = container.query_items(
            query="SELECT VALUE COUNT(1) FROM c",
            parameters=[],
            partition_key=partition_key,
        )
        rows = [raw async for raw in items]
        return int(rows[0]) if rows else 0

    async def purge_partition(self, partition_key: str) -> int:
        """Delete every document in one logical partition. Idempotent.

        Returns the number of documents actually removed by this call; a
        replay over an already-empty partition returns ``0`` and succeeds.
        """

        deleted = 0
        for document_id in await self.partition_ids(partition_key):
            if await self.delete(document_id, partition_key):
                deleted += 1
        return deleted

    async def get(self, id_: str, partition_key: str) -> T | None:
        container = await self._context.container(self._container_name)
        try:
            raw = await container.read_item(item=id_, partition_key=partition_key)
        except CosmosResourceNotFoundError:
            return None
        return self._model_cls.model_validate(_domain_document(raw))

    async def get_with_etag(self, id_: str, partition_key: str) -> tuple[T, str] | None:
        """Point-read a document together with its Cosmos concurrency token."""

        container = await self._context.container(self._container_name)
        try:
            raw = await container.read_item(item=id_, partition_key=partition_key)
        except CosmosResourceNotFoundError:
            return None
        return self._model_cls.model_validate(_domain_document(raw)), str(raw["_etag"])

    async def replace_if_match(self, item: T, etag: str) -> bool:
        """Replace only when no other process changed the document."""

        container = await self._context.container(self._container_name)
        try:
            await container.replace_item(
                item=item.id,
                body=item.model_dump(mode="json"),
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code == 412:
                return False
            raise
        return True

    async def query(
        self, query: str, parameters: list[dict] | None = None, partition_key: str | None = None
    ) -> list[T]:
        container = await self._context.container(self._container_name)
        options = {"query": query, "parameters": parameters or []}
        if partition_key is not None:
            options["partition_key"] = partition_key
        items = container.query_items(**options)
        results = []
        async for raw in items:
            results.append(self._model_cls.model_validate(_domain_document(raw)))
        return results

    async def raw_query(
        self,
        query: str,
        parameters: list[dict] | None = None,
        partition_key: str | None = None,
    ) -> list:
        container = await self._context.container(self._container_name)
        options = {"query": query, "parameters": parameters or []}
        if partition_key is not None:
            options["partition_key"] = partition_key
        items = container.query_items(**options)
        return [
            _domain_document(raw) if isinstance(raw, dict) else raw
            async for raw in items
        ]

    async def valid_score_counts_by_date(
        self, start_date: date, end_date: date
    ) -> dict[date, int]:
        dates = await self.raw_query(
            (
                "SELECT DISTINCT VALUE c.as_of_date FROM c "
                "WHERE c.is_backfilled = true AND IS_NUMBER(c.percentile) "
                "AND c.as_of_date >= @start AND c.as_of_date <= @end"
            ),
            [
                {"name": "@start", "value": start_date.isoformat()},
                {"name": "@end", "value": end_date.isoformat()},
            ],
        )
        semaphore = asyncio.Semaphore(16)

        async def count(as_of_date: str) -> tuple[date, int]:
            async with semaphore:
                rows = await self.raw_query(
                    (
                        "SELECT VALUE COUNT(1) FROM c "
                        "WHERE c.is_backfilled = true AND IS_NUMBER(c.percentile) "
                        "AND c.as_of_date = @as_of_date"
                    ),
                    [{"name": "@as_of_date", "value": as_of_date}],
                )
                return date.fromisoformat(as_of_date), int(rows[0]) if rows else 0

        return dict(await asyncio.gather(*(count(item) for item in dates)))

    async def for_dates(self, dates: set[date]) -> list[T]:
        if not dates:
            return []
        return await self.query(
            "SELECT * FROM c WHERE ARRAY_CONTAINS(@dates, c.as_of_date)",
            [{"name": "@dates", "value": sorted(item.isoformat() for item in dates)}],
        )

    async def all(self) -> list[T]:
        """Every row in the container (cross-partition) — backs the same
        ``.all()`` surface :mod:`auspex.persistence.memory`'s in-memory
        fakes expose, via :func:`auspex.pipeline.repo_access.fetch_all`, so
        pipeline steps read identically regardless of which is wired."""

        return await self.query("SELECT * FROM c")


class CosmosDocumentSink:
    """Backs :class:`auspex.collectors.base.DocumentSink` over the
    ``documents`` container (arc42 §5.3, §5.11)."""

    def __init__(self, context: CosmosContext, container_name: str = "documents") -> None:
        self._repo: CosmosRepository[Document] = CosmosRepository(context, container_name, Document)

    async def upsert_document(self, doc: Document) -> None:
        await self._repo.upsert(doc)

    async def find_by_content_hash(self, security_id: str, content_hash: str) -> Document | None:
        results = await self._repo.query(
            "SELECT * FROM c WHERE c.security_id=@sid AND c.content_hash=@h",
            [{"name": "@sid", "value": security_id}, {"name": "@h", "value": content_hash}],
            partition_key=security_id,
        )
        return results[0] if results else None

    async def all(self) -> list[Document]:
        return await self._repo.all()


class CosmosPriceSink:
    """Backs :class:`auspex.collectors.base.PriceSink` over the
    ``market_daily`` container (arc42 §5.3, §5.11).

    Every read excludes quarantined bars: a bar the market-data integrity pass
    could not justify must not reach scoring, performance or the API until it
    is repaired or released. Writes are unfiltered — the raw observation is
    always preserved. Use :class:`CosmosPriceIntegrityStore` for the
    unfiltered read surface the repair pass needs.
    """

    def __init__(self, context: CosmosContext, container_name: str = "market_daily") -> None:
        self._repo: CosmosRepository[PriceBar] = CosmosRepository(context, container_name, PriceBar)

    async def upsert_price_bar(self, bar: PriceBar) -> None:
        await self._repo.upsert(bar)

    async def all(self) -> list[PriceBar]:
        return await self._repo.query(
            f"SELECT * FROM c WHERE IS_DEFINED(c.security_id) AND {QUARANTINE_SQL_PREDICATE}"
        )

    async def latest_as_of(self, as_of: date, security_ids: list[str]) -> list[PriceBar]:
        async def latest(security_id: str) -> PriceBar | None:
            rows = await self._repo.query(
                (
                    "SELECT TOP 1 * FROM c WHERE c.security_id=@security_id "
                    f"AND c.session_date<=@as_of AND {QUARANTINE_SQL_PREDICATE} "
                    "ORDER BY c.session_date DESC"
                ),
                [
                    {"name": "@security_id", "value": security_id},
                    {"name": "@as_of", "value": as_of.isoformat()},
                ],
                partition_key=security_id,
            )
            return rows[0] if rows else None

        rows = await asyncio.gather(*(latest(security_id) for security_id in security_ids))
        return [row for row in rows if row is not None]

    async def history_as_of(self, security_id: str, as_of: date, days: int = 7) -> list[PriceBar]:
        limit = max(1, min(days, 130))
        rows = await self._repo.query(
            (
                f"SELECT TOP {limit} * FROM c WHERE c.security_id=@security_id "
                f"AND c.session_date<=@as_of AND {QUARANTINE_SQL_PREDICATE} "
                "ORDER BY c.session_date DESC"
            ),
            [
                {"name": "@security_id", "value": security_id},
                {"name": "@as_of", "value": as_of.isoformat()},
            ],
            partition_key=security_id,
        )
        return list(reversed(rows))


class CosmosPriceIntegrityStore:
    """Unfiltered ``market_daily`` access for the integrity/repair pass.

    Satisfies :class:`auspex.marketdata.service.PriceIntegrityStore`. Reads are
    partition-scoped (``/security_id``) so a repair pass never fans out across
    partitions, and they deliberately include quarantined bars — those must be
    re-examined on every pass so they can be released once repaired.
    """

    def __init__(self, context: CosmosContext, container_name: str = "market_daily") -> None:
        self._repo: CosmosRepository[PriceBar] = CosmosRepository(context, container_name, PriceBar)

    async def security_ids(self) -> list[str]:
        rows = await self._repo.raw_query(
            "SELECT DISTINCT VALUE c.security_id FROM c WHERE IS_DEFINED(c.security_id)"
        )
        return sorted({str(row) for row in rows if row})

    async def bars_for_security(self, security_id: str) -> list[PriceBar]:
        return await self._repo.query(
            (
                "SELECT * FROM c WHERE c.security_id=@security_id "
                "AND IS_DEFINED(c.session_date) ORDER BY c.session_date ASC"
            ),
            [{"name": "@security_id", "value": security_id}],
            partition_key=security_id,
        )

    async def upsert_bar(self, bar: PriceBar) -> None:
        await self._repo.upsert(bar)


class CosmosRepairManifestStore:
    """Append-only market-data repair manifests in ``config_versions``.

    The manifest shares the existing ``config_versions`` container (partition
    key ``/config_type``) under its own ``config_type`` discriminator, so every
    read is an explicit single-partition query and no infrastructure change is
    required.
    """

    def __init__(self, context: CosmosContext, container_name: str = "config_versions") -> None:
        self._repo: CosmosRepository[MarketDataRepairManifest] = CosmosRepository(
            context, container_name, MarketDataRepairManifest
        )

    async def latest(self) -> MarketDataRepairManifest | None:
        rows = await self.history(limit=1)
        return rows[0] if rows else None

    async def history(self, limit: int = 20) -> list[MarketDataRepairManifest]:
        top = max(1, min(limit, 100))
        return await self._repo.query(
            (
                f"SELECT TOP {top} * FROM c WHERE c.config_type=@config_type "
                "ORDER BY c.revision DESC"
            ),
            [{"name": "@config_type", "value": MANIFEST_CONFIG_TYPE}],
            partition_key=MANIFEST_CONFIG_TYPE,
        )

    async def upsert(self, manifest: MarketDataRepairManifest) -> None:
        await self._repo.upsert(manifest)


class CosmosFxSink:
    """Backs :class:`auspex.collectors.base.FxSink` over the shared
    ``market_daily`` container (arc42 §5.3, §5.11)."""

    def __init__(self, context: CosmosContext, container_name: str = "market_daily") -> None:
        self._repo: CosmosRepository[FxRate] = CosmosRepository(context, container_name, FxRate)

    async def upsert_fx_rate(self, rate: FxRate) -> None:
        await self._repo.upsert(rate)

    async def all(self) -> list[FxRate]:
        return await self._repo.query("SELECT * FROM c WHERE IS_DEFINED(c.pair)")


class CosmosFundamentalSink:
    """Backs :class:`auspex.collectors.base.FundamentalSink` over the
    ``fundamentals`` container (arc42 §5.3, §5.11)."""

    def __init__(self, context: CosmosContext, container_name: str = "fundamentals") -> None:
        self._repo: CosmosRepository[FundamentalSnapshot] = CosmosRepository(
            context, container_name, FundamentalSnapshot
        )

    async def upsert_fundamental_snapshot(self, snapshot: FundamentalSnapshot) -> None:
        await self._repo.upsert(snapshot)

    async def all(self) -> list[FundamentalSnapshot]:
        return await self._repo.all()


class CosmosChannelAExtractionSink:
    """Backs :class:`auspex.extraction.channel_a.ChannelAExtractionSink` over
    the ``extractions`` container (arc42 §5.4).

    ``cache_key`` (``security_id|content_hash|model_version|prompt_version|
    schema_version|taxonomy_version``,
    :func:`auspex.extraction.cache.channel_a_cache_key`)
    is a derived property on :class:`~auspex.models.extraction.ChannelAExtraction`,
    not a stored field, so ``find_by_cache_key`` splits it back into its
    component filters rather than querying on the joined string.
    """

    def __init__(self, context: CosmosContext, container_name: str = "extractions") -> None:
        self._repo: CosmosRepository[ChannelAExtraction] = CosmosRepository(
            context, container_name, ChannelAExtraction
        )

    async def find_by_cache_key(self, cache_key: str) -> ChannelAExtraction | None:
        parts = cache_key.split("|")
        if len(parts) != 6:
            return None
        (
            security_id,
            content_hash,
            model_version,
            prompt_version,
            schema_version,
            taxonomy_version,
        ) = parts
        results = await self._repo.query(
            "SELECT * FROM c WHERE c.content_hash=@ch AND c.model_version=@mv AND c.prompt_version=@pv "
            "AND c.schema_version=@sv AND c.taxonomy_version=@tv",
            [
                {"name": "@ch", "value": content_hash},
                {"name": "@mv", "value": model_version},
                {"name": "@pv", "value": prompt_version},
                {"name": "@sv", "value": schema_version},
                {"name": "@tv", "value": taxonomy_version},
            ],
            partition_key=security_id,
        )
        for candidate in results:
            if channel_a_cache_key(
                security_id=candidate.security_id,
                content_hash=candidate.content_hash,
                model_version=candidate.model_version,
                prompt_version=candidate.prompt_version,
                schema_version=candidate.schema_version,
                taxonomy_version=candidate.taxonomy_version,
            ) == cache_key:
                return candidate
        return None

    async def upsert(self, extraction: ChannelAExtraction) -> None:
        await self._repo.upsert(extraction)

    async def all(self) -> list[ChannelAExtraction]:
        return await self._repo.all()


class CosmosChannelBDigestSink:
    """Backs :class:`auspex.extraction.channel_b.ChannelBDigestSink` over
    the ``digests`` container (arc42 §5.4). Same cache-key-splitting
    approach as :class:`CosmosChannelAExtractionSink` — see its docstring."""

    def __init__(self, context: CosmosContext, container_name: str = "digests") -> None:
        self._repo: CosmosRepository[ChannelBDigest] = CosmosRepository(context, container_name, ChannelBDigest)

    async def find_by_cache_key(self, cache_key: str) -> ChannelBDigest | None:
        parts = cache_key.split("|")
        if len(parts) != 4:
            return None
        security_id, content_hash, model_version, prompt_version = parts
        results = await self._repo.query(
            "SELECT * FROM c WHERE c.content_hash=@ch AND c.model_version=@mv AND c.prompt_version=@pv",
            [
                {"name": "@ch", "value": content_hash},
                {"name": "@mv", "value": model_version},
                {"name": "@pv", "value": prompt_version},
            ],
            partition_key=security_id,
        )
        return results[0] if results else None

    async def upsert(self, digest: ChannelBDigest) -> None:
        await self._repo.upsert(digest)

    async def all(self) -> list[ChannelBDigest]:
        return await self._repo.all()


class CosmosNarrativeSink:
    """Backs :class:`auspex.narrative.generator.NarrativeSink` over a
    ``narratives`` container of simple ``{id, cache_key, narrative,
    model_version}`` rows, keyed by the narrative cache key
    (``package_fingerprint + model_version + prompt_version``, arc42 §5.9)
    so replaying an unchanged day's narrative is a pure cache hit — never a
    second LLM call. Rows are plain dicts (no domain model in
    :mod:`auspex.models`), so this talks to the container directly rather
    than through :class:`CosmosRepository`.
    """

    def __init__(self, context: CosmosContext, container_name: str = "narratives") -> None:
        self._context = context
        self._container_name = container_name

    async def find_by_cache_key(self, cache_key: str) -> str | None:
        container = await self._context.container(self._container_name)
        try:
            raw = await container.read_item(item=cache_key, partition_key=cache_key)
        except Exception:  # noqa: BLE001 - azure.cosmos raises CosmosResourceNotFoundError
            return None
        return raw.get("narrative") if isinstance(raw, dict) else None

    async def store(self, cache_key: str, narrative: str, model_version: str) -> None:
        container = await self._context.container(self._container_name)
        await container.upsert_item(
            {"id": cache_key, "cache_key": cache_key, "narrative": narrative, "model_version": model_version}
        )


class CosmosWatermarkStore:
    """Backs :class:`auspex.collectors.base.WatermarkStore` over the
    ``watermarks`` container (partition key ``/scope``, arc42 §5.11)."""

    def __init__(self, context: CosmosContext, container_name: str = "watermarks", scope: str = "watermarks") -> None:
        self._context = context
        self._container_name = container_name
        self._scope = scope

    async def get_watermark(self, key: str) -> str | None:
        container = await self._context.container(self._container_name)
        try:
            raw = await container.read_item(item=key, partition_key=self._scope)
        except Exception:  # noqa: BLE001 - azure.cosmos raises CosmosResourceNotFoundError
            return None
        return raw.get("value") if isinstance(raw, dict) else None

    async def set_watermark(self, key: str, value: str) -> None:
        container = await self._context.container(self._container_name)
        await container.upsert_item({"id": key, "scope": self._scope, "value": value})
