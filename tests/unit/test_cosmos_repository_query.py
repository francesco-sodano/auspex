from datetime import date

import pytest

from auspex.models.scoring import ScoreSnapshot
from auspex.persistence.repositories import CosmosFxSink, CosmosPriceSink, CosmosRepository


class StrictAsyncContainer:
    def __init__(self, rows=None):
        self.calls = []
        self.rows = list(rows or [])

    def query_items(self, *, query, parameters, partition_key=None):
        self.calls.append(
            {"query": query, "parameters": parameters, "partition_key": partition_key}
        )

        async def rows():
            for row in self.rows:
                yield row

        return rows()


class MixedMarketContainer(StrictAsyncContainer):
    def query_items(self, *, query, parameters, partition_key=None):
        selected = self.rows
        if "security_id" in query:
            selected = [row for row in self.rows if "security_id" in row]
        elif "c.pair" in query:
            selected = [row for row in self.rows if "pair" in row]

        async def rows():
            for row in selected:
                yield row

        return rows()


class Context:
    def __init__(self, container):
        self._container = container

    async def container(self, name):
        return self._container


class AggregateContainer(StrictAsyncContainer):
    def query_items(self, *, query, parameters, partition_key=None):
        async def rows():
            if "DISTINCT VALUE" in query:
                yield "2026-08-08"
                yield "2026-08-09"
            elif "VALUE COUNT" in query:
                as_of_date = next(
                    item["value"] for item in parameters if item["name"] == "@as_of_date"
                )
                yield 85 if as_of_date == "2026-08-08" else 84

        return rows()


@pytest.mark.asyncio
async def test_cross_partition_query_omits_unsupported_sdk_flag():
    container = StrictAsyncContainer()
    repository = CosmosRepository(Context(container), "scores", ScoreSnapshot)

    assert await repository.query("SELECT * FROM c") == []
    assert container.calls == [
        {"query": "SELECT * FROM c", "parameters": [], "partition_key": None}
    ]


@pytest.mark.asyncio
async def test_partition_query_passes_only_partition_key():
    container = StrictAsyncContainer()
    repository = CosmosRepository(Context(container), "scores", ScoreSnapshot)

    assert await repository.query(
        "SELECT * FROM c WHERE c.security_id=@id",
        [{"name": "@id", "value": "sec-1"}],
        partition_key="sec-1",
    ) == []
    assert container.calls[0]["partition_key"] == "sec-1"


@pytest.mark.asyncio
async def test_query_removes_cosmos_system_properties_before_validation():
    container = StrictAsyncContainer(
        [
            {
                "id": "sec-1:2026-08-08",
                "security_id": "sec-1",
                "as_of_date": "2026-08-08",
                "config_version_id": "config-1",
                "cohort_used": "semi-compute",
                "cohort_confidence": "HIGH",
                "filer_profile": "DOMESTIC",
                "coverage": "1",
                "legs": {},
                "composite": "0",
                "percentile": 50,
                "direction": "STABLE",
                "package_fingerprint": "sha256:test",
                "max_knowledge_date": "2026-08-08",
                "_rid": "rid",
                "_self": "self",
                "_etag": "etag",
                "_attachments": "attachments/",
                "_ts": 1,
            }
        ]
    )
    repository = CosmosRepository(Context(container), "scores", ScoreSnapshot)

    rows = await repository.query("SELECT * FROM c")

    assert rows[0].security_id == "sec-1"


@pytest.mark.asyncio
async def test_market_sinks_filter_mixed_price_and_fx_rows():
    container = MixedMarketContainer(
        [
            {
                "id": "sec-1:2026-08-08",
                "security_id": "sec-1",
                "session_date": "2026-08-08",
                "open_raw": "1",
                "high_raw": "2",
                "low_raw": "1",
                "close_raw": "2",
                "volume": 10,
                "close_adjusted": "2",
            },
            {
                "id": "USDCHF:2026-08-08",
                "pair": "USDCHF",
                "session_date": "2026-08-08",
                "close_rate": "0.8",
            },
        ]
    )
    context = Context(container)

    prices = await CosmosPriceSink(context).all()
    rates = await CosmosFxSink(context).all()

    assert [price.security_id for price in prices] == ["sec-1"]
    assert [rate.pair for rate in rates] == ["USDCHF"]


@pytest.mark.asyncio
async def test_valid_score_counts_use_supported_cross_partition_aggregates():
    repository = CosmosRepository(
        Context(AggregateContainer()), "scores", ScoreSnapshot
    )

    counts = await repository.valid_score_counts_by_date(
        date(2026, 8, 8), date(2026, 8, 9)
    )

    assert counts == {date(2026, 8, 8): 85, date(2026, 8, 9): 84}
